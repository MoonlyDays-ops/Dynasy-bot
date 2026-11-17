"""DYNASTY FAMILY v8 — Majestic RP Miami capts, profiles, achievements.

No game API or automated capture detection: results, participation and clips are
based on users' submissions and confirmation by the capt leader/administrator.
"""
from __future__ import annotations

import asyncio
import logging
import re
import time
from datetime import date, datetime, time as dt_time, timedelta
from urllib.parse import urlparse
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import discord
from discord import app_commands

from family_rewards import (EMBED_GREEN, achievement_progress, earned_achievements,
                            progress_bar, due_reminder_minutes)

LOG = logging.getLogger("dynasty.capts")


def clean(value: str, limit: int = 1000) -> str:
    return discord.utils.escape_mentions(discord.utils.escape_markdown(str(value).strip()))[:limit] or "—"


async def reply(interaction: discord.Interaction, message: str, *, embed=None, view=None):
    opts = dict(ephemeral=True, allowed_mentions=discord.AllowedMentions.none())
    if embed is not None:
        opts["embed"] = embed
    if view is not None:
        opts["view"] = view
    if interaction.response.is_done():
        await interaction.followup.send(message, **opts)
    else:
        await interaction.response.send_message(message, **opts)


def can_manage_capts(bot, member: discord.Member) -> bool:
    if member.guild_permissions.administrator:
        return True
    conf = bot.db.capt_settings(member.guild.id)
    role_id = conf["leader_role_id"]
    return bool(role_id and any(r.id == role_id for r in member.roles))


def is_family_member(bot, member: discord.Member) -> bool:
    if member.guild_permissions.administrator:
        return True
    conf = bot.db.config(member.guild.id)
    if conf is None:
        return False
    permitted = {conf["common_role_id"], conf["crime_role_id"], conf["state_role_id"]}
    permitted.discard(None)
    return bool(permitted.intersection(r.id for r in member.roles))


def direction_from_roles(bot, member: discord.Member) -> str | None:
    conf = bot.db.config(member.guild.id)
    if not conf:
        return None
    has = {r.id for r in member.roles}
    if conf["crime_role_id"] in has:
        return "Crime"
    if conf["state_role_id"] in has:
        return "State"
    return None


def checked_url(url: str) -> bool:
    if not url:
        return True
    try:
        parts = urlparse(url)
        return (parts.scheme == "https" and bool(parts.netloc)
                and len(url) <= 500 and not any(c.isspace() for c in url))
    except ValueError:
        return False


def achievements_embed(member: discord.Member, stats: dict) -> discord.Embed:
    unlocked = earned_achievements(stats)
    embed = discord.Embed(
        title="🏅 DYNASTY • Достижения",
        description=(f"**Игрок:** {member.mention}\n"
                     f"**Открыто:** {len(unlocked)}/{len(achievement_progress(stats))}\n"
                     "Награды рассчитываются по подтверждённой явке, результатам и откатам."),
        color=EMBED_GREEN,
    )
    for achievement, current in achievement_progress(stats):
        achieved = current >= achievement.threshold
        progress = f"{current}/{achievement.threshold}"
        embed.add_field(
            name=f"{'✅' if achieved else '🔒'} {achievement.icon} {achievement.title}",
            value=(f"{achievement.description}\n"
                   f"{progress_bar(current, achievement.threshold, width=8)}  **{progress}**"),
            inline=False,
        )
    embed.set_footer(text="Достижения не выдают Discord-роли и не используют данные игры напрямую")
    return embed


def profile_embed(bot, member: discord.Member) -> discord.Embed:
    p = bot.db.profile(member.guild.id,member.id)
    stats = bot.db.profile_stats(member.guild.id,member.id)
    approved_dir = direction_from_roles(bot,member)
    embed = discord.Embed(title="👑 DYNASTY • Личный кабинет", color=EMBED_GREEN)
    embed.description = (f"**Участник:** {member.mention}\n"
                         f"**Ник на Majestic RP Miami:** {clean(p['game_nick']) if p else 'Не указан'}\n"
                         f"**Статик #:** {p['static_number'] if p else 'Не указан'}\n"
                         f"**Направление:** {approved_dir or (p['direction'] if p else 'Не указано')}\n"
                         f"**Discord-роль:** {clean(member.top_role.name,80)}")
    embed.add_field(name="🎯 Посещено каптов", value=str(stats["attended"]), inline=True)
    embed.add_field(name="🏆 Победы", value=str(stats["wins"]), inline=True)
    embed.add_field(name="❌ Поражения", value=str(stats["losses"]), inline=True)
    embed.add_field(name="📅 Будущие капты в основном", value=str(stats["upcoming"]), inline=True)
    embed.add_field(name="🎥 Принято откатов", value=str(stats["approved_clips"]), inline=True)
    earned = earned_achievements(stats)
    preview = "\n".join(f"{a.icon} **{a.title}**" for a in earned[-4:]) if earned else "Пока нет достижений"
    embed.add_field(name=f"🎖️ Достижения ({len(earned)}/{len(achievement_progress(stats))})",
                    value=preview + "\nНажмите «Достижения», чтобы посмотреть все награды и прогресс.",
                    inline=False)
    embed.set_footer(text="Учитывается только явка, подтверждённая руководством • контракты не учитываются")
    return embed


def event_embed(bot, event) -> discord.Embed:
    mains = bot.db.capt_signups(event["id"],"main")
    reserve = bot.db.capt_signups(event["id"],"reserve")
    status = {"scheduled":"🟢 Запись открыта", "finished":"✅ Завершён", "cancelled":"⛔ Отменён"}[event["status"]]
    if event["result"]:
        status += " • " + {"win":"Победа", "loss":"Поражение", "draw":"Ничья"}[event["result"]]
    em = discord.Embed(title=f"⚔️ DYNASTY • Капт №{event['id']}",
                       description=(f"**Противник:** {clean(event['opponent'])}\n"
                                    f"**Место:** {clean(event['location'])}\n"
                                    f"**Формат:** {clean(event['format_text'])}\n"
                                    f"**Время:** <t:{event['scheduled_at']}:F> (<t:{event['scheduled_at']}:R>)\n"
                                    f"**Капт-лидер:** <@{event['created_by']}>\n"
                                    f"**Статус:** {status}"),
                       color=EMBED_GREEN if event["status"] in ("scheduled", "finished") else discord.Color.greyple())
    main_list = ", ".join(f"<@{r['user_id']}>" for r in mains[:16]) or "Пока никого"
    reserve_list = ", ".join(f"<@{r['user_id']}>" for r in reserve[:12]) or "Пусто"
    em.add_field(name="🟩 Заполнение основного состава",
                 value=(f"{progress_bar(len(mains),event['capacity'])} "
                        f"**{len(mains)}/{event['capacity']}**"),inline=False)
    em.add_field(name=f"Основной состав ({len(mains)}/{event['capacity']})",
                 value=main_list[:1024], inline=False)
    em.add_field(name=f"Резерв ({len(reserve)})",value=reserve_list[:1024],inline=False)
    confirmed = sum(r["attended"] == 1 for r in bot.db.capt_signups(event["id"]))
    if event["status"] == "finished":
        em.add_field(name="Подтверждённая явка", value=f"{confirmed} участников", inline=True)
        if event["replay_url"] and checked_url(event["replay_url"]):
            em.add_field(name="Запись", value=event["replay_url"][:1000],inline=False)
        if event["notes"]:
            em.add_field(name="Комментарий", value=clean(event["notes"]),inline=False)
    em.set_footer(text="Кнопки регистрации • Результат и явка подтверждаются капитаном")
    return em


async def capt_log(bot, guild: discord.Guild, title: str, description: str):
    conf = bot.db.capt_settings(guild.id)
    channel = guild.get_channel(conf["log_channel_id"]) if conf["log_channel_id"] else None
    if not isinstance(channel,discord.TextChannel):
        return
    em = discord.Embed(title=f"📒 DYNASTY • {title}", description=description[:3900],
                       color=discord.Color.blurple(),timestamp=discord.utils.utcnow())
    try:
        await channel.send(embed=em, allowed_mentions=discord.AllowedMentions.none())
    except discord.HTTPException:
        LOG.exception("Could not write capt log guild=%s",guild.id)


async def redraw_event(bot, guild: discord.Guild, event_id: int):
    event = bot.db.capt_event(event_id)
    if not event or not event["message_id"]:
        return
    channel = guild.get_channel(event["channel_id"])
    if not isinstance(channel,discord.TextChannel):
        return
    try:
        message = await channel.fetch_message(event["message_id"])
        await message.edit(embed=event_embed(bot,event),view=CaptEventView(bot),
                           allowed_mentions=discord.AllowedMentions.none())
    except discord.HTTPException:
        LOG.warning("Capt message %s could not be refreshed",event["message_id"])


class ProfileEditModal(discord.ui.Modal, title="DYNASTY • Редактирование профиля"):
    nickname = discord.ui.TextInput(label="Ник в игре",max_length=60,min_length=2)
    static_num = discord.ui.TextInput(label="Статик # — только цифры",max_length=20,min_length=1)

    def __init__(self, bot, member: discord.Member):
        super().__init__(timeout=300)
        self.bot=bot
        self.member_id=member.id
        p=bot.db.profile(member.guild.id,member.id)
        if p:
            self.nickname.default=p["game_nick"]
            self.static_num.default=p["static_number"]

    async def on_submit(self,i: discord.Interaction):
        if not isinstance(i.user,discord.Member) or i.user.id != self.member_id:
            return await reply(i,"⛔ Эта форма не для вас.")
        try:
            self.bot.db.upsert_profile(i.guild.id,i.user.id,str(self.nickname.value),
                                       str(self.static_num.value),direction_from_roles(self.bot,i.user))
        except ValueError as exc:
            return await reply(i,f"❌ {exc}")
        await reply(i,"✅ Игровой ник и статик сохранены. Нажмите «Обновить» в кабинете, чтобы увидеть изменения.")


class ProfileView(discord.ui.View):
    def __init__(self,bot,user_id):
        super().__init__(timeout=600)
        self.bot=bot; self.user_id=user_id

    async def interaction_check(self,i):
        if i.guild is None or i.user.id != self.user_id or not isinstance(i.user,discord.Member):
            await reply(i,"Это чужой личный кабинет.")
            return False
        return True

    @discord.ui.button(label="Изменить ник / статик",style=discord.ButtonStyle.primary)
    async def change(self,i,_):
        await i.response.send_modal(ProfileEditModal(self.bot,i.user))

    @discord.ui.button(label="Мои капты",style=discord.ButtonStyle.secondary)
    async def capts(self,i,_):
        rows=self.bot.db.conn.execute("""SELECT e.*,s.choice,s.attended FROM capt_signups s
            JOIN capt_events e ON e.id=s.event_id WHERE e.guild_id=? AND s.user_id=?
            ORDER BY e.scheduled_at DESC LIMIT 10""",(i.guild.id,i.user.id)).fetchall()
        if not rows:
            return await reply(i,"Вы ещё не записывались на капты.")
        lines=[]
        for r in rows:
            status="был" if r["attended"]==1 else "не был" if r["attended"]==0 else "явка не отмечена"
            lines.append(f"**#{r['id']}** • <t:{r['scheduled_at']}:f> • {clean(r['opponent'],50)} • {r['choice']} • {status}")
        await reply(i,"📅 **Ваши последние капты**\n"+"\n".join(lines))

    @discord.ui.button(label="🏅 Достижения",style=discord.ButtonStyle.secondary)
    async def achievements(self,i,_):
        stats=self.bot.db.profile_stats(i.guild.id,i.user.id)
        embed=achievements_embed(i.user,stats)
        await reply(i,"Ваши достижения (доступны только вам):",embed=embed)

    @discord.ui.button(label="Обновить",style=discord.ButtonStyle.success)
    async def refresh(self,i,_):
        await i.response.edit_message(embed=profile_embed(self.bot,i.user),view=self)


# The Discord API exposes dropdowns, but not a native calendar/date picker.
# A 25-day paginated date select avoids manual date entry entirely.
CAPT_DATE_PAGE_SIZE = 25
CAPT_DATE_MAX_DAYS = 365
RUS_WEEKDAYS = ("Пн", "Вт", "Ср", "Чт", "Пт", "Сб", "Вс")
CAPT_RESULT_NAMES = {"win": "Победа", "loss": "Поражение", "draw": "Ничья"}


def capt_schedule_timestamp(day: date, hour: int, minute: int, zone: ZoneInfo, *, now: int | None = None) -> int:
    """Validate the chosen local time, including DST gaps, and return Unix seconds."""
    if not 0 <= hour <= 23 or not 0 <= minute <= 59:
        raise ValueError("Некорректное время.")
    local = datetime.combine(day, dt_time(hour=hour, minute=minute))
    stamp = int(local.replace(tzinfo=zone).timestamp())
    if datetime.fromtimestamp(stamp, zone).replace(tzinfo=None) != local:
        raise ValueError("В этот день выбранное время отсутствует из-за перевода часов. Выберите другое.")
    if stamp <= (int(time.time()) if now is None else now) + 120:
        raise ValueError("Выберите время минимум через две минуты от текущего.")
    return stamp


async def publish_new_capt(bot, i: discord.Interaction, details: dict, timestamp: int):
    """Reuse the existing DB and announcement semantics after calendar selection."""
    conf = bot.db.capt_settings(i.guild.id)
    channel = i.guild.get_channel(conf["channel_id"]) if conf["channel_id"] else None
    if not isinstance(channel, discord.TextChannel):
        raise ValueError("Сначала создайте каналы: /menu → Капт-состав → Создать каналы.")
    event_id = bot.db.create_capt(
        i.guild.id, i.user.id, details["opponent"], details["location"],
        details["format_text"], timestamp, details["capacity"]
    )
    try:
        event = bot.db.capt_event(event_id)
        ping_role = i.guild.get_role(conf["announce_role_id"]) if conf["announce_role_id"] else None
        content = (f"📣 **Новый капт DYNASTY!** {ping_role.mention}" if ping_role
                   else "📣 **Новый капт DYNASTY!**")
        msg = await channel.send(
            content=content, embed=event_embed(bot, event), view=CaptEventView(bot),
            allowed_mentions=discord.AllowedMentions(
                everyone=False, users=False, roles=[ping_role] if ping_role else False,
                replied_user=False,
            ),
        )
        bot.db.attach_capt_event(event_id, channel.id, msg.id)
    except discord.HTTPException:
        bot.db.delete_unpublished_capt(event_id)
        LOG.exception("Failed creating capt event")
        raise
    await capt_log(bot, i.guild, "Создан капт",
                   f"Капт **#{event_id}** • {clean(event['opponent'])} • <t:{timestamp}:F> • <@{i.user.id}>")
    return event_id, channel


class CaptCreateModal(discord.ui.Modal, title="DYNASTY • Создать капт"):
    opponent = discord.ui.TextInput(label="Противник", max_length=100)
    location = discord.ui.TextInput(label="Место / квадрат", max_length=100)
    format_text = discord.ui.TextInput(label="Формат (пример: 15x15)", max_length=100)
    capacity = discord.ui.TextInput(label="Мест в основном составе (1–100)", placeholder="15", max_length=3)

    def __init__(self, bot):
        super().__init__(timeout=300)
        self.bot = bot

    async def on_submit(self, i: discord.Interaction):
        if (i.guild is None or not isinstance(i.user, discord.Member)
                or not can_manage_capts(self.bot, i.user)):
            return await reply(i, "Только капитан или администратор может создавать капты.")
        try:
            capacity = int(str(self.capacity.value).strip())
            if not 1 <= capacity <= 100:
                raise ValueError("Вместимость должна быть от 1 до 100.")
            details = dict(opponent=str(self.opponent.value).strip(),
                           location=str(self.location.value).strip(),
                           format_text=str(self.format_text.value).strip(),
                           capacity=capacity)
            if not all(details.values()):
                raise ValueError("Заполните все поля.")
            picker = CaptScheduleView(self.bot, i.user.id, i.guild.id, details)
        except (ValueError, ZoneInfoNotFoundError) as exc:
            return await reply(i, f"❌ {exc}")
        await i.response.send_message(
            picker.summary(), view=picker, ephemeral=True,
            allowed_mentions=discord.AllowedMentions.none(),
        )


class CaptDaySelect(discord.ui.Select):
    def __init__(self, picker: "CaptScheduleView"):
        self.picker = picker
        first = picker.today + timedelta(days=picker.page_offset)
        last = picker.today + timedelta(days=CAPT_DATE_MAX_DAYS)
        options = []
        for offset in range(CAPT_DATE_PAGE_SIZE):
            day = first + timedelta(days=offset)
            if day > last:
                break
            options.append(discord.SelectOption(
                label=f"{day:%d.%m.%Y} • {RUS_WEEKDAYS[day.weekday()]}",
                value=day.isoformat(), default=day == picker.selected_date,
            ))
        super().__init__(placeholder="📅 Выберите дату капта", options=options, row=0)

    async def callback(self, i: discord.Interaction):
        self.picker.selected_date = date.fromisoformat(self.values[0])
        self.picker.rebuild()
        await i.response.edit_message(content=self.picker.summary(), view=self.picker)


class CaptHourSelect(discord.ui.Select):
    def __init__(self, picker: "CaptScheduleView"):
        self.picker = picker
        options = [discord.SelectOption(label=f"{h:02d}:00", value=str(h),
                                        default=h == picker.hour) for h in range(24)]
        super().__init__(placeholder="🕒 Часы", options=options, row=1)

    async def callback(self, i: discord.Interaction):
        self.picker.hour = int(self.values[0])
        self.picker.rebuild()
        await i.response.edit_message(content=self.picker.summary(), view=self.picker)


class CaptMinuteSelect(discord.ui.Select):
    def __init__(self, picker: "CaptScheduleView"):
        self.picker = picker
        options = [discord.SelectOption(label=f"{m:02d} мин", value=str(m),
                                        default=m == picker.minute) for m in range(0, 60, 5)]
        super().__init__(placeholder="⏱️ Минуты (шаг 5 минут)", options=options, row=2)

    async def callback(self, i: discord.Interaction):
        self.picker.minute = int(self.values[0])
        self.picker.rebuild()
        await i.response.edit_message(content=self.picker.summary(), view=self.picker)


class CaptScheduleNav(discord.ui.Button):
    def __init__(self, picker: "CaptScheduleView", direction: int):
        self.picker = picker
        self.direction = direction
        super().__init__(label="← Предыдущие даты" if direction < 0 else "Следующие даты →",
                         style=discord.ButtonStyle.secondary, row=3,
                         disabled=(picker.page_offset == 0 if direction < 0 else
                                   picker.page_offset + CAPT_DATE_PAGE_SIZE > CAPT_DATE_MAX_DAYS))

    async def callback(self, i: discord.Interaction):
        self.picker.page_offset = max(0, min(CAPT_DATE_MAX_DAYS,
                                     self.picker.page_offset + self.direction * CAPT_DATE_PAGE_SIZE))
        # Select the first day of the visible page to avoid invisible selections.
        self.picker.selected_date = self.picker.today + timedelta(days=self.picker.page_offset)
        self.picker.rebuild()
        await i.response.edit_message(content=self.picker.summary(), view=self.picker)


class CaptScheduleConfirm(discord.ui.Button):
    def __init__(self, picker: "CaptScheduleView"):
        self.picker = picker
        super().__init__(label="✅ Опубликовать капт", style=discord.ButtonStyle.success, row=3)

    async def callback(self, i: discord.Interaction):
        picker = self.picker
        if picker.saving:
            return await reply(i, "Публикация уже выполняется.")
        try:
            timestamp = capt_schedule_timestamp(picker.selected_date, picker.hour,
                                                picker.minute, picker.zone)
        except ValueError as exc:
            return await reply(i, f"❌ {exc}")
        picker.saving = True
        await i.response.defer()  # acknowledges button without displaying a public message
        try:
            event_id, channel = await publish_new_capt(picker.bot, i, picker.details, timestamp)
        except (ValueError, discord.HTTPException) as exc:
            picker.saving = False
            return await i.edit_original_response(content=f"❌ Ошибка публикации: {exc}", view=picker)
        await i.edit_original_response(
            content=f"✅ Капт **#{event_id}** опубликован в {channel.mention} на <t:{timestamp}:F>.",
            view=None,
        )


class CaptScheduleView(discord.ui.View):
    """User-scoped date/time picker; all responses remain ephemeral."""
    def __init__(self, bot, owner_id: int, guild_id: int, details: dict):
        super().__init__(timeout=600)
        self.bot = bot
        self.owner_id = owner_id
        self.guild_id = guild_id
        self.details = details
        conf = bot.db.capt_settings(guild_id)
        self.zone = ZoneInfo(conf["timezone"])
        now = datetime.now(self.zone)
        suggested = now + timedelta(hours=1)
        # Round proposed time upward to a five-minute boundary.
        rounded = suggested + timedelta(minutes=(5 - suggested.minute % 5) % 5)
        self.today = now.date()
        self.selected_date = rounded.date()
        self.hour = rounded.hour
        self.minute = rounded.minute
        self.page_offset = 0
        self.saving = False
        self.rebuild()

    def rebuild(self):
        self.clear_items()
        self.add_item(CaptDaySelect(self))
        self.add_item(CaptHourSelect(self))
        self.add_item(CaptMinuteSelect(self))
        self.add_item(CaptScheduleNav(self, -1))
        self.add_item(CaptScheduleNav(self, 1))
        self.add_item(CaptScheduleConfirm(self))

    def summary(self) -> str:
        return ("📅 **DYNASTY • Выберите дату и время капта**\n"
                f"Противник: **{clean(self.details['opponent'])}** • "
                f"Мест: **{self.details['capacity']}**\n"
                f"Дата: **{self.selected_date:%d.%m.%Y}** • "
                f"Время: **{self.hour:02d}:{self.minute:02d}** "
                f"(`{self.zone.key}`)\n"
                "Выберите день, час и минуты из списков, затем нажмите «Опубликовать капт». "
                "Для дальних дат используйте кнопки навигации.")

    async def interaction_check(self, i: discord.Interaction) -> bool:
        if (i.guild is None or i.guild.id != self.guild_id or i.user.id != self.owner_id
                or not isinstance(i.user, discord.Member) or not can_manage_capts(self.bot, i.user)):
            await reply(i, "⛔ Это меню доступно только капитану, который его открыл.")
            return False
        return True


class CaptFinishModal(discord.ui.Modal):
    replay = discord.ui.TextInput(label="Ссылка на запись (необязательно)", required=False, max_length=500)
    comment = discord.ui.TextInput(label="Комментарий (необязательно)", required=False, max_length=600,
                                   style=discord.TextStyle.paragraph)

    def __init__(self, bot, event_id: int, result_code: str):
        if result_code not in CAPT_RESULT_NAMES:
            raise ValueError("Неизвестный результат капта")
        super().__init__(title=f"DYNASTY • {CAPT_RESULT_NAMES[result_code]}", timeout=300)
        self.bot = bot
        self.event_id = event_id
        self.result_code = result_code

    async def on_submit(self, i: discord.Interaction):
        if (i.guild is None or not isinstance(i.user, discord.Member)
                or not can_manage_capts(self.bot, i.user)):
            return await reply(i, "Недостаточно прав для завершения капта.")
        link = str(self.replay.value).strip()
        if not checked_url(link):
            return await reply(i, "❌ Ссылка должна начинаться с https://")
        event = self.bot.db.capt_event(self.event_id)
        if not event or event["guild_id"] != i.guild.id or not self.bot.db.finish_capt(
                self.event_id, self.result_code, link, str(self.comment.value)):
            return await reply(i, "Капт уже завершён или недоступен.")
        await i.response.defer(ephemeral=True, thinking=True)
        await redraw_event(self.bot, i.guild, self.event_id)
        count = sum(s["attended"] == 1 for s in self.bot.db.capt_signups(self.event_id))
        await capt_log(self.bot, i.guild, "Капт завершён",
                       f"#{self.event_id} • результат **{CAPT_RESULT_NAMES[self.result_code]}** "
                       f"• явка: **{count}** • лидер <@{i.user.id}>"
                       + (f"\nЗапись: {link}" if link else ""))
        await reply(i, "✅ Результат записан. Статистика профилей учитывает только участников, которым капитан подтвердил явку.")


class CaptResultSelect(discord.ui.Select):
    def __init__(self):
        super().__init__(placeholder="Выберите результат капта", min_values=1, max_values=1,
                         options=[discord.SelectOption(label="Победа", value="win", emoji="🏆"),
                                  discord.SelectOption(label="Поражение", value="loss", emoji="❌"),
                                  discord.SelectOption(label="Ничья", value="draw", emoji="🤝")])

    async def callback(self, i: discord.Interaction):
        view = self.view
        event = view.bot.db.capt_event(view.event_id)
        if (not event or event["guild_id"] != i.guild.id or event["status"] != "scheduled"):
            return await reply(i, "Капт уже завершён или недоступен.")
        await i.response.send_modal(CaptFinishModal(view.bot, view.event_id, self.values[0]))


class CaptFinishChoiceView(discord.ui.View):
    def __init__(self, bot, event_id: int, owner_id: int):
        super().__init__(timeout=300)
        self.bot, self.event_id, self.owner_id = bot, event_id, owner_id
        self.add_item(CaptResultSelect())

    async def interaction_check(self, i: discord.Interaction):
        if (i.guild is None or i.user.id != self.owner_id
                or not isinstance(i.user, discord.Member) or not can_manage_capts(self.bot, i.user)):
            await reply(i, "⛔ Только капитан, открывший список, может выбрать результат.")
            return False
        return True

class CaptClipModal(discord.ui.Modal,title="DYNASTY • Сдать откат"):
    url=discord.ui.TextInput(label="Ссылка на запись/видео (https://)",max_length=400)
    note=discord.ui.TextInput(label="Короткое описание",required=False,max_length=500,
                               style=discord.TextStyle.paragraph)

    def __init__(self,bot,event_id):
        super().__init__(timeout=300); self.bot=bot; self.event_id=event_id

    async def on_submit(self,i:discord.Interaction):
        if not isinstance(i.user,discord.Member) or not is_family_member(self.bot,i.user):
            return await reply(i,"Эта функция доступна участникам семьи.")
        event=self.bot.db.capt_event(self.event_id)
        if not event or event["guild_id"] != i.guild.id or event["status"]=="cancelled":
            return await reply(i,"Этот капт недоступен.")
        registrations=self.bot.db.capt_signups(self.event_id)
        if not any(r["user_id"]==i.user.id and r["choice"] in ("main","reserve") for r in registrations):
            return await reply(i,"Сначала запишитесь в состав или резерв этого капта.")
        link=str(self.url.value).strip()
        if not link or not checked_url(link):
            return await reply(i,"Введите ссылку вида https://... на вашу запись.")
        conf=self.bot.db.capt_settings(i.guild.id)
        channel=i.guild.get_channel(conf["clips_channel_id"]) if conf["clips_channel_id"] else None
        if not isinstance(channel,discord.TextChannel):
            return await reply(i,"Канал откатов ещё не настроен. Обратитесь к администрации.")
        await i.response.defer(ephemeral=True,thinking=True)
        clip_id=self.bot.db.add_capt_clip(i.guild.id,self.event_id,i.user.id,link,str(self.note.value))
        em=discord.Embed(title=f"🎥 Откат #{clip_id} • Капт #{self.event_id}",
                         description=(f"**Автор:** {i.user.mention}\n**Ссылка:** {link}\n"
                                      f"**Описание:** {clean(self.note.value,500)}"),color=discord.Color.blurple())
        try:
            msg=await channel.send(embed=em,view=ClipReviewView(self.bot),
                                   allowed_mentions=discord.AllowedMentions.none())
            self.bot.db.attach_clip(clip_id,msg.id)
        except discord.HTTPException:
            self.bot.db.delete_unpublished_clip(clip_id)
            LOG.exception("Cannot publish clip")
            return await reply(i,"Не удалось передать откат на проверку. Проверьте права бота.")
        await reply(i,"✅ Откат отправлен на проверку. О результате сообщим в ЛС, если Discord разрешит.")


class CaptAttendanceUserSelect(discord.ui.UserSelect):
    def __init__(self):
        super().__init__(placeholder="Выберите участников из списка сервера",min_values=1,max_values=25,row=0)

    async def callback(self,i:discord.Interaction):
        parent=self.view
        parent.selected=[user.id for user in self.values]
        await i.response.edit_message(content=f"Выбрано участников: **{len(parent.selected)}**. Отметьте их как пришедших или отсутствующих.",view=parent)


class CaptAttendanceView(discord.ui.View):
    def __init__(self,bot,event_id,owner_id):
        super().__init__(timeout=600)
        self.bot=bot; self.event_id=event_id; self.owner_id=owner_id; self.selected=[]
        self.add_item(CaptAttendanceUserSelect())

    async def interaction_check(self,i):
        if (i.guild is None or i.user.id!=self.owner_id or
            not isinstance(i.user,discord.Member) or not can_manage_capts(self.bot,i.user)):
            await reply(i,"Управлять явкой может только капитан, открывший меню.")
            return False
        return True

    async def mark(self,i,attended):
        if not self.selected:
            return await reply(i,"Сначала выберите пользователей в выпадающем списке.")
        try:
            changed=self.bot.db.capt_set_attendance(self.event_id,self.selected,attended)
        except ValueError as exc:
            return await reply(i,f"❌ {exc}")
        await i.response.defer(ephemeral=True,thinking=True)
        await redraw_event(self.bot,i.guild,self.event_id)
        await capt_log(self.bot,i.guild,"Учёт явки",
                       f"Капт #{self.event_id} • <@{i.user.id}> • {'пришли' if attended else 'не пришли'}: "
                       +", ".join(f"<@{u}>" for u in self.selected[:25]))
        await reply(i,f"✅ Обновлено записей: **{changed}**. Не записанные на капт участники не учитываются.")

    @discord.ui.button(label="✅ Пришёл",style=discord.ButtonStyle.success,row=1)
    async def present(self,i,_): await self.mark(i,True)

    @discord.ui.button(label="❌ Не пришёл",style=discord.ButtonStyle.danger,row=1)
    async def absent(self,i,_): await self.mark(i,False)


class CaptCancelView(discord.ui.View):
    def __init__(self,bot,event_id,owner_id):
        super().__init__(timeout=90); self.bot=bot; self.event_id=event_id; self.owner_id=owner_id

    async def interaction_check(self,i):
        if (i.guild is None or i.user.id != self.owner_id or
            not isinstance(i.user,discord.Member) or not can_manage_capts(self.bot,i.user)):
            await reply(i,"Нет прав.")
            return False
        return True

    @discord.ui.button(label="Подтвердить отмену капта",style=discord.ButtonStyle.danger)
    async def confirm(self,i,_):
        if not self.bot.db.cancel_capt(self.event_id):
            return await reply(i,"Капт уже завершён или отменён.")
        await i.response.defer(ephemeral=True,thinking=True)
        await redraw_event(self.bot,i.guild,self.event_id)
        await capt_log(self.bot,i.guild,"Капт отменён",f"#{self.event_id} • <@{i.user.id}>")
        await reply(i,"⛔ Капт отменён. Регистрация закрыта.")


class CaptRosterView(discord.ui.View):
    """Paginated complete roster, including capts with more than 25 participants."""
    def __init__(self,bot,event_id,owner_id):
        super().__init__(timeout=600)
        self.bot=bot; self.event_id=event_id; self.owner_id=owner_id; self.page=0
        self._sync_buttons()

    def _rows(self):
        mains=[("Основной",r) for r in self.bot.db.capt_signups(self.event_id,"main")]
        reserve=[("Резерв",r) for r in self.bot.db.capt_signups(self.event_id,"reserve")]
        return mains+reserve

    def _sync_buttons(self):
        rows=self._rows()
        last=max(0,(len(rows)-1)//20)
        self.page=min(max(self.page,0),last)
        self.prev.disabled=self.page<=0
        self.next.disabled=self.page>=last

    def content(self):
        event=self.bot.db.capt_event(self.event_id)
        rows=self._rows()
        self._sync_buttons()
        start=self.page*20
        end=min(len(rows),start+20)
        lines=[f"⚔️ **Капт #{self.event_id} — полный список ({len(rows)} человек)**",
               f"Страница **{self.page+1}** из **{max(1,(len(rows)+19)//20)}**"]
        for idx,(group,r) in enumerate(rows[start:end],start+1):
            mark="✅" if r["attended"]==1 else "❌" if r["attended"]==0 else "▫️"
            lines.append(f"{idx}. {mark} <@{r['user_id']}> — {group}")
        if not rows:
            lines.append("Пока никто не записался")
        return "\n".join(lines)

    async def interaction_check(self,i):
        if i.user.id!=self.owner_id or i.guild is None:
            await reply(i,"Это чужой список участников.")
            return False
        return True

    @discord.ui.button(label="← Назад",style=discord.ButtonStyle.secondary)
    async def prev(self,i,_):
        self.page-=1
        await i.response.edit_message(content=self.content(),view=self)

    @discord.ui.button(label="Вперёд →",style=discord.ButtonStyle.secondary)
    async def next(self,i,_):
        self.page+=1
        await i.response.edit_message(content=self.content(),view=self)


class CaptEventView(discord.ui.View):
    """One persistent view for every event: event ID derived from message ID in SQLite."""
    def __init__(self,bot):
        super().__init__(timeout=None)
        self.bot=bot

    async def locate(self,i):
        if i.guild is None or i.message is None:
            await reply(i,"Капт не найден."); return None
        e=self.bot.db.capt_event_by_message(i.guild.id,i.message.id)
        if e is None:
            await reply(i,"Информация о капте удалена из базы данных.")
        return e

    async def signup(self,i,choice):
        event=await self.locate(i)
        if event is None:
            return
        if not isinstance(i.user,discord.Member) or not is_family_member(self.bot,i.user):
            return await reply(i,"Записаться могут только участники семьи с ролью Crime/State или общей ролью.")
        before_main={r["user_id"] for r in self.bot.db.capt_signups(event["id"],"main")}
        try:
            selected=self.bot.db.capt_signup(event["id"],i.user.id,choice)
        except ValueError as exc:
            return await reply(i,f"❌ {exc}")
        await i.response.defer(ephemeral=True,thinking=True)
        after_main={r["user_id"] for r in self.bot.db.capt_signups(event["id"],"main")}
        promoted_ids=(after_main-before_main)-{i.user.id}
        for promoted_id in promoted_ids:
            try:
                promoted=await self.bot.fetch_user(promoted_id)
                await promoted.send(f"✅ DYNASTY FAMILY: вы переведены из резерва в основной состав "
                                    f"на капт #{event['id']} (<t:{event['scheduled_at']}:F>). ")
            except discord.HTTPException:
                LOG.info("Could not DM promoted reserve member %s",promoted_id)
        await redraw_event(self.bot,i.guild,event["id"])
        text={"main":"✅ Вы в основном составе.","reserve":"🟡 Вы в резерве.",
              "declined":"❌ Вы не участвуете."}[selected]
        if choice=="main" and selected=="reserve":
            text="🟡 Основной состав заполнен. Вы автоматически добавлены в резерв."
        await reply(i,text)

    @discord.ui.button(label="Участвую",style=discord.ButtonStyle.success,
                       custom_id="dynasty:capt:main",row=0)
    async def main(self,i,_): await self.signup(i,"main")

    @discord.ui.button(label="Резерв",style=discord.ButtonStyle.primary,
                       custom_id="dynasty:capt:reserve",row=0)
    async def reserve(self,i,_): await self.signup(i,"reserve")

    @discord.ui.button(label="Не смогу",style=discord.ButtonStyle.secondary,
                       custom_id="dynasty:capt:declined",row=0)
    async def decline(self,i,_): await self.signup(i,"declined")

    @discord.ui.button(label="Сдать откат",style=discord.ButtonStyle.secondary,
                       custom_id="dynasty:capt:clip",row=0)
    async def clip(self,i,_):
        event=await self.locate(i)
        if event and event["status"]!="cancelled" and isinstance(i.user,discord.Member):
            await i.response.send_modal(CaptClipModal(self.bot,event["id"]))
        elif event:
            await reply(i,"Приём откатов по этому капту закрыт.")

    @discord.ui.button(label="Явка",style=discord.ButtonStyle.primary,
                       custom_id="dynasty:capt:attendance",row=1)
    async def attendance(self,i,_):
        event=await self.locate(i)
        if not event:
            return
        if not isinstance(i.user,discord.Member) or not can_manage_capts(self.bot,i.user):
            return await reply(i,"Только капитаны отмечают явку.")
        await reply(i,"Выберите записанных участников и отметьте их явку. Можно повторять по 25 человек.",
                    view=CaptAttendanceView(self.bot,event["id"],i.user.id))

    @discord.ui.button(label="Завершить",style=discord.ButtonStyle.success,
                       custom_id="dynasty:capt:finish",row=1)
    async def finish(self,i,_):
        event=await self.locate(i)
        if event and event["status"]=="scheduled" and isinstance(i.user,discord.Member) and can_manage_capts(self.bot,i.user):
            await reply(i, "🏁 Выберите результат капта из списка. Затем можно добавить запись и комментарий.",
                        view=CaptFinishChoiceView(self.bot, event["id"], i.user.id))
        elif event:
            await reply(i,"Нет прав или капт уже закрыт.")

    @discord.ui.button(label="Полный состав",style=discord.ButtonStyle.secondary,
                       custom_id="dynasty:capt:roster",row=1)
    async def roster(self,i,_):
        event=await self.locate(i)
        if event is None:
            return
        view=CaptRosterView(self.bot,event["id"],i.user.id)
        await reply(i,view.content(),view=view)

    @discord.ui.button(label="Отменить",style=discord.ButtonStyle.danger,
                       custom_id="dynasty:capt:cancel",row=1)
    async def cancel(self,i,_):
        event=await self.locate(i)
        if event and event["status"]=="scheduled" and isinstance(i.user,discord.Member) and can_manage_capts(self.bot,i.user):
            await reply(i,"Вы уверены? Отмена закроет запись и сохранится в журнале.",
                        view=CaptCancelView(self.bot,event["id"],i.user.id))
        elif event:
            await reply(i,"Нет прав или капт уже закрыт.")


class ClipReviewView(discord.ui.View):
    def __init__(self,bot):
        super().__init__(timeout=None); self.bot=bot

    async def check_and_set(self,i,approved):
        if i.guild is None or i.message is None:
            return await reply(i,"Откат не найден.")
        clip=self.bot.db.clip_for_message(i.guild.id,i.message.id)
        if clip is None:
            return await reply(i,"Откат отсутствует в базе.")
        if not isinstance(i.user,discord.Member) or not can_manage_capts(self.bot,i.user):
            return await reply(i,"Проверять откаты могут только капитаны и администраторы.")
        if not self.bot.db.resolve_clip(clip["id"],i.user.id,approved):
            return await reply(i,"Откат уже рассмотрен.")
        await i.response.defer(ephemeral=True,thinking=True)
        em=i.message.embeds[0] if i.message.embeds else discord.Embed(title="Откат")
        em.color=discord.Color.green() if approved else discord.Color.red()
        em.add_field(name="Решение",value=("✅ Принято" if approved else "❌ Отклонено")+f" • <@{i.user.id}>",inline=False)
        try:
            await i.message.edit(embed=em,view=None,allowed_mentions=discord.AllowedMentions.none())
        except discord.HTTPException:
            LOG.warning("Cannot edit clip review message %s",i.message.id)
        try:
            user=await self.bot.fetch_user(clip["user_id"])
            await user.send(f"Ваш откат #{clip['id']} для капта #{clip['event_id']}: "
                            +("✅ принят" if approved else "❌ отклонён")+".")
        except discord.HTTPException:
            pass
        await capt_log(self.bot,i.guild,"Откат рассмотрен",
                       f"Капт #{clip['event_id']}, откат #{clip['id']} • <@{i.user.id}> • "
                       +("принят" if approved else "отклонён"))
        await reply(i,"✅ Решение по откату сохранено.")

    @discord.ui.button(label="Принять",style=discord.ButtonStyle.success,
                       custom_id="dynasty:clip:approve")
    async def approve(self,i,_): await self.check_and_set(i,True)

    @discord.ui.button(label="Отклонить",style=discord.ButtonStyle.danger,
                       custom_id="dynasty:clip:reject")
    async def reject(self,i,_): await self.check_and_set(i,False)


class CaptHubView(discord.ui.View):
    def __init__(self,bot):
        super().__init__(timeout=None); self.bot=bot

    @discord.ui.button(label="Личный кабинет",style=discord.ButtonStyle.primary,
                       custom_id="dynasty:capt:profile")
    async def profile(self,i,_):
        if not isinstance(i.user,discord.Member):
            return await reply(i,"Доступно только на сервере.")
        await reply(i,"👑 Ваш кабинет (доступен только вам):",
                    embed=profile_embed(self.bot,i.user),view=ProfileView(self.bot,i.user.id))

    @discord.ui.button(label="Расписание",style=discord.ButtonStyle.secondary,
                       custom_id="dynasty:capt:schedule")
    async def schedule(self,i,_):
        rows=self.bot.db.scheduled_capts(i.guild.id,10)
        if not rows:
            return await reply(i,"Предстоящих каптов пока нет.")
        await reply(i,"📅 **Ближайшие капты**\n"+"\n".join(
            f"**#{r['id']}** <t:{r['scheduled_at']}:F> — {clean(r['opponent'],60)} — {clean(r['location'],60)}" for r in rows))

    @discord.ui.button(label="Рейтинг",style=discord.ButtonStyle.secondary,
                       custom_id="dynasty:capt:leaders")
    async def top(self,i,_):
        rows=self.bot.db.family_leaderboard(i.guild.id)
        if not rows:
            return await reply(i,"Пока нет подтверждённой явки на завершённые капты.")
        lines=[]
        for position,r in enumerate(rows,1):
            # A Discord user mention, not a bare ID. Suppress notifications
            # while still rendering the user's nickname in the interface.
            mention=f"<@{r['user_id']}>"
            nick=f" ({clean(r['game_nick'],40)})" if r['game_nick'] else ""
            medal={1:"🥇",2:"🥈",3:"🥉"}.get(position,f"**{position}.**")
            badges=len(earned_achievements(self.bot.db.profile_stats(i.guild.id,r['user_id'])))
            lines.append(f"{medal} {mention}{nick} — **{r['attended']}** явок, "
                         f"**{r['wins']}** побед, 🏅 {badges}")
        em=discord.Embed(title="🏆 DYNASTY • Рейтинг капт-состава",
                         description="\n".join(lines),color=EMBED_GREEN)
        em.set_footer(text="Только подтверждённая явка на завершённые капты")
        await reply(i,"Рейтинг участников:",embed=em)

    @discord.ui.button(label="Создать капт",style=discord.ButtonStyle.success,
                       custom_id="dynasty:capt:create")
    async def create(self,i,_):
        if not isinstance(i.user,discord.Member) or not can_manage_capts(self.bot,i.user):
            return await reply(i,"Создавать капты может только капитан или администратор.")
        await i.response.send_modal(CaptCreateModal(self.bot))


async def publish_capt_hub(bot,guild:discord.Guild):
    conf=bot.db.capt_settings(guild.id)
    channel=guild.get_channel(conf["channel_id"]) if conf["channel_id"] else None
    if not isinstance(channel,discord.TextChannel):
        raise ValueError("Сначала создайте каналы капт-состава")
    em=discord.Embed(title="⚔️ DYNASTY • Капт-состав — Majestic RP Miami",
                     description=("Здесь публикуются капты и формируется **основной состав и резерв**.\n\n"
                                  "✅ Записывайтесь кнопкой «Участвую» под каптом.\n"
                                  "🎥 Откаты сдавайте ссылкой под нужным каптом.\n"
                                  "👑 Профиль и статистику смотрите через «Личный кабинет».\n"
                                  "⚙️ Капт-лидер может создать капт через кнопку ниже.\n\n"
                                  "Данные о посещаемости и результатах подтверждает руководство."),
                     color=EMBED_GREEN)
    old=conf["hub_message_id"]
    if old:
        try: await (await channel.fetch_message(old)).delete()
        except discord.HTTPException: pass
    msg=await channel.send(embed=em,view=CaptHubView(bot),
                           allowed_mentions=discord.AllowedMentions.none())
    bot.db.set_capt_settings(guild.id,hub_message_id=msg.id)
    return msg


async def setup_capt_channels(bot,guild:discord.Guild):
    if bot.db.config(guild.id) is None:
        raise ValueError("Сначала сделайте первичную настройку сервера в /menu")
    conf=bot.db.capt_settings(guild.id)
    me=guild.me or await guild.fetch_member(bot.user.id)
    public={guild.default_role:discord.PermissionOverwrite(view_channel=True,
                      read_message_history=True,send_messages=False),
            me:discord.PermissionOverwrite(view_channel=True,send_messages=True,
                    read_message_history=True,embed_links=True,manage_messages=True)}
    staff={guild.default_role:discord.PermissionOverwrite(view_channel=False),
           me:discord.PermissionOverwrite(view_channel=True,send_messages=True,
                    read_message_history=True,embed_links=True,manage_messages=True)}
    leader=guild.get_role(conf["leader_role_id"]) if conf["leader_role_id"] else None
    if leader:
        for dest in (staff,):
            dest[leader]=discord.PermissionOverwrite(view_channel=True,read_message_history=True,
                                                     send_messages=True)
    fields={"channel_id":("капт-состав",public),"log_channel_id":("журнал-каптов",staff),
            "clips_channel_id":("проверка-откатов",staff)}
    updated={}
    for key,(name,overwrites) in fields.items():
        channel=guild.get_channel(conf[key]) if conf[key] else None
        if not isinstance(channel,discord.TextChannel):
            channel=await guild.create_text_channel(name,overwrites=overwrites,
                           reason="DYNASTY FAMILY: капты и личные профили")
            updated[key]=channel.id
    if updated:
        bot.db.set_capt_settings(guild.id,**updated)
    return updated


async def apply_leader_permissions(bot,guild:discord.Guild,role:discord.Role):
    conf=bot.db.capt_settings(guild.id)
    for key in ("log_channel_id","clips_channel_id"):
        c=guild.get_channel(conf[key]) if conf[key] else None
        if isinstance(c,discord.TextChannel):
            await c.set_permissions(role,view_channel=True,read_message_history=True,
                                    send_messages=True,reason="DYNASTY: роль капт-лидера")


class CaptLeaderSelect(discord.ui.RoleSelect):
    def __init__(self):
        super().__init__(placeholder="Выберите роль капт-лидера",min_values=1,max_values=1,row=0)

    async def callback(self,i:discord.Interaction):
        if i.guild is None or not isinstance(i.user,discord.Member) or not i.user.guild_permissions.administrator:
            return await reply(i,"Только администраторам.")
        role=self.values[0]
        if role.is_default():
            return await reply(i,"Нельзя выбрать @everyone")
        parent=self.view
        old_id=parent.bot.db.capt_settings(i.guild.id)["leader_role_id"]
        parent.bot.db.set_capt_settings(i.guild.id,leader_role_id=role.id)
        try:
            # When changing capt leader roles, revoke the old explicit staff
            # channel override to avoid leaving unnecessary private access.
            if old_id and old_id != role.id:
                old_role=i.guild.get_role(old_id)
                if old_role:
                    for key in ("log_channel_id","clips_channel_id"):
                        c_id=parent.bot.db.capt_settings(i.guild.id)[key]
                        channel=i.guild.get_channel(c_id) if c_id else None
                        if isinstance(channel,discord.TextChannel):
                            await channel.set_permissions(old_role,overwrite=None,
                                reason="DYNASTY: снята прежняя роль капт-лидера")
            await apply_leader_permissions(parent.bot,i.guild,role)
        except discord.HTTPException:
            LOG.exception("Cannot update capt leader channel permissions")
            return await reply(i,"Роль сохранена, но не удалось обновить доступ к журналам. Проверьте права бота.")
        await reply(i,f"✅ Капт-лидер: {role.mention}. Он может создавать капты, отмечать явку и проверять откаты.")


class CaptLeaderSettingsView(discord.ui.View):
    def __init__(self,bot,owner_id):
        super().__init__(timeout=600); self.bot=bot; self.owner_id=owner_id
        self.add_item(CaptLeaderSelect())

    async def interaction_check(self,i):
        if i.user.id!=self.owner_id or not isinstance(i.user,discord.Member) or not i.user.guild_permissions.administrator:
            await reply(i,"Меню доступно только администратору, который его открыл."); return False
        return True


class CaptAnnouncementRoleSelect(discord.ui.RoleSelect):
    def __init__(self):
        super().__init__(placeholder="Роль для анонса новых каптов",min_values=1,max_values=1,row=0)

    async def callback(self,i:discord.Interaction):
        if not isinstance(i.user,discord.Member) or not i.user.guild_permissions.administrator:
            return await reply(i,"Настройка доступна только администратору.")
        role=self.values[0]
        if role.is_default():
            return await reply(i,"Нужно выбрать отдельную роль, а не @everyone.")
        self.view.bot.db.set_capt_settings(i.guild.id, announce_role_id=role.id)
        can_ping = role.mentionable or (i.guild.me is not None and
                                       i.guild.me.guild_permissions.mention_everyone)
        notice=("Бот сможет уведомлять участников роли."
                if can_ping else
                "⚠️ Роль не допускает упоминания. Разрешите ей упоминания в настройках роли "
                "или выдайте боту право «Упоминать @everyone, @here и все роли».")
        await reply(i,f"✅ Роль анонсов: {role.mention}. {notice}")


class CaptAnnouncementRoleSettingsView(discord.ui.View):
    def __init__(self,bot,owner_id):
        super().__init__(timeout=600); self.bot=bot; self.owner_id=owner_id
        self.add_item(CaptAnnouncementRoleSelect())

    async def interaction_check(self,i):
        if (i.user.id != self.owner_id or not isinstance(i.user,discord.Member)
                or not i.user.guild_permissions.administrator):
            await reply(i,"Меню доступно только администратору, который его открыл.")
            return False
        return True


class CaptTimezoneModal(discord.ui.Modal,title="Часовой пояс каптов"):
    timezone=discord.ui.TextInput(label="Название часового пояса (IANA)",
                                  placeholder="Europe/Moscow",max_length=64)
    def __init__(self,bot):
        super().__init__(timeout=300); self.bot=bot
    async def on_submit(self,i):
        if not isinstance(i.user,discord.Member) or not i.user.guild_permissions.administrator:
            return await reply(i,"Только для администраторов.")
        value=str(self.timezone.value).strip()
        try: ZoneInfo(value)
        except ZoneInfoNotFoundError:
            return await reply(i,"Неизвестный часовой пояс. Например: Europe/Moscow или Europe/Riga.")
        self.bot.db.set_capt_settings(i.guild.id,timezone=value)
        await reply(i,f"✅ Время новых каптов будет вводиться в часовом поясе `{value}`. В Discord оно показывается каждому по локальному времени.")


class CaptVoiceSelect(discord.ui.ChannelSelect):
    def __init__(self):
        super().__init__(placeholder="Выберите голосовой канал сбора",min_values=1,max_values=1,
                         channel_types=[discord.ChannelType.voice],row=0)
    async def callback(self,i):
        if not isinstance(i.user,discord.Member) or not i.user.guild_permissions.administrator:
            return await reply(i,"Только для администраторов.")
        channel=self.values[0]
        self.view.bot.db.set_capt_settings(i.guild.id,voice_channel_id=channel.id)
        await reply(i,f"✅ Канал сбора: {channel.mention}. Бот укажет его в напоминаниях.")


class CaptVoiceSettingsView(discord.ui.View):
    def __init__(self,bot,owner_id):
        super().__init__(timeout=600); self.bot=bot; self.owner_id=owner_id
        self.add_item(CaptVoiceSelect())
    async def interaction_check(self,i):
        if i.user.id!=self.owner_id or not isinstance(i.user,discord.Member) or not i.user.guild_permissions.administrator:
            await reply(i,"Недостаточно прав"); return False
        return True


class CaptsAdminView(discord.ui.View):
    """Nested in the existing /menu; no new admin slash command is needed."""
    def __init__(self,bot,owner_id,back_factory):
        super().__init__(timeout=600)
        self.bot=bot; self.owner_id=owner_id; self.back_factory=back_factory

    async def interaction_check(self,i):
        if i.guild is None or i.user.id!=self.owner_id or not isinstance(i.user,discord.Member) or not i.user.guild_permissions.administrator:
            await reply(i,"Настройки доступны только администратору, открывшему это меню."); return False
        return True

    @discord.ui.button(label="Создать каналы",style=discord.ButtonStyle.primary,row=0)
    async def setup(self,i,_):
        await i.response.defer(ephemeral=True,thinking=True)
        try:
            changed=await setup_capt_channels(self.bot,i.guild)
            conf=self.bot.db.capt_settings(i.guild.id)
            if not conf["hub_message_id"]:
                await publish_capt_hub(self.bot,i.guild)
        except (discord.HTTPException,ValueError) as exc:
            LOG.exception("Capt setup failure")
            return await reply(i,f"❌ Не удалось создать модуль: {exc}")
        await reply(i,"✅ Каналы капт-состава и журналов готовы. "+
                    ("Созданы: "+", ".join(changed) if changed else "Существующие каналы сохранены."))

    @discord.ui.button(label="Создать капт",style=discord.ButtonStyle.success,row=0)
    async def create(self,i,_):
        await i.response.send_modal(CaptCreateModal(self.bot))

    @discord.ui.button(label="Роль капитана",style=discord.ButtonStyle.secondary,row=0)
    async def leader(self,i,_):
        await i.response.edit_message(content="Выберите роль руководителя капт-состава.",
                            view=CaptLeaderSettingsView(self.bot,self.owner_id))

    @discord.ui.button(label="Часовой пояс",style=discord.ButtonStyle.secondary,row=1)
    async def timezone(self,i,_):
        await i.response.send_modal(CaptTimezoneModal(self.bot))

    @discord.ui.button(label="Канал сбора",style=discord.ButtonStyle.secondary,row=1)
    async def voice(self,i,_):
        await i.response.edit_message(content="Выберите голосовой канал для сбора капт-состава.",
                            view=CaptVoiceSettingsView(self.bot,self.owner_id))

    @discord.ui.button(label="Публиковать панель",style=discord.ButtonStyle.primary,row=1)
    async def panel(self,i,_):
        await i.response.defer(ephemeral=True,thinking=True)
        try: msg=await publish_capt_hub(self.bot,i.guild)
        except (ValueError,discord.HTTPException) as exc:
            return await reply(i,f"❌ {exc}")
        await reply(i,f"✅ Панель каптов повторно опубликована: {msg.jump_url}")

    @discord.ui.button(label="Журнал каптов",style=discord.ButtonStyle.secondary,row=2)
    async def journal(self,i,_):
        conf=self.bot.db.capt_settings(i.guild.id)
        c=i.guild.get_channel(conf["log_channel_id"]) if conf["log_channel_id"] else None
        if not isinstance(c,discord.TextChannel):
            return await reply(i,"Журнал не создан. Используйте «Создать каналы».")
        await capt_log(self.bot,i.guild,"Журнал активен",
                       f"Проверка администратором <@{i.user.id}>. Новые события будут записываться сюда.")
        await reply(i,f"✅ Журнал каптов: {c.mention}. История удалённых сообщений не восстанавливается.")

    @discord.ui.button(label="Журнал откатов",style=discord.ButtonStyle.secondary,row=2)
    async def clips(self,i,_):
        conf=self.bot.db.capt_settings(i.guild.id)
        c=i.guild.get_channel(conf["clips_channel_id"]) if conf["clips_channel_id"] else None
        if not isinstance(c,discord.TextChannel):
            return await reply(i,"Канал проверки откатов не создан. Используйте «Создать каналы».")
        await reply(i,f"🎥 Проверка откатов: {c.mention}. Новые отчёты будут появляться здесь с кнопками принятия/отказа.")

    @discord.ui.button(label="Статистика",style=discord.ButtonStyle.secondary,row=2)
    async def stats(self,i,_):
        leaderboard=self.bot.db.family_leaderboard(i.guild.id,10)
        upcoming=self.bot.db.scheduled_capts(i.guild.id,5)
        lines=[f"#{r['id']} — <t:{r['scheduled_at']}:f> • {clean(r['opponent'],50)}" for r in upcoming]
        top=[f"{index}. <@{r['user_id']}> — {r['attended']} явок" for index,r in enumerate(leaderboard,1)]
        await reply(i,"📊 **Ближайшие капты:**\n"+("\n".join(lines) if lines else "Нет")+
                    "\n\n🏆 **Топ по явке:**\n"+("\n".join(top) if top else "Нет"))

    @discord.ui.button(label="📣 Роль анонсов",style=discord.ButtonStyle.primary,row=3)
    async def announce_role(self,i,_):
        await i.response.edit_message(
            content="Выберите одну роль, которую бот будет упоминать при публикации нового капта.",
            view=CaptAnnouncementRoleSettingsView(self.bot,self.owner_id))

    @discord.ui.button(label="← Главное меню",style=discord.ButtonStyle.secondary,row=4)
    async def back(self,i,_):
        await i.response.edit_message(content="🛠️ DYNASTY • Меню администратора",view=self.back_factory())


async def capt_reminder_loop(bot):
    """Announce capts 30/15/5 minutes before start; idempotent across restarts.

    Notifications go to the capt channel with mentions of the actual main
    roster, plus best-effort DM to each main participant. No pings are sent to
    the entire role on every reminder (role receives the initial announcement).
    """
    await bot.wait_until_ready()
    while not bot.is_closed():
        try:
            now=int(time.time())
            for event in bot.db.due_capt_reminders(now):
                minutes=due_reminder_minutes(event["scheduled_at"],now)
                if minutes is None or not bot.db.claim_capt_reminder(event["id"],minutes):
                    continue
                guild=bot.get_guild(event["guild_id"])
                channel=guild.get_channel(event["channel_id"]) if guild and event["channel_id"] else None
                if not isinstance(channel,discord.TextChannel):
                    bot.db.release_capt_reminder(event["id"],minutes)
                    continue
                mains=bot.db.capt_signups(event["id"],"main")
                conf=bot.db.capt_settings(guild.id)
                voice=guild.get_channel(conf["voice_channel_id"]) if conf["voice_channel_id"] else None
                participants=[r["user_id"] for r in mains]
                link=(f"https://discord.com/channels/{guild.id}/{event['channel_id']}/"
                      f"{event['message_id']}")
                headline=f"⏰ **До капта #{event['id']} — {minutes} минут!**"
                body=(f"Противник: **{clean(event['opponent'],80)}**. "
                      f"Сбор: {voice.mention if voice else 'уточните у капитана'}.\n"
                      f"Карточка капта: {link}")
                try:
                    # Full roster can exceed Discord's 2000-character limit.
                    # Mention main participants in batches of up to 30 users.
                    await channel.send(headline+"\n"+body,
                        allowed_mentions=discord.AllowedMentions.none())
                    for begin in range(0,len(participants),30):
                        selected=participants[begin:begin+30]
                        await channel.send("🔔 Основной состав: "+" ".join(f"<@{uid}>" for uid in selected),
                            allowed_mentions=discord.AllowedMentions(
                                everyone=False,roles=False,
                                users=[discord.Object(id=uid) for uid in selected],
                                replied_user=False))
                except discord.HTTPException:
                    bot.db.release_capt_reminder(event["id"],minutes)
                    LOG.exception("Could not send capt reminder %s (%sm)",event["id"],minutes)
                    continue
                # DMs are helpful but optional; privacy settings may forbid them.
                # The public reminder remains delivered even if a DM fails.
                for user_id in participants:
                    try:
                        member=guild.get_member(user_id)
                        if member is None:
                            member=await bot.fetch_user(user_id)
                        await member.send(f"⏰ DYNASTY FAMILY: до капта #{event['id']} "
                                          f"осталось {minutes} минут!\n"+body)
                    except discord.HTTPException:
                        LOG.info("Could not DM capt reminder to %s",user_id)
        except Exception:
            LOG.exception("Capt reminder loop error")
        await asyncio.sleep(25)


def register_slash_commands(bot):
    @bot.tree.command(name="profile",description="Ваш личный кабинет DYNASTY FAMILY")
    @app_commands.guild_only()
    async def profile(i:discord.Interaction):
        if not isinstance(i.user,discord.Member):
            return await reply(i,"Личный кабинет доступен только внутри сервера.")
        await reply(i,"👑 Личный кабинет — доступен только вам.",
                    embed=profile_embed(bot,i.user),view=ProfileView(bot,i.user.id))


def register_persistent_views(bot):
    bot.add_view(CaptHubView(bot))
    bot.add_view(CaptEventView(bot))
    bot.add_view(ClipReviewView(bot))
