"""SQLite state for guild settings, application reviews and temporary voice rooms."""
from __future__ import annotations

import sqlite3
from pathlib import Path


class Storage:
    def __init__(self, path: str):
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(path)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA foreign_keys = ON")
        self.conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS guild_config (
                guild_id INTEGER PRIMARY KEY,
                approved_role_id INTEGER NOT NULL,
                moderator_role_id INTEGER NOT NULL,
                application_channel_id INTEGER NOT NULL,
                review_channel_id INTEGER NOT NULL,
                log_channel_id INTEGER NOT NULL,
                control_channel_id INTEGER NOT NULL,
                lobby_channel_id INTEGER NOT NULL,
                voice_category_id INTEGER NOT NULL,
                ping_role_ids TEXT NOT NULL DEFAULT ''
            );
            CREATE TABLE IF NOT EXISTS applications (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                guild_id INTEGER NOT NULL,
                applicant_id INTEGER NOT NULL,
                thread_id INTEGER,
                review_message_id INTEGER,
                anchor_message_id INTEGER,
                status TEXT NOT NULL DEFAULT 'creating',
                reviewer_id INTEGER,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                decided_at TEXT
            );
            CREATE UNIQUE INDEX IF NOT EXISTS idx_one_active_application
                ON applications(guild_id, applicant_id)
                WHERE status IN ('creating', 'pending', 'call_issuing', 'calling', 'processing');
            CREATE UNIQUE INDEX IF NOT EXISTS idx_application_message
                ON applications(review_message_id)
                WHERE review_message_id IS NOT NULL;
            CREATE TABLE IF NOT EXISTS voice_rooms (
                channel_id INTEGER PRIMARY KEY,
                guild_id INTEGER NOT NULL,
                owner_id INTEGER NOT NULL,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                UNIQUE(guild_id, owner_id)
            );
            """
        )
        # Migrate databases created by older versions without deleting channels,
        # application history, moderator settings or existing voice rooms.
        for table, columns in {
            "guild_config": {
                "crime_role_id": "INTEGER",
                "state_role_id": "INTEGER",
                "ping_role_ids": "TEXT NOT NULL DEFAULT ''",
                "call_channel_id": "INTEGER",
                "common_role_id": "INTEGER",
            },
            "applications": {
                "game_nick": "TEXT",
                "static_number": "TEXT",
                "irl_name_age": "TEXT",
                "direction": "TEXT",
                "history": "TEXT",
                "call_channel_id": "INTEGER",
                "call_requested_by": "INTEGER",
                "call_deadline": "INTEGER",
                "call_result": "TEXT",
            },
        }.items():
            present = {row["name"] for row in self.conn.execute(f"PRAGMA table_info({table})")}
            for name, data_type in columns.items():
                if name not in present:
                    self.conn.execute(f"ALTER TABLE {table} ADD COLUMN {name} {data_type}")
        # Existing v3 DBs have an older unique index that omits `calling`.
        # Rebuild it after migration to keep exactly one live application per user.
        self.conn.execute("DROP INDEX IF EXISTS idx_one_active_application")
        self.conn.execute(
            """CREATE UNIQUE INDEX idx_one_active_application ON applications(guild_id, applicant_id)
               WHERE status IN ('creating', 'pending', 'call_issuing', 'calling', 'processing')"""
        )
        self.conn.commit()

    def config(self, guild_id: int):
        return self.conn.execute(
            "SELECT * FROM guild_config WHERE guild_id = ?", (guild_id,)
        ).fetchone()

    def save_config(self, values: dict):
        cols = ", ".join(values)
        placeholders = ", ".join("?" for _ in values)
        with self.conn:
            self.conn.execute(
                f"INSERT INTO guild_config ({cols}) VALUES ({placeholders})",
                tuple(values.values()),
            )

    def set_application_roles(self, guild_id: int, crime_role_id: int, state_role_id: int):
        with self.conn:
            self.conn.execute(
                "UPDATE guild_config SET crime_role_id = ?, state_role_id = ? WHERE guild_id = ?",
                (crime_role_id, state_role_id, guild_id),
            )

    def set_application_ping_roles(self, guild_id: int, role_ids: list[int]):
        """Replace notification role IDs; bounded by Discord message and mention limits."""
        if len(role_ids) > 50 or any(not isinstance(r, int) or r <= 0 for r in role_ids):
            raise ValueError("Разрешено от 0 до 50 корректных ID ролей")
        if len(set(role_ids)) != len(role_ids):
            raise ValueError("Одинаковые роли нельзя указывать дважды")
        with self.conn:
            result = self.conn.execute(
                "UPDATE guild_config SET ping_role_ids = ? WHERE guild_id = ?",
                (",".join(str(role_id) for role_id in role_ids), guild_id),
            )
            if result.rowcount != 1:
                raise ValueError("Сервер ещё не настроен")

    def set_call_channel(self, guild_id: int, channel_id: int):
        with self.conn:
            cursor = self.conn.execute(
                "UPDATE guild_config SET call_channel_id = ? WHERE guild_id = ?",
                (channel_id, guild_id),
            )
            if cursor.rowcount != 1:
                raise ValueError("Сервер ещё не настроен")

    def set_common_role(self, guild_id: int, role_id: int):
        with self.conn:
            cursor = self.conn.execute(
                "UPDATE guild_config SET common_role_id = ? WHERE guild_id = ?",
                (role_id, guild_id),
            )
            if cursor.rowcount != 1:
                raise ValueError("Сервер ещё не настроен")

    def application_ping_role_ids(self, guild_id: int) -> list[int]:
        config = self.config(guild_id)
        if config is None or not config["ping_role_ids"]:
            return []
        return [int(value) for value in config["ping_role_ids"].split(",") if value.isdigit()]

    def active_application(self, guild_id: int, applicant_id: int):
        return self.conn.execute(
            """SELECT * FROM applications WHERE guild_id = ? AND applicant_id = ?
            AND status IN ('creating', 'pending', 'call_issuing', 'calling', 'processing')""",
            (guild_id, applicant_id),
        ).fetchone()

    def reserve_application(self, guild_id: int, applicant_id: int, *,
                            game_nick: str | None = None, static_number: str | None = None,
                            irl_name_age: str | None = None, direction: str | None = None,
                            history: str | None = None) -> int:
        with self.conn:
            cursor = self.conn.execute(
                """INSERT INTO applications(guild_id, applicant_id, game_nick, static_number,
                   irl_name_age, direction, history) VALUES (?, ?, ?, ?, ?, ?, ?)""",
                (guild_id, applicant_id, game_nick, static_number, irl_name_age, direction, history),
            )
        return cursor.lastrowid

    def attach_application(self, app_id: int, thread_id: int, message_id: int,
                           anchor_id: int | None = None):
        with self.conn:
            self.conn.execute(
                """UPDATE applications SET status = 'pending', thread_id = ?,
                review_message_id = ?, anchor_message_id = ? WHERE id = ?""",
                (thread_id, message_id, anchor_id, app_id),
            )

    def abort_application(self, app_id: int):
        with self.conn:
            self.conn.execute(
                "DELETE FROM applications WHERE id = ? AND status = 'creating'", (app_id,)
            )

    def application_by_id(self, app_id: int):
        return self.conn.execute(
            "SELECT * FROM applications WHERE id = ?", (app_id,)
        ).fetchone()

    def application_for_message(self, message_id: int):
        return self.conn.execute(
            "SELECT * FROM applications WHERE review_message_id = ?", (message_id,)
        ).fetchone()

    def start_call(self, app_id: int, channel_id: int, requester_id: int, deadline: int) -> bool:
        """Atomically mark a pending application as awaiting the voice interview."""
        with self.conn:
            cursor = self.conn.execute(
                """UPDATE applications SET status = 'call_issuing', call_channel_id = ?,
                   call_requested_by = ?, call_deadline = ?, call_result = 'waiting'
                   WHERE id = ? AND status = 'pending'""",
                (channel_id, requester_id, deadline, app_id),
            )
        return cursor.rowcount == 1

    def activate_call(self, app_id: int) -> bool:
        """Start the real timeout only AFTER the call message is sent."""
        with self.conn:
            cursor = self.conn.execute(
                "UPDATE applications SET status = 'calling' WHERE id = ? AND status = 'call_issuing'",
                (app_id,),
            )
        return cursor.rowcount == 1

    def cancel_call(self, app_id: int):
        """Only an unfinished invitation can be cancelled."""
        with self.conn:
            self.conn.execute(
                """UPDATE applications SET status = 'pending', call_deadline = NULL,
                   call_result = 'failed' WHERE id = ? AND status = 'call_issuing'""", (app_id,),
            )

    def mark_call_joined(self, guild_id: int, user_id: int, channel_id: int, now: int):
        """Return application only if candidate joined the correct room on time."""
        with self.conn:
            cursor = self.conn.execute(
                """UPDATE applications SET status = 'pending', call_result = 'joined',
                   call_deadline = NULL WHERE guild_id = ? AND applicant_id = ?
                   AND status = 'calling' AND call_channel_id = ?
                   AND call_deadline >= ?""",
                (guild_id, user_id, channel_id, now),
            )
        if cursor.rowcount:
            return self.active_application(guild_id, user_id)
        return None

    def expired_calls(self, now: int):
        return self.conn.execute(
            """SELECT * FROM applications WHERE status = 'calling'
               AND call_deadline IS NOT NULL AND call_deadline <= ?
               ORDER BY call_deadline LIMIT 30""", (now,),
        ).fetchall()

    def claim_application(self, app_id: int) -> bool:
        with self.conn:
            cursor = self.conn.execute(
                """UPDATE applications SET status = 'processing'
                   WHERE id = ? AND status IN ('pending', 'calling')""", (app_id,),
            )
        return cursor.rowcount == 1

    def revert_application(self, app_id: int):
        with self.conn:
            self.conn.execute(
                """UPDATE applications SET status = CASE
                   WHEN call_result = 'waiting' AND call_deadline IS NOT NULL
                   THEN 'calling' ELSE 'pending' END
                   WHERE id = ? AND status = 'processing'""", (app_id,),
            )

    def mark_call_timeout_failed(self, app_id: int):
        """Avoid retrying an impossible kick forever; moderators may decide."""
        with self.conn:
            self.conn.execute(
                """UPDATE applications SET status = 'pending', call_result = 'timeout_failed',
                   call_deadline = NULL WHERE id = ? AND status = 'processing'""", (app_id,),
            )

    def mark_call_missed(self, app_id: int):
        with self.conn:
            self.conn.execute(
                "UPDATE applications SET call_result = 'missed' WHERE id = ? AND status = 'processing'",
                (app_id,),
            )

    def finish_application(self, app_id: int, decision: str, reviewer_id: int):
        assert decision in ('accepted', 'rejected')
        with self.conn:
            self.conn.execute(
                """UPDATE applications SET status = ?, reviewer_id = ?,
                decided_at = CURRENT_TIMESTAMP WHERE id = ? AND status = 'processing'""",
                (decision, reviewer_id, app_id),
            )

    def reset_application(self, guild_id: int, applicant_id: int):
        app = self.active_application(guild_id, applicant_id)
        if app:
            with self.conn:
                self.conn.execute("DELETE FROM applications WHERE id = ?", (app["id"],))
        return app

    def recover_incomplete_operations(self):
        """On restart, a previous process can no longer finish its actions.

        Repeating role addition is safe and rejecting an already-departed member
        is treated as successful. Unfinished modal creations are released.
        """
        with self.conn:
            self.conn.execute(
                """UPDATE applications SET status = CASE
                   WHEN call_result = 'waiting' AND call_deadline IS NOT NULL
                   THEN 'calling' ELSE 'pending' END WHERE status = 'processing'"""
            )
            self.conn.execute("DELETE FROM applications WHERE status = 'creating'")
            self.conn.execute(
                """UPDATE applications SET status = 'pending', call_deadline = NULL,
                   call_result = 'failed' WHERE status = 'call_issuing'"""
            )

    def reset_missing_thread(self, thread_id: int):
        with self.conn:
            self.conn.execute(
                """DELETE FROM applications WHERE thread_id = ?
                AND status IN ('pending', 'creating', 'call_issuing', 'calling')""",
                (thread_id,),
            )

    def all_rooms(self):
        return self.conn.execute("SELECT * FROM voice_rooms").fetchall()

    def room(self, channel_id: int):
        return self.conn.execute(
            "SELECT * FROM voice_rooms WHERE channel_id = ?", (channel_id,)
        ).fetchone()

    def owned_room(self, guild_id: int, user_id: int):
        return self.conn.execute(
            "SELECT * FROM voice_rooms WHERE guild_id = ? AND owner_id = ?",
            (guild_id, user_id),
        ).fetchone()

    def create_room(self, channel_id: int, guild_id: int, owner_id: int):
        with self.conn:
            self.conn.execute(
                "INSERT INTO voice_rooms(channel_id, guild_id, owner_id) VALUES (?, ?, ?)",
                (channel_id, guild_id, owner_id),
            )

    def transfer_room(self, channel_id: int, owner_id: int):
        with self.conn:
            self.conn.execute(
                "UPDATE voice_rooms SET owner_id = ? WHERE channel_id = ?",
                (owner_id, channel_id),
            )

    def remove_room(self, channel_id: int):
        with self.conn:
            self.conn.execute("DELETE FROM voice_rooms WHERE channel_id = ?", (channel_id,))

    def close(self):
        self.conn.close()
