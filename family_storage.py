"""Majestic RP Miami: profiles, capts, reminders, badges (SQLite).

This mixin deliberately shares the SAME connection/transaction as the legacy bot.
No schema or records from v5 are deleted.
"""
from __future__ import annotations

import sqlite3
import time


class FamilyStorageMixin:
    def init_family_storage(self):
        with self.conn:
            self.conn.executescript("""
                CREATE TABLE IF NOT EXISTS family_profiles (
                    guild_id INTEGER NOT NULL,
                    user_id INTEGER NOT NULL,
                    game_nick TEXT NOT NULL,
                    static_number TEXT NOT NULL,
                    direction TEXT NOT NULL DEFAULT 'Не указано',
                    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    PRIMARY KEY (guild_id, user_id)
                );
                CREATE TABLE IF NOT EXISTS capt_settings (
                    guild_id INTEGER PRIMARY KEY,
                    channel_id INTEGER,
                    log_channel_id INTEGER,
                    clips_channel_id INTEGER,
                    leader_role_id INTEGER,
                    announce_role_id INTEGER,
                    voice_channel_id INTEGER,
                    hub_message_id INTEGER,
                    timezone TEXT NOT NULL DEFAULT 'Europe/Moscow'
                );
                CREATE TABLE IF NOT EXISTS capt_events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    guild_id INTEGER NOT NULL,
                    created_by INTEGER NOT NULL,
                    opponent TEXT NOT NULL,
                    location TEXT NOT NULL,
                    format_text TEXT NOT NULL,
                    scheduled_at INTEGER NOT NULL,
                    capacity INTEGER NOT NULL CHECK (capacity BETWEEN 1 AND 100),
                    status TEXT NOT NULL DEFAULT 'scheduled' CHECK(status IN ('scheduled','finished','cancelled')),
                    result TEXT CHECK (result IN ('win','loss','draw') OR result IS NULL),
                    replay_url TEXT,
                    notes TEXT,
                    channel_id INTEGER,
                    message_id INTEGER UNIQUE,
                    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
                );
                CREATE TABLE IF NOT EXISTS capt_signups (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    event_id INTEGER NOT NULL REFERENCES capt_events(id) ON DELETE CASCADE,
                    user_id INTEGER NOT NULL,
                    choice TEXT NOT NULL CHECK(choice IN ('main','reserve','declined')),
                    attended INTEGER CHECK(attended IN (0,1) OR attended IS NULL),
                    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    UNIQUE (event_id, user_id)
                );
                CREATE INDEX IF NOT EXISTS idx_capt_signups_choices
                    ON capt_signups(event_id,choice,id);
                CREATE INDEX IF NOT EXISTS idx_capt_events_guild_time
                    ON capt_events(guild_id,scheduled_at);
                CREATE TABLE IF NOT EXISTS capt_reminders (
                    event_id INTEGER NOT NULL REFERENCES capt_events(id) ON DELETE CASCADE,
                    remaining_minutes INTEGER NOT NULL,
                    sent_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    PRIMARY KEY(event_id,remaining_minutes)
                );
                CREATE TABLE IF NOT EXISTS capt_clips (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    guild_id INTEGER NOT NULL,
                    event_id INTEGER NOT NULL REFERENCES capt_events(id) ON DELETE CASCADE,
                    user_id INTEGER NOT NULL,
                    url TEXT NOT NULL,
                    comment TEXT NOT NULL DEFAULT '',
                    status TEXT NOT NULL DEFAULT 'pending' CHECK(status IN ('pending','approved','rejected')),
                    reviewer_id INTEGER,
                    message_id INTEGER UNIQUE,
                    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    reviewed_at TEXT
                );
                CREATE INDEX IF NOT EXISTS idx_capt_clips_user
                    ON capt_clips(guild_id,user_id,status);
            """)
            # Safe upgrade from v6: add the separate role to ping when a capt
            # is created. The existing capts, signups and settings are untouched.
            columns = {row["name"] for row in self.conn.execute("PRAGMA table_info(capt_settings)")}
            if "announce_role_id" not in columns:
                self.conn.execute("ALTER TABLE capt_settings ADD COLUMN announce_role_id INTEGER")

    # --- Per-server capt configuration ------------------------------------
    def capt_settings(self, guild_id: int):
        with self.conn:
            self.conn.execute("INSERT OR IGNORE INTO capt_settings(guild_id) VALUES(?)", (guild_id,))
        return self.conn.execute("SELECT * FROM capt_settings WHERE guild_id=?", (guild_id,)).fetchone()

    def set_capt_settings(self, guild_id: int, **values):
        allowed = {"channel_id", "log_channel_id", "clips_channel_id",
                   "leader_role_id", "announce_role_id", "voice_channel_id", "hub_message_id", "timezone"}
        if not values or set(values) - allowed:
            raise ValueError("Неизвестная настройка каптов")
        fields = ", ".join(f"{key}=?" for key in values)
        with self.conn:
            self.conn.execute("INSERT OR IGNORE INTO capt_settings(guild_id) VALUES(?)", (guild_id,))
            self.conn.execute(f"UPDATE capt_settings SET {fields} WHERE guild_id=?",
                              (*values.values(), guild_id))

    # --- Member profile --------------------------------------------------
    def profile(self, guild_id: int, user_id: int):
        return self.conn.execute("SELECT * FROM family_profiles WHERE guild_id=? AND user_id=?",
                                 (guild_id, user_id)).fetchone()

    def upsert_profile(self, guild_id: int, user_id: int, nick: str, static: str,
                       direction: str | None = None):
        nick, static = nick.strip(), static.strip()
        if not (2 <= len(nick) <= 60) or not static.isascii() or not static.isdecimal() or not (1 <= len(static) <= 20):
            raise ValueError("Ник: 2–60 символов; статик: только цифры (1–20)")
        direction = direction if direction in ("Crime", "State") else "Не указано"
        with self.conn:
            self.conn.execute("""INSERT INTO family_profiles(guild_id,user_id,game_nick,static_number,direction)
                VALUES (?,?,?,?,?) ON CONFLICT(guild_id,user_id) DO UPDATE SET
                game_nick=excluded.game_nick, static_number=excluded.static_number,
                direction=CASE WHEN excluded.direction='Не указано'
                  THEN family_profiles.direction ELSE excluded.direction END,
                updated_at=CURRENT_TIMESTAMP""",
                (guild_id, user_id, nick, static, direction))

    def upsert_profile_from_application(self, guild_id: int, application_id: int):
        row = self.conn.execute("""SELECT applicant_id,game_nick,static_number,direction
            FROM applications WHERE id=? AND guild_id=? AND status='accepted'""",
            (application_id, guild_id)).fetchone()
        if not row or not row["game_nick"] or not row["static_number"]:
            return False
        self.upsert_profile(guild_id, row["applicant_id"], row["game_nick"],
                            row["static_number"], row["direction"])
        return True

    def profile_stats(self, guild_id: int, user_id: int):
        row = self.conn.execute("""SELECT
            SUM(CASE WHEN s.attended=1 AND e.status='finished' THEN 1 ELSE 0 END) AS attended,
            SUM(CASE WHEN s.attended=1 AND e.status='finished' AND e.result='win' THEN 1 ELSE 0 END) AS wins,
            SUM(CASE WHEN s.attended=1 AND e.status='finished' AND e.result='loss' THEN 1 ELSE 0 END) AS losses,
            SUM(CASE WHEN s.choice='main' AND e.status='scheduled' THEN 1 ELSE 0 END) AS upcoming
            FROM capt_signups s JOIN capt_events e ON e.id=s.event_id
            WHERE e.guild_id=? AND s.user_id=?""", (guild_id,user_id)).fetchone()
        clips = self.conn.execute("""SELECT COUNT(*) FROM capt_clips
            WHERE guild_id=? AND user_id=? AND status='approved'""",
            (guild_id, user_id)).fetchone()[0]
        return {"attended": row["attended"] or 0, "wins": row["wins"] or 0,
                "losses": row["losses"] or 0, "upcoming": row["upcoming"] or 0,
                "approved_clips": clips}

    def family_leaderboard(self, guild_id: int, limit: int = 10):
        return self.conn.execute("""SELECT s.user_id, p.game_nick,
            COUNT(*) AS attended,
            SUM(CASE WHEN e.result='win' THEN 1 ELSE 0 END) AS wins
            FROM capt_signups s JOIN capt_events e ON e.id=s.event_id
            LEFT JOIN family_profiles p ON p.guild_id=e.guild_id AND p.user_id=s.user_id
            WHERE e.guild_id=? AND e.status='finished' AND s.attended=1
            GROUP BY s.user_id ORDER BY attended DESC, wins DESC, s.user_id LIMIT ?""",
            (guild_id, limit)).fetchall()

    # --- Capt events/registration --------------------------------------
    def create_capt(self, guild_id: int, owner_id: int, opponent: str, location: str,
                    format_text: str, timestamp: int, capacity: int) -> int:
        if not 1 <= capacity <= 100:
            raise ValueError("Вместимость 1–100 человек")
        if not all(1 <= len(s.strip()) <= 100 for s in (opponent, location, format_text)):
            raise ValueError("Заполните название противника, место и формат")
        with self.conn:
            cur = self.conn.execute("""INSERT INTO capt_events
                (guild_id,created_by,opponent,location,format_text,scheduled_at,capacity)
                VALUES (?,?,?,?,?,?,?)""",
                (guild_id,owner_id,opponent.strip(),location.strip(),format_text.strip(),timestamp,capacity))
        return cur.lastrowid

    def capt_event(self, event_id: int):
        return self.conn.execute("SELECT * FROM capt_events WHERE id=?", (event_id,)).fetchone()

    def capt_event_by_message(self, guild_id: int, message_id: int):
        return self.conn.execute("SELECT * FROM capt_events WHERE guild_id=? AND message_id=?",
                                 (guild_id,message_id)).fetchone()

    def attach_capt_event(self, event_id: int, channel_id: int, message_id: int):
        with self.conn:
            self.conn.execute("UPDATE capt_events SET channel_id=?, message_id=? WHERE id=?",
                              (channel_id,message_id,event_id))

    def delete_unpublished_capt(self, event_id: int):
        with self.conn:
            self.conn.execute("DELETE FROM capt_events WHERE id=? AND message_id IS NULL", (event_id,))

    def capt_signup(self, event_id: int, user_id: int, choice: str) -> str:
        """Transactionally limit main roster, auto-promote earliest reserve on vacancy."""
        if choice not in ("main", "reserve", "declined"):
            raise ValueError("Неверная запись")
        with self.conn:
            event = self.capt_event(event_id)
            if event is None or event["status"] != "scheduled" or event["scheduled_at"] <= int(time.time()):
                raise ValueError("Запись на этот капт закрыта: он уже начался или завершён")
            previous = self.conn.execute("SELECT choice FROM capt_signups WHERE event_id=? AND user_id=?",
                                         (event_id,user_id)).fetchone()
            selected = choice
            main_count = self.conn.execute("""SELECT COUNT(*) FROM capt_signups
                WHERE event_id=? AND choice='main'""", (event_id,)).fetchone()[0]
            if choice == "main" and main_count >= event["capacity"] and (
                    previous is None or previous["choice"] != "main"):
                selected = "reserve"
            self.conn.execute("""INSERT INTO capt_signups(event_id,user_id,choice,attended)
                VALUES(?,?,?,NULL) ON CONFLICT(event_id,user_id) DO UPDATE SET
                choice=excluded.choice, attended=NULL, updated_at=CURRENT_TIMESTAMP""",
                (event_id,user_id,selected))
            if previous is not None and previous["choice"] == "main" and selected != "main":
                self._promote_reserve(event_id, exclude_user_id=user_id)
        return selected

    def _promote_reserve(self, event_id: int, exclude_user_id: int):
        # A player voluntarily moving from main to reserve must NOT immediately
        # jump back ahead of members who have been on the waiting list longer.
        row = self.conn.execute("""SELECT id FROM capt_signups
            WHERE event_id=? AND choice='reserve' AND user_id != ?
            ORDER BY id LIMIT 1""", (event_id,exclude_user_id)).fetchone()
        if row:
            self.conn.execute("UPDATE capt_signups SET choice='main',updated_at=CURRENT_TIMESTAMP WHERE id=?",
                              (row["id"],))

    def capt_signups(self, event_id: int, choice: str | None = None):
        if choice:
            return self.conn.execute("""SELECT * FROM capt_signups WHERE event_id=? AND choice=?
                ORDER BY id""", (event_id,choice)).fetchall()
        return self.conn.execute("SELECT * FROM capt_signups WHERE event_id=? ORDER BY id",
                                 (event_id,)).fetchall()

    def capt_set_attendance(self, event_id: int, user_ids: list[int], attended: bool) -> int:
        if not user_ids or len(user_ids) > 25:
            raise ValueError("Выберите от 1 до 25 участников")
        placeholders = ",".join("?" for _ in user_ids)
        with self.conn:
            event = self.capt_event(event_id)
            if event is None or event["status"] == "cancelled":
                raise ValueError("Капт не найден или отменён")
            cur = self.conn.execute(f"""UPDATE capt_signups SET attended=?
                WHERE event_id=? AND choice IN ('main','reserve') AND user_id IN ({placeholders})""",
                (int(attended),event_id,*user_ids))
        return cur.rowcount

    def finish_capt(self, event_id: int, result: str, replay_url: str = "", notes: str = ""):
        if result not in ("win", "loss", "draw"):
            raise ValueError("Результат: Победа, Поражение или Ничья")
        with self.conn:
            cur = self.conn.execute("""UPDATE capt_events SET status='finished',result=?,
                replay_url=?, notes=? WHERE id=? AND status='scheduled'""",
                (result,replay_url[:500],notes[:600],event_id))
        return cur.rowcount == 1

    def cancel_capt(self, event_id: int) -> bool:
        with self.conn:
            cur = self.conn.execute("UPDATE capt_events SET status='cancelled' WHERE id=? AND status='scheduled'",
                                    (event_id,))
        return cur.rowcount == 1

    def scheduled_capts(self, guild_id: int, limit: int = 10):
        return self.conn.execute("""SELECT * FROM capt_events WHERE guild_id=? AND status='scheduled'
            ORDER BY scheduled_at ASC LIMIT ?""", (guild_id, limit)).fetchall()

    def due_capt_reminders(self, now: int):
        """Candidate events near a 30/15/5-minute threshold, not old capts.

        A second check in family_rewards.due_reminder_minutes determines the
        exact slot. 500 events per polling cycle is an ample cap for guild use.
        """
        from family_rewards import due_reminder_minutes
        ranges = tuple(value for threshold in (30,15,5)
                       for value in (now+threshold*60-90,now+threshold*60))
        rows = self.conn.execute("""SELECT * FROM capt_events WHERE status='scheduled'
            AND message_id IS NOT NULL AND
            ((scheduled_at BETWEEN ? AND ?) OR
             (scheduled_at BETWEEN ? AND ?) OR
             (scheduled_at BETWEEN ? AND ?))
            ORDER BY scheduled_at ASC LIMIT 500""",ranges).fetchall()
        return [row for row in rows
                if due_reminder_minutes(row["scheduled_at"], now) is not None]

    def claim_capt_reminder(self, event_id: int, remaining_minutes: int) -> bool:
        with self.conn:
            cur = self.conn.execute("""INSERT OR IGNORE INTO capt_reminders(event_id,remaining_minutes)
                VALUES (?,?)""", (event_id,remaining_minutes))
        return cur.rowcount == 1

    def release_capt_reminder(self, event_id: int, remaining_minutes: int):
        """Allow retry when Discord rejects or fails to deliver a reminder."""
        with self.conn:
            self.conn.execute("DELETE FROM capt_reminders WHERE event_id=? AND remaining_minutes=?",
                              (event_id,remaining_minutes))

    # --- Clips ----------------------------------------------------------
    def add_capt_clip(self, guild_id: int, event_id: int, user_id: int,
                      url: str, comment: str) -> int:
        event = self.capt_event(event_id)
        if event is None or event["guild_id"] != guild_id:
            raise ValueError("Капт не найден")
        if not url.startswith(("https://", "http://")) or len(url) > 400:
            raise ValueError("Для отката нужна корректная ссылка https://...")
        with self.conn:
            cur = self.conn.execute("""INSERT INTO capt_clips(guild_id,event_id,user_id,url,comment)
                VALUES (?,?,?,?,?)""", (guild_id,event_id,user_id,url,comment[:500]))
        return cur.lastrowid

    def attach_clip(self, clip_id: int, message_id: int):
        with self.conn:
            self.conn.execute("UPDATE capt_clips SET message_id=? WHERE id=?", (message_id,clip_id))

    def delete_unpublished_clip(self, clip_id: int):
        with self.conn:
            self.conn.execute("DELETE FROM capt_clips WHERE id=? AND message_id IS NULL", (clip_id,))

    def clip_for_message(self, guild_id: int, message_id: int):
        return self.conn.execute("SELECT * FROM capt_clips WHERE guild_id=? AND message_id=?",
                                 (guild_id,message_id)).fetchone()

    def resolve_clip(self, clip_id: int, moderator_id: int, approved: bool) -> bool:
        with self.conn:
            cur = self.conn.execute("""UPDATE capt_clips SET status=?,reviewer_id=?,reviewed_at=CURRENT_TIMESTAMP
                WHERE id=? AND status='pending'""",
                ("approved" if approved else "rejected",moderator_id,clip_id))
        return cur.rowcount == 1

    def close_family_storage(self):
        pass  # Same SQLite connection is closed by Storage.close().
