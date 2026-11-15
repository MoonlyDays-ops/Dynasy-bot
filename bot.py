"""Discord application approvals + self-managed temporary voice rooms.

Python 3.11+; discord.py 2.7.1. Start with: python bot.py
"""
from __future__ import annotations

import asyncio
import logging
import os
import re
import sqlite3
import time
from pathlib import Path

import discord
from discord import app_commands
from discord.ext import commands
from dotenv import load_dotenv

from storage import Storage

load_dotenv()
TOKEN = os.getenv("DISCORD_TOKEN", "").strip()
GUILD_ID = int(os.getenv("GUILD_ID", "0") or "0")
DATABASE_PATH = os.getenv("DATABASE_PATH", "data/bot.sqlite3")
BANNER_PATH = Path(__file__).resolve().parent / "assets" / "dynasty_banner.png"

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
LOG = logging.getLogger("application_voice_bot")


def parse_user_id(value: str) -> int | None:
    """Allow Discord IDs or <@mentions>; do not guess from names."""
    match = re.fullmatch(r"\s*(?:<@!?(\d{15,22})>|(\d{15,22}))\s*", value)
    return int(match.group(1) or match.group(2)) if match else None


def parse_role_list(text: str) -> list[int]:
    """Comma/space/newline-separated Discord role IDs or role mentions."""
    parts = re.split(r"[,;\s]+", text.strip())
    if not text.strip() or len(parts) > 50:
        raise ValueError("Укажите от 1 до 50 ролей (ID или @упоминания).")
    result = []
    for part in parts:
        match = re.fullmatch(r"(?:<@&(\d{15,22})>|(\d{15,22}))", part)
        if not match:
            raise ValueError(f"Неверное значение роли: {part[:50]}")
        value = int(match.group(1) or match.group(2))
        if value not in result:
            result.append(value)
    return result


async def say(interaction: discord.Interaction, message: str):
    """Use followup after a deferred interaction, else the initial response."""
    if interaction.response.is_done():
        await interaction.followup.send(message, ephemeral=True)
    else:
        await interaction.response.send_message(message, ephemeral=True)


async def get_member(guild: discord.Guild, user_id: int) -> discord.Member | None:
    cached = guild.get_member(user_id)
    if cached is not None:
        return cached
    try:
        return await guild.fetch_member(user_id)
    except discord.NotFound:
        return None


async def require_owner_room(bot: "ApprovalVoiceBot", interaction: discord.Interaction):
    guild = interaction.guild
    if guild is None or not isinstance(interaction.user, discord.Member):
        await say(interaction, "Эта функция доступна только участникам сервера.")
        return None
    data = bot.db.owned_room(guild.id, interaction.user.id)
    channel = guild.get_channel(data["channel_id"]) if data else None
    if not isinstance(channel, discord.VoiceChannel):
        await say(interaction, "У вас нет временной комнаты. Подключитесь к каналу «➕ Создать комнату».")
        return None
    voice = interaction.user.voice
    if not voice or not voice.channel or voice.channel.id != channel.id:
        await say(interaction, "Для управления необходимо находиться в своей голосовой комнате.")
        return None
    return channel


def application_panel_embed() -> discord.Embed:
    """The banner is uploaded as a message attachment, not an external URL."""
    embed = discord.Embed(
        title="👑 DYNASTY FAMILY — Заявка на вступление",
        description=(
            "Добро пожаловать в **DYNASTY FAMILY**!\n\n"
            "Нажмите **«Подать заявку»** и заполните анкету. "
            "Выберите направление **Crime** или **State**.\n\n"
            "📨 После отправки ваша анкета поступит администрации.\n"
            "✅ При одобрении вы получите роль выбранного направления.\n"
            "❌ При отказе бот исключит вас с сервера.\n"
            "🔔 Статус заявки бот постарается отправить в личные сообщения."
        ),
        color=discord.Color.from_rgb(188, 155, 91),
    )
    embed.set_image(url="attachment://dynasty_banner.png")
    embed.set_footer(text="DYNASTY FAMILY • Заявки доступны только модераторам")
    return embed


async def publish_application_panel(bot: "ApprovalVoiceBot", channel: discord.TextChannel):
    """Upload banner with the embed so the image survives without an image host."""
    if not BANNER_PATH.is_file():
        raise FileNotFoundError(f"Файл баннера не найден: {BANNER_PATH}")
    return await channel.send(
        file=discord.File(BANNER_PATH, filename="dynasty_banner.png"),
        embed=application_panel_embed(),
        view=ApplicationPanelView(bot),
    )


def safe_embed_value(value: str) -> str:
    """Prevent applicants from inserting mass mentions and confusing Markdown."""
    return discord.utils.escape_mentions(discord.utils.escape_markdown(value.strip()))[:1024] or "—"


def configured_application_roles(guild: discord.Guild, config) -> bool:
    return bool(config and config["crime_role_id"] and config["state_role_id"]
                and guild.get_role(config["crime_role_id"])
                and guild.get_role(config["state_role_id"]))


def application_ping_roles(guild: discord.Guild, bot: "ApprovalVoiceBot") -> list[discord.Role]:
    """Only mention the roles explicitly selected by a server administrator.

    Deleted roles are ignored; no @everyone, user-provided text or implicit pings.
    """
    return [role for role_id in bot.db.application_ping_role_ids(guild.id)
            if (role := guild.get_role(role_id)) is not None and not role.is_default()]


async def prepare_application_channel(guild: discord.Guild, settings, bot_member: discord.Member):
    """Upgrade old #подать-заявку permissions for PRIVATE application threads.

    Manage Threads lets staff see all private threads. Ordinary users can see
    the application panel, but must not create threads or see other applications.
    """
    channel = guild.get_channel(settings["application_channel_id"])
    moderator = guild.get_role(settings["moderator_role_id"])
    if not isinstance(channel, discord.TextChannel) or moderator is None:
        raise RuntimeError("Канал «подать-заявку» или роль модератора не найдены.")
    bot_ow = channel.overwrites_for(bot_member)
    bot_ow.update(view_channel=True, send_messages=True, read_message_history=True,
                  create_private_threads=True, send_messages_in_threads=True,
                  manage_threads=True, manage_messages=True, embed_links=True, attach_files=True)
    await channel.set_permissions(bot_member, overwrite=bot_ow,
                                  reason="DYNASTY: доступ бота к приватным веткам")
    mod_ow = channel.overwrites_for(moderator)
    mod_ow.update(view_channel=True, read_message_history=True,
                  manage_threads=True, send_messages_in_threads=True)
    await channel.set_permissions(moderator, overwrite=mod_ow,
                                  reason="DYNASTY: модерация приватных веток")
    everyone_ow = channel.overwrites_for(guild.default_role)
    everyone_ow.update(view_channel=True, send_messages=False, read_message_history=True,
                       create_public_threads=False, create_private_threads=False,
                       send_messages_in_threads=False)
    await channel.set_permissions(guild.default_role, overwrite=everyone_ow,
                                  reason="DYNASTY: приватность заявок")
    return channel


async def send_application_dm(user: discord.abc.User, *, app_id: int, guild_name: str,
                              status: str, direction: str | None = None,
                              role_name: str | None = None,
                              call_channel: discord.VoiceChannel | None = None) -> bool:
    """Best effort: closed DMs must never cancel an already-processed decision."""
    if status == "pending":
        title = "📨 Заявка получена"
        body = f"Ваша заявка №{app_id} на сервер **{guild_name}** принята к рассмотрению."
        color = discord.Color.gold()
    elif status == "called":
        title = "📞 Вас вызвали на обзвон"
        channel_text = call_channel.mention if call_channel else "голосовой канал обзвона"
        body = (f"Ваша заявка №{app_id} на сервер **{guild_name}**: "
                f"зайдите в {channel_text} в течение **5 минут**, "
                "иначе заявка будет автоматически отклонена, но вы останетесь на сервере.")
        color = discord.Color.orange()
    elif status == "arrived":
        title = "✅ Явка на обзвон зафиксирована"
        body = (f"Вы вошли в канал обзвона по заявке №{app_id} на сервере **{guild_name}**. "
                "Автоматическое отклонение отменено, ожидайте решения модератора.")
        color = discord.Color.green()
    elif status == "missed_call":
        title = "⌛ Заявка отклонена: неявка на обзвон"
        body = (f"Вы не вошли в канал обзвона по заявке №{app_id} на сервере **{guild_name}** "
                "за отведённые 5 минут. Заявка закрыта, но **вы остались на сервере**. "
                "Для новой заявки воспользуйтесь панелью подачи заявок.")
        color = discord.Color.orange()
    elif status == "accepted":
        title = "✅ Заявка одобрена"
        body = (f"Ваша заявка №{app_id} на сервер **{guild_name}** одобрена!\n"
                f"Направление: **{direction or '—'}**.\n"
                f"Назначенная роль: **{discord.utils.escape_markdown(role_name or '—')}**.")
        color = discord.Color.green()
    else:
        title = "❌ Заявка отклонена"
        body = (f"Ваша заявка №{app_id} на сервер **{guild_name}** отклонена. "
                "В связи с этим ваш доступ к серверу будет закрыт.")
        color = discord.Color.red()
    try:
        await user.send(embed=discord.Embed(title=title, description=body, color=color))
        return True
    except (discord.Forbidden, discord.HTTPException):
        LOG.info("Could not DM application status to user %s", user.id)
        return False


def application_audit_embed(entry, *, event: str, actor_id: int | None = None,
                            dm_sent: bool | None = None, note: str | None = None) -> discord.Embed:
    """Always retain the full submitted form in the private audit log."""
    colors = {"received": discord.Color.gold(), "called": discord.Color.orange(),
              "arrived": discord.Color.green(), "accepted": discord.Color.green(),
              "rejected": discord.Color.red(), "missed_call": discord.Color.orange(),
              "timeout_failed": discord.Color.red()}
    labels = {"received": "📨 Отправлена", "called": "📞 Вызван на обзвон",
              "arrived": "🎙️ Прибыл на обзвон", "accepted": "✅ Принята",
              "rejected": "❌ Отклонена", "missed_call": "⌛ Неявка (без кика)",
              "timeout_failed": "⚠️ Не удалось выполнить автоотказ"}
    embed = discord.Embed(
        title=f"Заявка №{entry['id']}: {labels.get(event, event)}",
        description=f"**Кандидат:** <@{entry['applicant_id']}> (`{entry['applicant_id']}`)",
        color=colors.get(event, discord.Color.blurple()),
    )
    embed.add_field(name="1. Ник в игре", value=safe_embed_value(entry['game_nick'] or '—'), inline=False)
    embed.add_field(name="2. Статик #", value=safe_embed_value(entry['static_number'] or '—'), inline=True)
    embed.add_field(name="3. Имя и возраст IRL", value=safe_embed_value(entry['irl_name_age'] or '—'), inline=True)
    embed.add_field(name="4. Направление", value=safe_embed_value(entry['direction'] or '—'), inline=False)
    embed.add_field(name="5. Как узнали о семье и другие семьи",
                    value=safe_embed_value(entry['history'] or '—'), inline=False)
    if actor_id is not None:
        embed.add_field(name="Модератор", value=f"<@{actor_id}>", inline=True)
    else:
        embed.add_field(name="Решение", value="Автоматическая проверка", inline=True)
    if dm_sent is not None:
        embed.add_field(name="Личное сообщение", value="Отправлено" if dm_sent else "Не доставлено", inline=True)
    if entry['call_channel_id']:
        embed.add_field(name="Канал обзвона", value=f"<#{entry['call_channel_id']}>", inline=True)
    if entry['call_result']:
        outcomes = {"waiting": "Ожидается", "joined": "Прибыл вовремя",
                    "missed": "Не прибыл за 5 минут", "failed": "Ошибка вызова",
                    "timeout_failed": "Ошибка автоотказа"}
        embed.add_field(name="Обзвон", value=outcomes.get(entry['call_result'], entry['call_result']), inline=True)
    if note:
        embed.add_field(name="Примечание", value=safe_embed_value(note), inline=False)
    embed.set_footer(text=f"ID заявки: {entry['id']} • ID пользователя: {entry['applicant_id']}")
    return embed


async def write_application_log(bot: "ApprovalVoiceBot", guild: discord.Guild,
                                entry, *, event: str, actor_id: int | None = None,
                                dm_sent: bool | None = None, note: str | None = None):
    settings = bot.db.config(guild.id)
    if settings is None:
        return
    channel = guild.get_channel(settings["log_channel_id"])
    if not isinstance(channel, discord.TextChannel):
        LOG.warning("Application log channel missing on guild %s", guild.id)
        return
    try:
        await channel.send(
            embed=application_audit_embed(entry, event=event, actor_id=actor_id,
                                          dm_sent=dm_sent, note=note),
            allowed_mentions=discord.AllowedMentions.none(),
        )
    except discord.HTTPException:
        LOG.exception("Could not log application %s event %s", entry['id'], event)


async def safely_delete_application_thread(bot: "ApprovalVoiceBot", guild: discord.Guild, entry,
                                            reason: str):
    if not entry["thread_id"]:
        return
    try:
        thread = guild.get_thread(entry["thread_id"]) or await guild.fetch_channel(entry["thread_id"])
        if isinstance(thread, discord.Thread):
            await thread.delete(reason=reason)
    except discord.NotFound:
        pass
    except discord.HTTPException:
        LOG.exception("Could not delete application thread %s", entry['thread_id'])



async def write_voice_log(bot: "ApprovalVoiceBot", guild: discord.Guild, title: str,
                          description: str, *, color: discord.Color | None = None):
    """Best-effort private event journal for temporary voice rooms."""
    settings = bot.db.config(guild.id)
    if settings is None or not settings["voice_log_channel_id"]:
        return
    channel = guild.get_channel(settings["voice_log_channel_id"])
    if not isinstance(channel, discord.TextChannel):
        return
    try:
        await channel.send(
            embed=discord.Embed(title=title, description=description,
                                color=color or discord.Color.dark_teal()),
            allowed_mentions=discord.AllowedMentions.none(),
        )
    except discord.HTTPException:
        LOG.exception("Could not write voice log on guild %s", guild.id)


def voice_panel_embed() -> discord.Embed:
    embed = discord.Embed(
        title="🎙️ Управление приватной комнатой",
        description=(
            "**Как создать комнату:** зайдите в голосовой канал **«➕ Создать комнату»**. "
            "Бот создаст отдельный канал и переместит вас туда.\n\n"
            "**Кнопки управления:**\n"
            "➕ / ➖ — добавить / убрать один слот\n"
            "🔓 / 🔒 — открыть / закрыть вход\n"
            "✏️ — переименовать канал\n"
            "👤+ / 👤− — предоставить / убрать персональный доступ\n"
            "⛔ / ♻️ — заблокировать / разблокировать пользователя\n"
            "🤝 — передать владение участнику комнаты\n"
            "🙈 / 👁️ — скрыть / показать комнату\n"
            "🗑️ — удалить комнату (с подтверждением)\n\n"
            "**Важно:** управлять настройками может только владелец, находящийся в своей комнате. "
            "При выходе всех пользователей комната удаляется автоматически."
        ),
        color=discord.Color.dark_teal(),
    )
    embed.set_footer(text="0 слотов = без ограничений. Права модераторов сохраняются.")
    return embed


class ApplicationModal(discord.ui.Modal, title="Заявка в DYNASTY FAMILY"):
    """Five fields, with an actual Crime/State dropdown in the Discord modal."""

    def __init__(self, bot: "ApprovalVoiceBot"):
        super().__init__()
        self.bot = bot

        self.game_nick = discord.ui.TextInput(
            placeholder="Например, Alex_Dynasty", min_length=2, max_length=80,
        )
        self.static_number = discord.ui.TextInput(
            placeholder="Только цифры, например 12345", min_length=1, max_length=12,
        )
        self.irl_name_age = discord.ui.TextInput(
            placeholder="Например, Александр, 20 лет", min_length=3, max_length=100,
        )
        self.direction = discord.ui.Select(
            placeholder="Выберите одно направление",
            min_values=1, max_values=1, required=True,
            options=[
                discord.SelectOption(label="Crime", value="Crime", description="Криминальное направление"),
                discord.SelectOption(label="State", value="State", description="Государственное направление"),
            ],
        )
        self.history = discord.ui.TextInput(
            placeholder="Откуда узнали о семье? Были ли в других известных семьях?",
            style=discord.TextStyle.paragraph, min_length=5, max_length=1000,
        )
        self.add_item(discord.ui.Label(text="1. Ник в игре", component=self.game_nick))
        self.add_item(discord.ui.Label(text="2. Статик # (только цифры)", component=self.static_number))
        self.add_item(discord.ui.Label(text="3. Имя и возраст IRL", component=self.irl_name_age))
        self.add_item(discord.ui.Label(text="4. Направление", component=self.direction))
        self.add_item(discord.ui.Label(text="5. Как узнали о семье и где были раньше?", component=self.history))

    async def on_submit(self, interaction: discord.Interaction):
        guild = interaction.guild
        if guild is None or not isinstance(interaction.user, discord.Member):
            return await say(interaction, "Используйте форму на сервере.")

        static_number = self.static_number.value.strip()
        # ASCII digits only, not Unicode numbers, signs, spaces or # symbols.
        if not re.fullmatch(r"[0-9]{1,12}", static_number):
            return await say(interaction, "❌ В поле «Статик #» разрешены только цифры 0–9, без пробелов и знака #.")
        direction = self.direction.values[0] if self.direction.values else None
        if direction not in ("Crime", "State"):
            return await say(interaction, "❌ Выберите Crime или State.")

        settings = self.bot.db.config(guild.id)
        if settings is None:
            return await say(interaction, "Система заявок ещё не настроена.")
        if not configured_application_roles(guild, settings):
            return await say(interaction, "Администратору нужно настроить роли через /menu.")
        approved_roles = {settings["crime_role_id"], settings["state_role_id"]}
        if any(role.id in approved_roles for role in interaction.user.roles):
            return await say(interaction, "Вы уже состоите в семье.")

        await interaction.response.defer(ephemeral=True, thinking=True)
        try:
            application_id = self.bot.db.reserve_application(
                guild.id, interaction.user.id,
                game_nick=self.game_nick.value.strip(), static_number=static_number,
                irl_name_age=self.irl_name_age.value.strip(), direction=direction,
                history=self.history.value.strip(),
            )
        except sqlite3.IntegrityError:
            return await say(interaction, "Ваша предыдущая заявка ещё рассматривается.")

        thread = None
        try:
            # Private thread appears DIRECTLY under #подать-заявку.
            channel = guild.get_channel(settings["application_channel_id"])
            if not isinstance(channel, discord.TextChannel):
                raise RuntimeError("Канал «подать-заявку» удалён или недоступен.")
            thread = await channel.create_thread(
                name=f"Заявка-{application_id}-{interaction.user.id}",
                type=discord.ChannelType.private_thread,
                invitable=False,
                auto_archive_duration=1440,
                reason=f"Заявка пользователя {interaction.user.id}",
            )
            embed = discord.Embed(
                title=f"👑 DYNASTY • Анкета №{application_id}",
                color=discord.Color.gold(),
                description=f"**Пользователь:** <@{interaction.user.id}>\n**Discord ID:** `{interaction.user.id}`",
            )
            embed.add_field(name="1. Ник в игре", value=safe_embed_value(self.game_nick.value), inline=False)
            embed.add_field(name="2. Статик #", value=static_number, inline=True)
            embed.add_field(name="3. Имя и возраст IRL", value=safe_embed_value(self.irl_name_age.value), inline=True)
            embed.add_field(name="4. Направление", value=direction, inline=False)
            embed.add_field(name="5. Откуда узнали и другие семьи", value=safe_embed_value(self.history.value), inline=False)
            embed.set_footer(text="Решение принимается только модераторами • одна активная заявка")
            # A real message mention (not an embed field) can ping the selected roles.
            # Restrict AllowedMentions to those roles; never ping @everyone or the applicant.
            ping_roles = application_ping_roles(guild, self.bot)
            ping_text = " ".join(role.mention for role in ping_roles)
            message = await thread.send(
                content=(f"🔔 **Новая заявка №{application_id}!**\n{ping_text}"
                         if ping_roles else None),
                embed=embed, view=ReviewView(self.bot),
                allowed_mentions=discord.AllowedMentions(
                    everyone=False, users=False, roles=ping_roles, replied_user=False,
                ),
            )
            self.bot.db.attach_application(application_id, thread.id, message.id)
            await write_application_log(
                self.bot, guild, self.bot.db.application_by_id(application_id), event="received",
                actor_id=interaction.user.id,
            )
        except Exception:
            LOG.exception("Could not create application %s", application_id)
            self.bot.db.abort_application(application_id)
            if thread is not None:
                try:
                    await thread.delete(reason="Ошибка создания заявки")
                except discord.HTTPException:
                    pass
            return await say(interaction, "Не удалось отправить заявку. Проверьте права бота и консоль.")

        # The application is already saved; a DM/response failure must not
        # delete it or leave a dangling active record.
        dm_sent = await send_application_dm(
            interaction.user, app_id=application_id, guild_name=guild.name,
            status="pending",
        )
        reply = "✅ Заявка отправлена. Администрация рассмотрит её во внутренней ветке."
        if not dm_sent:
            reply += "\n⚠️ Не удалось отправить ЛС: возможно, у вас закрыты личные сообщения."
        await say(interaction, reply)


class ApplicationPanelView(discord.ui.View):
    def __init__(self, bot: "ApprovalVoiceBot"):
        super().__init__(timeout=None)
        self.bot = bot

    @discord.ui.button(label="Подать заявку", emoji="📝", style=discord.ButtonStyle.primary,
                       custom_id="avb:application:open")
    async def apply(self, interaction: discord.Interaction, button: discord.ui.Button):
        guild = interaction.guild
        if guild is None or not isinstance(interaction.user, discord.Member):
            return await say(interaction, "Подать заявку можно только на сервере.")
        settings = self.bot.db.config(guild.id)
        if settings is None:
            return await say(interaction, "Администрация ещё не настроила эту функцию.")
        if not configured_application_roles(guild, settings):
            return await say(interaction, "Администратору нужно назначить роли Crime и State через /menu.")
        approved_roles = {settings["crime_role_id"], settings["state_role_id"]}
        if any(role.id in approved_roles for role in interaction.user.roles):
            return await say(interaction, "Вы уже состоите в семье.")
        if self.bot.db.active_application(guild.id, interaction.user.id):
            return await say(interaction, "Ваша предыдущая заявка ещё находится на рассмотрении.")
        await interaction.response.send_modal(ApplicationModal(self.bot))


class ReviewView(discord.ui.View):
    def __init__(self, bot: "ApprovalVoiceBot"):
        super().__init__(timeout=None)
        self.bot = bot

    async def context(self, interaction: discord.Interaction):
        guild = interaction.guild
        if guild is None or not isinstance(interaction.user, discord.Member) or interaction.message is None:
            await say(interaction, "Кнопки работают только внутри ветки заявки.")
            return None
        settings = self.bot.db.config(guild.id)
        entry = self.bot.db.application_for_message(interaction.message.id)
        if settings is None or entry is None or interaction.channel_id != entry["thread_id"]:
            await say(interaction, "Заявка не найдена или уже удалена.")
            return None
        moderator_role = guild.get_role(settings["moderator_role_id"])
        if not (interaction.user.guild_permissions.administrator or
                (moderator_role is not None and moderator_role in interaction.user.roles)):
            await say(interaction, "Рассматривать заявки могут только модераторы.")
            return None
        return guild, settings, entry

    async def decide(self, interaction: discord.Interaction, accepted: bool):
        context = await self.context(interaction)
        if context is None:
            return
        guild, settings, entry = context
        if not self.bot.db.claim_application(entry["id"]):
            return await say(interaction, "Заявку уже рассматривают или решение принято.")
        await interaction.response.defer(ephemeral=True, thinking=True)
        selected_role = None
        common_role = None
        dm_sent = False
        applicant = None
        direction = entry["direction"]
        try:
            applicant = await get_member(guild, entry["applicant_id"])
            me = guild.me or await guild.fetch_member(self.bot.user.id)
            if accepted:
                role_id = (settings["crime_role_id"] if direction == "Crime" else
                           settings["state_role_id"] if direction == "State" else
                           settings["approved_role_id"])
                selected_role = guild.get_role(role_id) if role_id else None
                common_id = settings["common_role_id"]
                common_role = guild.get_role(common_id) if common_id else None
                if selected_role is None:
                    raise RuntimeError("Роль Crime/State не найдена.")
                if common_role is None:
                    raise RuntimeError("Сначала настройте обязательную роль через /menu → Общая роль.")
                if selected_role >= me.top_role or common_role >= me.top_role:
                    raise RuntimeError("Роль бота должна быть выше ВСЕХ выдаваемых ролей.")
                if applicant is None:
                    raise RuntimeError("Кандидат уже покинул сервер.")
                await applicant.add_roles(
                    selected_role, common_role,
                    reason=f"Заявка #{entry['id']} одобрена {interaction.user}",
                )
                decision = "accepted"
            else:
                if applicant is not None:
                    if applicant.id == guild.owner_id or applicant.top_role >= me.top_role:
                        raise RuntimeError("Нельзя исключить владельца или участника с ролью выше роли бота.")
                    if not me.guild_permissions.kick_members:
                        raise RuntimeError("У бота нет права исключать участников (Kick Members).")
                    dm_sent = await send_application_dm(
                        applicant, app_id=entry["id"], guild_name=guild.name,
                        status="rejected", direction=direction,
                    )
                    await applicant.kick(reason=f"Заявка #{entry['id']} отклонена {interaction.user}")
                decision = "rejected"
            self.bot.db.finish_application(entry["id"], decision, interaction.user.id)
        except (discord.Forbidden, discord.HTTPException, RuntimeError) as exc:
            self.bot.db.revert_application(entry["id"])
            LOG.warning("Moderation failed: %s", exc)
            return await say(interaction, f"❌ Решение не выполнено: {exc}")
        except Exception:
            self.bot.db.revert_application(entry["id"])
            LOG.exception("Unexpected application moderation failure")
            return await say(interaction, "❌ Внутренняя ошибка. Проверьте логи.")

        if accepted or applicant is None:
            applicant_user = applicant
            if applicant_user is None:
                try:
                    applicant_user = await self.bot.fetch_user(entry["applicant_id"])
                except discord.HTTPException:
                    applicant_user = None
            if applicant_user is not None:
                role_names = (f"{selected_role.name} + {common_role.name}" if accepted else None)
                dm_sent = await send_application_dm(
                    applicant_user, app_id=entry["id"], guild_name=guild.name,
                    status=decision, direction=direction, role_name=role_names,
                )
        await write_application_log(
            self.bot, guild, self.bot.db.application_by_id(entry["id"]),
            event=decision, actor_id=interaction.user.id, dm_sent=dm_sent,
            note=(f"Выданы роли: {selected_role.name}, {common_role.name}" if accepted else None),
        )
        await safely_delete_application_thread(
            self.bot, guild, entry, f"Заявка #{entry['id']} {decision}",
        )
        await say(interaction, f"{'✅ Принята' if accepted else '❌ Отклонена'}. "
                  "Решение сохранено в журнале, удаление ветки выполнено при наличии доступа."
                  + (" ЛС не доставлено." if not dm_sent else ""))

    @discord.ui.button(label="Принять", emoji="✅", style=discord.ButtonStyle.success,
                       custom_id="avb:application:accept")
    async def accept(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self.decide(interaction, True)

    @discord.ui.button(label="Отклонить", emoji="❌", style=discord.ButtonStyle.danger,
                       custom_id="avb:application:reject")
    async def reject(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self.decide(interaction, False)

    @discord.ui.button(label="Вызвать на обзвон", emoji="📞", style=discord.ButtonStyle.primary,
                       custom_id="avb:application:call")
    async def call(self, interaction: discord.Interaction, button: discord.ui.Button):
        context = await self.context(interaction)
        if context is None:
            return
        guild, settings, entry = context
        channel_id = settings["call_channel_id"]
        channel = guild.get_channel(channel_id) if channel_id else None
        if not isinstance(channel, discord.VoiceChannel):
            return await say(interaction, "❌ Сначала назначьте голосовой канал через /menu → Канал обзвона.")
        applicant = await get_member(guild, entry["applicant_id"])
        if applicant is None:
            return await say(interaction, "Кандидат покинул сервер. Добавить его в ветку нельзя.")
        if not channel.permissions_for(applicant).view_channel or not channel.permissions_for(applicant).connect:
            return await say(interaction, "У кандидата нет доступа к голосовому каналу обзвона.")
        if entry["status"] != "pending":
            return await say(interaction, "Обзвон уже назначен или заявка находится на рассмотрении.")
        deadline = int(time.time()) + 300
        if not self.bot.db.start_call(entry["id"], channel.id, interaction.user.id, deadline):
            return await say(interaction, "Статус заявки изменился. Повторите действие при необходимости.")
        await interaction.response.defer(ephemeral=True, thinking=True)
        thread = interaction.channel
        try:
            if not isinstance(thread, discord.Thread):
                raise RuntimeError("Канал заявки больше не является веткой.")
            await thread.add_user(applicant)
            await thread.send(
                content=(f"{applicant.mention}, **вас вызвали на обзвон!**\n"
                         f"Зайдите в голосовой канал {channel.mention} в течение **5 минут** "
                         "(<t:" + str(deadline) + ":R>), иначе ваша заявка будет отклонена "
                         "— без исключения с сервера."),
                allowed_mentions=discord.AllowedMentions(everyone=False, roles=False,
                                                          users=[applicant], replied_user=False),
            )
            if not self.bot.db.activate_call(entry["id"]):
                raise RuntimeError("Не удалось подтвердить вызов в базе данных.")
        except (discord.HTTPException, RuntimeError, sqlite3.Error) as exc:
            self.bot.db.cancel_call(entry["id"])
            LOG.warning("Could not call applicant: %s", exc)
            return await say(interaction, f"❌ Не удалось добавить кандидата или отправить вызов: {exc}")
        dm_sent = await send_application_dm(
            applicant, app_id=entry["id"], guild_name=guild.name,
            status="called", call_channel=channel,
        )
        await write_application_log(
            self.bot, guild, self.bot.db.application_by_id(entry["id"]),
            event="called", actor_id=interaction.user.id, dm_sent=dm_sent,
            note=f"Время истечения: <t:{deadline}:F>",
        )
        # If already in the designated channel, immediately count this as attendance.
        if applicant.voice and applicant.voice.channel and applicant.voice.channel.id == channel.id:
            await self.bot.record_call_arrival(guild, applicant, channel)
        await say(interaction, f"📞 Кандидат добавлен в ветку и вызван в {channel.mention}. "
                  "При отсутствии в канале через 5 минут заявка будет отклонена без кика.")


class RoomModal(discord.ui.Modal):
    LABELS = {
        "rename": "Переименовать комнату",
        "grant": "Добавить пользователя",
        "remove": "Убрать пользователя",
        "block": "Заблокировать пользователя",
        "unblock": "Разблокировать пользователя",
        "transfer": "Передать комнату",
    }

    def __init__(self, bot: "ApprovalVoiceBot", action: str):
        super().__init__(title=self.LABELS[action])
        self.bot = bot
        self.action = action
        self.field = discord.ui.TextInput(
            label="Новое название" if action == "rename" else "ID пользователя или @упоминание",
            placeholder=("Например, Комната друзей" if action == "rename"
                         else "123456789012345678 или <@123456789012345678>"),
            min_length=1, max_length=75 if action == "rename" else 32,
        )
        self.add_item(self.field)

    async def on_submit(self, interaction: discord.Interaction):
        channel = await require_owner_room(self.bot, interaction)
        if channel is None:
            return
        await interaction.response.defer(ephemeral=True, thinking=True)
        try:
            guild = channel.guild
            if self.action == "rename":
                new_name = self.field.value.strip()
                if not 2 <= len(new_name) <= 75:
                    return await say(interaction, "Название должно содержать 2–75 символов.")
                old_name = channel.name
                await channel.edit(name=new_name, reason=f"Изменение названия владельцем {interaction.user.id}")
                await write_voice_log(self.bot, guild, "✏️ Переименование комнаты",
                                      f"Владелец: {interaction.user.mention}\n"
                                      f"Было: `{discord.utils.escape_markdown(old_name)}`\n"
                                      f"Стало: `{discord.utils.escape_markdown(new_name)}`")
                return await say(interaction, f"✏️ Канал переименован: **{discord.utils.escape_markdown(new_name)}**")

            target_id = parse_user_id(self.field.value)
            if target_id is None:
                return await say(interaction, "Укажите числовой Discord ID или упоминание вида <@ID>.")
            target = await get_member(guild, target_id)
            if target is None:
                return await say(interaction, "Участник с этим ID не найден на сервере.")
            if target.id == interaction.user.id:
                return await say(interaction, "Нельзя изменять собственный доступ таким способом.")
            if target.bot:
                return await say(interaction, "Для управления ботами обратитесь к администратору.")

            if self.action == "grant":
                overwrite = channel.overwrites_for(target)
                overwrite.view_channel = True
                overwrite.connect = True
                await channel.set_permissions(target, overwrite=overwrite,
                                              reason=f"Разрешение владельца {interaction.user.id}")
                result = f"✅ {target.mention} получил доступ к комнате."
            elif self.action == "remove":
                await channel.set_permissions(target, overwrite=None,
                                              reason=f"Отзыв разрешения владельцем {interaction.user.id}")
                if target.voice and target.voice.channel and target.voice.channel.id == channel.id:
                    await target.move_to(None, reason="Владелец убрал пользователя из комнаты")
                result = (f"👤 Доступ {target.mention} сброшен до стандартных прав. "
                          "В открытой комнате пользователь сможет зайти снова.")
            elif self.action == "block":
                overwrite = channel.overwrites_for(target)
                overwrite.view_channel = False
                overwrite.connect = False
                await channel.set_permissions(target, overwrite=overwrite,
                                              reason=f"Блокировка владельцем {interaction.user.id}")
                if target.voice and target.voice.channel and target.voice.channel.id == channel.id:
                    await target.move_to(None, reason="Блокировка в голосовой комнате")
                result = f"⛔ {target.mention} заблокирован в этой комнате (не на сервере)."
            elif self.action == "unblock":
                await channel.set_permissions(target, overwrite=None,
                                              reason=f"Разблокировка владельцем {interaction.user.id}")
                result = f"♻️ Ограничения для {target.mention} сняты."
            elif self.action == "transfer":
                if not target.voice or not target.voice.channel or target.voice.channel.id != channel.id:
                    return await say(interaction, "Передать комнату можно только участнику, который уже находится в ней.")
                if self.bot.db.owned_room(guild.id, target.id):
                    return await say(interaction, "У пользователя уже есть своя временная комната.")
                await channel.set_permissions(target, view_channel=True, connect=True,
                                              reason="Новый владелец голосовой комнаты")
                try:
                    self.bot.db.transfer_room(channel.id, target.id)
                except sqlite3.IntegrityError:
                    return await say(interaction, "Пользователь уже владеет другой комнатой.")
                try:
                    await channel.set_permissions(interaction.user, overwrite=None,
                                                  reason="Передача владения комнатой")
                except discord.HTTPException:
                    LOG.exception("Could not reset former room owner's overwrite")
                result = f"🤝 Владельцем комнаты теперь является {target.mention}."
            else:
                return await say(interaction, "Неизвестное действие.")
            await write_voice_log(self.bot, guild, "🛠️ Изменение комнаты",
                                  f"Комната: {channel.mention}\nВладелец: {interaction.user.mention}\n"
                                  f"Действие: `{self.action}`\nУчастник: {target.mention}")
            await say(interaction, result)
        except discord.HTTPException as exc:
            LOG.warning("Room modal action failed: %s", exc)
            await say(interaction, f"Ошибка изменения комнаты: {exc}")


class ConfirmDeleteRoom(discord.ui.View):
    def __init__(self, bot: "ApprovalVoiceBot", user_id: int):
        super().__init__(timeout=45)
        self.bot = bot
        self.user_id = user_id

    @discord.ui.button(label="Да, удалить", style=discord.ButtonStyle.danger)
    async def confirm(self, interaction: discord.Interaction, button: discord.ui.Button):
        if interaction.user.id != self.user_id:
            return await say(interaction, "Это подтверждение предназначено не вам.")
        channel = await require_owner_room(self.bot, interaction)
        if channel is None:
            return
        await interaction.response.defer(ephemeral=True, thinking=True)
        try:
            await channel.delete(reason=f"Владелец {interaction.user.id} удалил голосовую комнату")
            self.bot.db.remove_room(channel.id)
            await write_voice_log(self.bot, interaction.guild, "🗑️ Комната удалена вручную",
                                  f"Канал: `{channel.name}` (`{channel.id}`)\nВладелец: {interaction.user.mention}")
            await say(interaction, "🗑️ Комната удалена. Участники отключены от неё.")
        except discord.HTTPException as exc:
            await say(interaction, f"Не удалось удалить канал: {exc}")
        self.stop()

    @discord.ui.button(label="Отмена", style=discord.ButtonStyle.secondary)
    async def cancel(self, interaction: discord.Interaction, button: discord.ui.Button):
        if interaction.user.id != self.user_id:
            return await say(interaction, "Это подтверждение предназначено не вам.")
        await say(interaction, "Удаление отменено.")
        self.stop()


class VoiceControlView(discord.ui.View):
    def __init__(self, bot: "ApprovalVoiceBot"):
        super().__init__(timeout=None)
        self.bot = bot

    async def adjust(self, interaction: discord.Interaction, action: str):
        channel = await require_owner_room(self.bot, interaction)
        if channel is None:
            return
        await interaction.response.defer(ephemeral=True, thinking=True)
        try:
            if action == "plus":
                new_limit = 2 if channel.user_limit == 0 else min(channel.user_limit + 1, 99)
                await channel.edit(user_limit=new_limit)
                result = f"➕ Лимит участников: {new_limit}."
            elif action == "minus":
                new_limit = 0 if channel.user_limit <= 1 else channel.user_limit - 1
                await channel.edit(user_limit=new_limit)
                result = f"➖ Лимит участников: {new_limit or 'без ограничений'}."
            else:
                role = channel.guild.default_role
                overwrite = channel.overwrites_for(role)
                if action == "open":
                    overwrite.connect = True
                    result = "🔓 Вход в комнату открыт."
                elif action == "close":
                    overwrite.connect = False
                    result = "🔒 Вход закрыт для остальных. Приглашённые участники могут войти."
                elif action == "hide":
                    overwrite.view_channel = False
                    result = "🙈 Комната скрыта от обычных участников."
                elif action == "show":
                    overwrite.view_channel = True
                    result = "👁️ Комната видна участникам сервера."
                else:
                    return await say(interaction, "Неизвестная настройка.")
                await channel.set_permissions(role, overwrite=overwrite,
                                              reason=f"Изменение настройки владельцем {interaction.user.id}")
            await write_voice_log(self.bot, channel.guild, "⚙️ Настройки комнаты",
                                  f"Комната: {channel.mention}\nВладелец: {interaction.user.mention}\n"
                                  f"Действие: `{action}`")
            await say(interaction, result)
        except discord.HTTPException as exc:
            LOG.warning("Room edit failed: %s", exc)
            await say(interaction, f"Не удалось изменить настройки: {exc}")

    async def prompt(self, interaction: discord.Interaction, action: str):
        channel = await require_owner_room(self.bot, interaction)
        if channel is not None:
            await interaction.response.send_modal(RoomModal(self.bot, action))

    @discord.ui.button(label="➕", row=0, style=discord.ButtonStyle.secondary, custom_id="avb:room:plus")
    async def plus(self, i: discord.Interaction, b: discord.ui.Button): await self.adjust(i, "plus")

    @discord.ui.button(label="➖", row=0, style=discord.ButtonStyle.secondary, custom_id="avb:room:minus")
    async def minus(self, i: discord.Interaction, b: discord.ui.Button): await self.adjust(i, "minus")

    @discord.ui.button(label="🔓", row=0, style=discord.ButtonStyle.secondary, custom_id="avb:room:open")
    async def open_room(self, i: discord.Interaction, b: discord.ui.Button): await self.adjust(i, "open")

    @discord.ui.button(label="🔒", row=0, style=discord.ButtonStyle.secondary, custom_id="avb:room:close")
    async def close_room(self, i: discord.Interaction, b: discord.ui.Button): await self.adjust(i, "close")

    @discord.ui.button(label="✏️", row=0, style=discord.ButtonStyle.secondary, custom_id="avb:room:rename")
    async def rename(self, i: discord.Interaction, b: discord.ui.Button): await self.prompt(i, "rename")

    @discord.ui.button(label="👤+", row=1, style=discord.ButtonStyle.secondary, custom_id="avb:room:grant")
    async def grant(self, i: discord.Interaction, b: discord.ui.Button): await self.prompt(i, "grant")

    @discord.ui.button(label="👤−", row=1, style=discord.ButtonStyle.secondary, custom_id="avb:room:remove")
    async def remove(self, i: discord.Interaction, b: discord.ui.Button): await self.prompt(i, "remove")

    @discord.ui.button(label="⛔", row=1, style=discord.ButtonStyle.secondary, custom_id="avb:room:block")
    async def block(self, i: discord.Interaction, b: discord.ui.Button): await self.prompt(i, "block")

    @discord.ui.button(label="♻️", row=1, style=discord.ButtonStyle.secondary, custom_id="avb:room:unblock")
    async def unblock(self, i: discord.Interaction, b: discord.ui.Button): await self.prompt(i, "unblock")

    @discord.ui.button(label="🤝", row=1, style=discord.ButtonStyle.secondary, custom_id="avb:room:transfer")
    async def transfer(self, i: discord.Interaction, b: discord.ui.Button): await self.prompt(i, "transfer")

    @discord.ui.button(label="🙈", row=2, style=discord.ButtonStyle.secondary, custom_id="avb:room:hide")
    async def hide(self, i: discord.Interaction, b: discord.ui.Button): await self.adjust(i, "hide")

    @discord.ui.button(label="👁️", row=2, style=discord.ButtonStyle.secondary, custom_id="avb:room:show")
    async def show(self, i: discord.Interaction, b: discord.ui.Button): await self.adjust(i, "show")

    @discord.ui.button(label="🗑️", row=2, style=discord.ButtonStyle.danger, custom_id="avb:room:delete")
    async def delete(self, interaction: discord.Interaction, button: discord.ui.Button):
        channel = await require_owner_room(self.bot, interaction)
        if channel is not None:
            await interaction.response.send_message(
                f"Удалить **{discord.utils.escape_markdown(channel.name)}**? Все участники будут отключены.",
                view=ConfirmDeleteRoom(self.bot, interaction.user.id), ephemeral=True,
            )


class ApprovalVoiceBot(commands.Bot):
    def __init__(self):
        intents = discord.Intents.default()
        intents.guilds = True
        intents.members = True  # Privileged Server Members Intent: enable in Developer Portal.
        intents.voice_states = True
        super().__init__(command_prefix=commands.when_mentioned, intents=intents)
        self.db = Storage(DATABASE_PATH)
        self.voice_lock = asyncio.Lock()
        self._rooms_reconciled = False
        self._legacy_commands_cleaned = False
        self._call_task: asyncio.Task | None = None

    async def setup_hook(self):
        # Release operations left half-finished by a crashed previous process.
        self.db.recover_incomplete_operations()
        # These views work even for messages that were sent before the last restart.
        self.add_view(ApplicationPanelView(self))
        self.add_view(ReviewView(self))
        self.add_view(VoiceControlView(self))
        self._call_task = asyncio.create_task(self.call_timeout_loop(), name="application-call-timeouts")
        if GUILD_ID:
            guild = discord.Object(id=GUILD_ID)
            self.tree.copy_global_to(guild=guild)
            synced = await self.tree.sync(guild=guild)
            LOG.info("Synced %s slash commands to server %s", len(synced), GUILD_ID)
        else:
            synced = await self.tree.sync()
            LOG.info("Synced %s global slash commands", len(synced))

    async def on_ready(self):
        LOG.info("Bot logged in as %s (%s)", self.user, self.user.id if self.user else "?")
        if not self._rooms_reconciled:
            self._rooms_reconciled = True
            await self.clean_rooms_after_restart()
        if not self._legacy_commands_cleaned and not GUILD_ID:
            self._legacy_commands_cleaned = True
            asyncio.create_task(self.remove_old_guild_scoped_commands(),
                                name="remove-legacy-local-slash-commands")

    async def remove_old_guild_scoped_commands(self):
        """Remove old per-guild /setup_server etc. after switching to global /menu.

        A former guild-scoped registration can shadow a new global command.
        Only guilds already in this bot's own configuration database are touched.
        """
        for guild_id in self.db.configured_guild_ids():
            if self.get_guild(guild_id) is None:
                continue
            target = discord.Object(id=guild_id)
            try:
                self.tree.clear_commands(guild=target)
                await self.tree.sync(guild=target)
                LOG.info("Removed legacy guild-scoped commands on %s", guild_id)
            except discord.HTTPException:
                LOG.exception("Could not clean legacy slash commands on %s", guild_id)

    async def clean_rooms_after_restart(self):
        # Reconcile voice rooms left empty while the bot was offline.
        for data in self.db.all_rooms():
            guild = self.get_guild(data["guild_id"])
            if guild is None or guild.unavailable:
                continue
            channel = guild.get_channel(data["channel_id"])
            if channel is None:
                self.db.remove_room(data["channel_id"])
            elif isinstance(channel, discord.VoiceChannel) and not any(not m.bot for m in channel.members):
                try:
                    await channel.delete(reason="Очистка пустой комнаты после перезапуска")
                    self.db.remove_room(channel.id)
                except discord.NotFound:
                    self.db.remove_room(channel.id)
                except discord.HTTPException:
                    LOG.exception("Unable to clean empty room %s", channel.id)

    async def close(self):
        if self._call_task and not self._call_task.done():
            self._call_task.cancel()
            try:
                await self._call_task
            except asyncio.CancelledError:
                pass
        await super().close()
        self.db.close()

    async def record_call_arrival(self, guild: discord.Guild, member: discord.Member,
                                  channel: discord.VoiceChannel):
        entry = self.db.mark_call_joined(guild.id, member.id, channel.id, int(time.time()))
        if entry is None:
            return
        try:
            thread = guild.get_thread(entry["thread_id"]) or await guild.fetch_channel(entry["thread_id"])
            if isinstance(thread, discord.Thread):
                await thread.send(
                    f"✅ {member.mention} прибыл на обзвон в {channel.mention} вовремя. "
                    "Автоматическое отклонение отменено. Ожидается решение администрации.",
                    allowed_mentions=discord.AllowedMentions(everyone=False, roles=False,
                                                              users=[member], replied_user=False),
                )
        except discord.HTTPException:
            LOG.exception("Could not notify arrival in application thread %s", entry['id'])
        await write_application_log(self, guild, entry, event="arrived",
                                    actor_id=entry["call_requested_by"])
        await send_application_dm(member, app_id=entry["id"], guild_name=guild.name,
                                  status="arrived")

    async def call_timeout_loop(self):
        await self.wait_until_ready()
        while not self.is_closed():
            try:
                now = int(time.time())
                for entry in self.db.expired_calls(now):
                    await self.process_expired_call(entry)
            except asyncio.CancelledError:
                raise
            except Exception:
                LOG.exception("Error checking application call deadlines")
            await asyncio.sleep(10)

    async def process_expired_call(self, entry):
        guild = self.get_guild(entry["guild_id"])
        if guild is None or guild.unavailable:
            # Wait until we can verify membership instead of guessing.
            return
        applicant = await get_member(guild, entry["applicant_id"])
        if (applicant is not None and applicant.voice and applicant.voice.channel and
                applicant.voice.channel.id == entry["call_channel_id"]):
            # If the candidate is in the required voice channel, err on their side
            # in case a gateway event was lost. Do not auto-reject them.
            saved = self.db.mark_call_joined(guild.id, applicant.id,
                                             entry["call_channel_id"], entry["call_deadline"])
            if saved:
                await write_application_log(self, guild, saved, event="arrived",
                                            actor_id=entry["call_requested_by"],
                                            note="Подключение подтверждено при проверке таймера")
            return
        # A deleted interview channel must never trigger an unfair rejection.
        call_channel = guild.get_channel(entry["call_channel_id"])
        if not isinstance(call_channel, discord.VoiceChannel):
            if self.db.claim_application(entry["id"]):
                self.db.mark_call_timeout_failed(entry["id"])
                await write_application_log(
                    self, guild, self.db.application_by_id(entry["id"]),
                    event="timeout_failed", note="Канал обзвона удалён: нужна ручная проверка",
                )
            return
        # The timer only rejects the application. It NEVER removes a guild member.
        # A single SQLite transition prevents races with a moderator or voice arrival.
        if not self.db.expire_call_without_kick(entry["id"], int(time.time())):
            return
        refreshed = self.db.application_by_id(entry["id"])
        applicant_user = applicant
        if applicant_user is None:
            try:
                applicant_user = await self.fetch_user(entry["applicant_id"])
            except discord.HTTPException:
                applicant_user = None
        dm_sent = False
        if applicant_user is not None:
            dm_sent = await send_application_dm(
                applicant_user, app_id=entry["id"], guild_name=guild.name,
                status="missed_call", direction=entry["direction"],
            )
        await write_application_log(self, guild, refreshed, event="missed_call",
                                    dm_sent=dm_sent,
                                    note="Неявка за 5 минут: заявка закрыта БЕЗ исключения с сервера")
        await safely_delete_application_thread(self, guild, refreshed,
                                               "Неявка на обзвон — без исключения")
        LOG.info("Application %s auto-rejected without kick after call timeout", entry["id"])

    async def on_guild_channel_delete(self, channel: discord.abc.GuildChannel):
        if isinstance(channel, discord.VoiceChannel):
            self.db.remove_room(channel.id)

    async def on_raw_thread_delete(self, payload: discord.RawThreadDeleteEvent):
        # If a moderator manually deleted a pending thread, unblock re-application.
        self.db.reset_missing_thread(payload.thread_id)

    async def on_voice_state_update(self, member: discord.Member, before: discord.VoiceState,
                                    after: discord.VoiceState):
        if member.bot or before.channel == after.channel:
            return
        if isinstance(after.channel, discord.VoiceChannel):
            await self.record_call_arrival(member.guild, member, after.channel)
        settings = self.db.config(member.guild.id)
        if settings is None:
            return
        async with self.voice_lock:
            # Join-to-create lobby.
            if after.channel is not None and after.channel.id == settings["lobby_channel_id"]:
                await self.create_temporary_room(member, settings)
            # Automatic clean-up or transfer when somebody leaves a temporary room.
            if before.channel is not None:
                data = self.db.room(before.channel.id)
                if data is not None:
                    await self.handle_room_departure(member, before.channel, data)

    async def create_temporary_room(self, member: discord.Member, settings):
        guild = member.guild
        if not member.voice or not member.voice.channel or member.voice.channel.id != settings["lobby_channel_id"]:
            return
        existing = self.db.owned_room(guild.id, member.id)
        if existing:
            old_channel = guild.get_channel(existing["channel_id"])
            if isinstance(old_channel, discord.VoiceChannel):
                try:
                    await member.move_to(old_channel, reason="Возврат в существующую личную комнату")
                except discord.HTTPException:
                    LOG.exception("Could not move user to existing room")
                return
            self.db.remove_room(existing["channel_id"])

        category = guild.get_channel(settings["voice_category_id"])
        if not isinstance(category, discord.CategoryChannel):
            LOG.error("Voice category not found on guild %s", guild.id)
            return
        moderator_role = guild.get_role(settings["moderator_role_id"])
        me = guild.me
        if me is None:
            LOG.error("Bot member not cached on guild %s", guild.id)
            return
        overwrites = {
            guild.default_role: discord.PermissionOverwrite(view_channel=True, connect=True),
            member: discord.PermissionOverwrite(view_channel=True, connect=True),
            me: discord.PermissionOverwrite(view_channel=True, connect=True,
                                            manage_channels=True, move_members=True),
        }
        if moderator_role:
            overwrites[moderator_role] = discord.PermissionOverwrite(view_channel=True, connect=True)
        room = None
        try:
            room = await category.create_voice_channel(
                name=f"🔊・{member.display_name[:70]}", user_limit=2, overwrites=overwrites,
                reason=f"Временная комната для {member.id}",
            )
            self.db.create_room(room.id, guild.id, member.id)
            await member.move_to(room, reason="Создание собственной голосовой комнаты")
            LOG.info("Created voice room %s for %s", room.id, member.id)
            await write_voice_log(self, guild, "🎙️ Комната создана",
                                  f"Владелец: {member.mention}\nКанал: {room.mention}")
        except (discord.HTTPException, sqlite3.Error):
            LOG.exception("Unable to create or enter temporary room")
            if room is not None:
                self.db.remove_room(room.id)
                try:
                    await room.delete(reason="Ошибка создания временной комнаты")
                except discord.HTTPException:
                    pass

    async def handle_room_departure(self, member: discord.Member,
                                    channel: discord.abc.GuildChannel, data):
        if not isinstance(channel, discord.VoiceChannel):
            return
        # The event fires after the voice member lists are updated.
        remaining = [m for m in channel.members if not m.bot]
        if not remaining:
            try:
                await channel.delete(reason="Временная комната опустела")
                self.db.remove_room(channel.id)
                LOG.info("Deleted empty room %s", channel.id)
                await write_voice_log(self, member.guild, "🗑️ Комната опустела",
                                      f"Канал: `{channel.name}` (`{channel.id}`)")
            except discord.NotFound:
                self.db.remove_room(channel.id)
            except discord.HTTPException:
                LOG.exception("Could not delete empty room %s", channel.id)
            return
        if member.id != data["owner_id"]:
            return
        # Preserve an occupied room when its owner leaves.
        new_owner = next(
            (m for m in remaining if self.db.owned_room(channel.guild.id, m.id) is None), None
        )
        if new_owner is None:
            return
        try:
            await channel.set_permissions(new_owner, view_channel=True, connect=True,
                                          reason="Автоматическая передача комнаты после выхода владельца")
            self.db.transfer_room(channel.id, new_owner.id)
            await channel.set_permissions(member, overwrite=None,
                                          reason="Прежний владелец покинул комнату")
            LOG.info("Room %s transferred to %s", channel.id, new_owner.id)
            await write_voice_log(self, member.guild, "🤝 Передан владелец",
                                  f"Комната: {channel.mention}\nНовый владелец: {new_owner.mention}")
        except (discord.HTTPException, sqlite3.IntegrityError):
            LOG.exception("Room %s could not be reassigned", channel.id)



# ---------------------------------------------------------------------------
# /menu: ephemeral configuration UI; every callback re-checks administrator
# privileges.  No configuration menu is persistent or visible to the public.
# ---------------------------------------------------------------------------

def config_overview(bot: "ApprovalVoiceBot", guild: discord.Guild) -> str:
    cfg = bot.db.config(guild.id)
    if cfg is None:
        return ("🛠️ **DYNASTY • Меню администратора**\n"
                "Сервер ещё не настроен. Нажмите **«Первичная настройка»**, "
                "выберите 4 роли и подтвердите создание каналов.")
    def role_name(key):
        r = guild.get_role(cfg[key]) if cfg[key] else None
        return r.mention if r else "⚠️ не задана"
    def channel_name(key):
        c = guild.get_channel(cfg[key]) if cfg[key] else None
        return c.mention if c else "⚠️ канал отсутствует"
    pings = bot.db.application_ping_role_ids(guild.id)
    return ("🛠️ **DYNASTY • Меню администратора**\n"
            f"**Crime:** {role_name('crime_role_id')} | **State:** {role_name('state_role_id')}\n"
            f"**Общая роль:** {role_name('common_role_id')}\n"
            f"**Модераторы:** {role_name('moderator_role_id')} | **Теги:** {len(pings)}/50\n"
            f"**Канал обзвона:** {channel_name('call_channel_id')}\n"
            f"**Заявки:** {channel_name('application_channel_id')}\n"
            f"**Пульт комнат:** {channel_name('control_channel_id')}\n"
            f"**Журнал заявок:** {channel_name('log_channel_id')}\n"
            f"**Журнал комнат:** {channel_name('voice_log_channel_id')}\n\n"
            "Выберите раздел ниже. Повторная публикация не затрагивает другие панели.")


class AdminOnlyView(discord.ui.View):
    def __init__(self, bot: "ApprovalVoiceBot", user_id: int):
        super().__init__(timeout=600)
        self.bot = bot
        self.user_id = user_id

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if (interaction.guild is None or interaction.user.id != self.user_id
                or not isinstance(interaction.user, discord.Member)
                or not interaction.user.guild_permissions.administrator):
            await say(interaction, "❌ Настраивать бота может только администратор, открывший это меню.")
            return False
        return True

    @discord.ui.button(label="← В меню", style=discord.ButtonStyle.secondary, row=4)
    async def back(self, interaction: discord.Interaction, _: discord.ui.Button):
        await interaction.response.edit_message(
            content=config_overview(self.bot, interaction.guild),
            view=AdminHomeView(self.bot, self.user_id),
        )


class MenuRoleSelect(discord.ui.RoleSelect):
    def __init__(self, key: str, label: str, *, row: int):
        super().__init__(placeholder=label, min_values=1, max_values=1, row=row)
        self.key = key

    async def callback(self, interaction: discord.Interaction):
        parent: AdminRolesView = self.view
        parent.chosen[self.key] = self.values[0]
        selections = ", ".join(f"{key}: {role.name}" for key, role in parent.chosen.items())
        await interaction.response.edit_message(
            content=f"🔧 **{parent.title}**\nВыбрано: {selections}\nНажмите «Сохранить». ",
            view=parent,
        )


class AdminRolesView(AdminOnlyView):
    def __init__(self, bot: "ApprovalVoiceBot", user_id: int, mode: str):
        self.mode = mode
        self.chosen: dict[str, discord.Role] = {}
        self.title = {
            "initial": "Первичная настройка (создание каналов)",
            "directions": "Роли Crime / State", "moderator": "Роль модераторов",
            "common": "Общая обязательная роль",
        }[mode]
        super().__init__(bot, user_id)
        fields = {
            "initial": [("crime", "Выберите роль Crime"), ("state", "Выберите роль State"),
                        ("moderator", "Выберите роль модераторов"),
                        ("common", "Выберите общую роль")],
            "directions": [("crime", "Выберите роль Crime"), ("state", "Выберите роль State")],
            "moderator": [("moderator", "Выберите роль модераторов")],
            "common": [("common", "Выберите общую роль")],
        }[mode]
        for index, (key, label) in enumerate(fields):
            self.add_item(MenuRoleSelect(key, label, row=index))

    @discord.ui.button(label="💾 Сохранить", style=discord.ButtonStyle.success, row=4)
    async def save(self, interaction: discord.Interaction, _: discord.ui.Button):
        guild = interaction.guild
        required = {"initial": {"crime", "state", "moderator", "common"},
                    "directions": {"crime", "state"}, "moderator": {"moderator"},
                    "common": {"common"}}[self.mode]
        if not required.issubset(self.chosen):
            return await say(interaction, "❌ Выберите все роли, затем нажмите «Сохранить».")
        roles = self.chosen
        if any(r.is_default() for r in roles.values()):
            return await say(interaction, "Нельзя выбрать роль @everyone.")
        cfg = self.bot.db.config(guild.id)
        if self.mode == "initial":
            if cfg is not None:
                return await say(interaction, "Сервер уже настроен. Используйте отдельные разделы меню.")
            if len({r.id for r in roles.values()}) != 4:
                return await say(interaction, "Выберите четыре разные роли.")
            await setup_server.callback(interaction, roles["crime"], roles["state"],
                                        roles["moderator"], roles["common"])
            return
        if cfg is None:
            return await say(interaction, "Сначала запустите первичную настройку через /menu.")
        if self.mode == "directions":
            return await set_application_roles.callback(interaction, roles["crime"], roles["state"])
        if self.mode == "common":
            return await set_application_common_role.callback(interaction, roles["common"])
        new_role = roles["moderator"]
        if new_role.id == cfg["moderator_role_id"]:
            return await say(interaction, "Эта роль уже назначена модераторской.")
        if new_role.id in (cfg["crime_role_id"], cfg["state_role_id"], cfg["common_role_id"]):
            return await say(interaction, "Роль модераторов должна отличаться от ролей кандидатов.")
        old_role = guild.get_role(cfg["moderator_role_id"])
        await interaction.response.defer(ephemeral=True, thinking=True)
        try:
            # Save first so prepare_application_channel applies overwrites to the NEW role.
            self.bot.db.set_moderator_role(guild.id, new_role.id)
            cfg = self.bot.db.config(guild.id)
            me = guild.me or await guild.fetch_member(self.bot.user.id)
            await prepare_application_channel(guild, cfg, me)
            for key in ("log_channel_id", "voice_log_channel_id", "control_channel_id"):
                channel = guild.get_channel(cfg[key]) if cfg[key] else None
                if isinstance(channel, discord.TextChannel):
                    new_ow = channel.overwrites_for(new_role)
                    new_ow.update(view_channel=True, send_messages=True, read_message_history=True)
                    await channel.set_permissions(new_role, overwrite=new_ow,
                                                  reason="Обновление роли модератора DYNASTY")
            if old_role:
                for key in ("application_channel_id", "log_channel_id",
                            "voice_log_channel_id", "control_channel_id"):
                    channel = guild.get_channel(cfg[key]) if cfg[key] else None
                    if isinstance(channel, discord.TextChannel):
                        await channel.set_permissions(old_role, overwrite=None,
                                                      reason="Удаление старого доступа модераторов DYNASTY")
        except (discord.HTTPException, sqlite3.Error, RuntimeError) as exc:
            LOG.exception("Could not fully change moderator role")
            return await say(interaction, f"⚠️ Роль сохранена, но доступы обновлены не полностью: {exc}")
        await say(interaction, f"✅ Роль модераторов изменена на {new_role.mention}.")


class MenuChannelSelect(discord.ui.ChannelSelect):
    def __init__(self):
        super().__init__(placeholder="Выберите голосовой канал обзвона",
                         channel_types=[discord.ChannelType.voice], min_values=1,
                         max_values=1, row=0)

    async def callback(self, interaction: discord.Interaction):
        parent: AdminCallChannelView = self.view
        selected = interaction.guild.get_channel(self.values[0].id)
        if not isinstance(selected, discord.VoiceChannel):
            return await say(interaction, "❌ Выберите существующий голосовой канал.")
        if parent.bot.db.config(interaction.guild.id) is None:
            return await say(interaction, "Сначала выполните первичную настройку через /menu.")
        try:
            parent.bot.db.set_call_channel(interaction.guild.id, selected.id)
        except (sqlite3.Error, ValueError) as exc:
            return await say(interaction, f"❌ Ошибка базы: {exc}")
        await interaction.response.edit_message(
            content=f"✅ Канал обзвона сохранён: {selected.mention}.\n"
                    "Через 5 минут без явки заявка будет отклонена **БЕЗ кика**.",
            view=AdminHomeView(parent.bot, parent.user_id),
        )


class AdminCallChannelView(AdminOnlyView):
    def __init__(self, bot: "ApprovalVoiceBot", user_id: int):
        super().__init__(bot, user_id)
        self.add_item(MenuChannelSelect())


class MenuPingSelect(discord.ui.RoleSelect):
    def __init__(self):
        super().__init__(placeholder="Выберите от 1 до 25 ролей за один раз",
                         min_values=1, max_values=25, row=0)

    async def callback(self, interaction: discord.Interaction):
        parent: AdminPingsView = self.view
        parent.selected = [role.id for role in self.values if not role.is_default()]
        await interaction.response.edit_message(
            content=(f"🔔 **Теги в новых заявках**\n"
                     f"Выбрано ролей: {len(parent.selected)}. "
                     "Нажмите «Добавить» либо «Убрать». "
                     "Можно сделать несколько подходов по 25 ролей, всего до 50."),
            view=parent,
        )


class AdminPingsView(AdminOnlyView):
    def __init__(self, bot: "ApprovalVoiceBot", user_id: int):
        super().__init__(bot, user_id)
        self.selected: list[int] = []
        self.add_item(MenuPingSelect())

    async def update(self, interaction: discord.Interaction, operation: str):
        guild_id = interaction.guild.id
        if self.bot.db.config(guild_id) is None:
            return await say(interaction, "Сначала выполните первичную настройку через /menu.")
        existing = self.bot.db.application_ping_role_ids(guild_id)
        if operation != "clear" and not self.selected:
            return await say(interaction, "Сначала выберите роли из списка выше.")
        if operation == "add":
            merged = list(dict.fromkeys(existing + self.selected))
        elif operation == "remove":
            merged = [r for r in existing if r not in self.selected]
        else:
            merged = []
        if len(merged) > 50:
            return await say(interaction, "❌ Всего не более 50 ролей. Уберите лишние.")
        try:
            self.bot.db.set_application_ping_roles(guild_id, merged)
        except (sqlite3.Error, ValueError) as exc:
            return await say(interaction, f"❌ Не удалось сохранить: {exc}")
        self.selected.clear()
        await interaction.response.edit_message(
            content=(f"✅ Сохранено **{len(merged)}** ролей для упоминания. "
                     "Теги появятся только в новых заявках.\n"
                     "Для уведомлений ролям нужен доступ к приватным веткам; "
                     "у бота должно быть право упоминать эти роли."),
            view=AdminPingsView(self.bot, self.user_id),
        )

    @discord.ui.button(label="➕ Добавить", style=discord.ButtonStyle.success, row=1)
    async def add(self, i: discord.Interaction, _: discord.ui.Button):
        await self.update(i, "add")

    @discord.ui.button(label="➖ Убрать", style=discord.ButtonStyle.secondary, row=1)
    async def remove(self, i: discord.Interaction, _: discord.ui.Button):
        await self.update(i, "remove")

    @discord.ui.button(label="🧹 Очистить теги", style=discord.ButtonStyle.danger, row=1)
    async def clear(self, i: discord.Interaction, _: discord.ui.Button):
        await self.update(i, "clear")


class MenuUserSelect(discord.ui.UserSelect):
    def __init__(self):
        super().__init__(placeholder="Выберите кандидата с зависшей заявкой",
                         min_values=1, max_values=1, row=0)

    async def callback(self, interaction: discord.Interaction):
        parent: AdminResetView = self.view
        user = self.values[0]
        candidate = interaction.guild.get_member(user.id)
        if candidate is None:
            try:
                candidate = await interaction.guild.fetch_member(user.id)
            except discord.HTTPException:
                return await say(interaction, "Участник больше не находится на сервере.")
        parent.selected_member = candidate
        await interaction.response.edit_message(
            content=f"⚠️ Выбрана заявка {candidate.mention}. Нажмите «Сбросить заявку» для подтверждения.",
            view=parent,
        )


class AdminResetView(AdminOnlyView):
    def __init__(self, bot: "ApprovalVoiceBot", user_id: int):
        super().__init__(bot, user_id)
        self.selected_member: discord.Member | None = None
        self.add_item(MenuUserSelect())

    @discord.ui.button(label="🧹 Сбросить заявку", style=discord.ButtonStyle.danger, row=1)
    async def confirm(self, interaction: discord.Interaction, _: discord.ui.Button):
        if self.selected_member is None:
            return await say(interaction, "Сначала выберите участника.")
        await reset_application.callback(interaction, self.selected_member)
        self.selected_member = None


def staff_permissions(guild: discord.Guild, me: discord.Member,
                      moderator: discord.Role | None) -> dict:
    permissions = {
        guild.default_role: discord.PermissionOverwrite(view_channel=False),
        me: discord.PermissionOverwrite(view_channel=True, send_messages=True,
                                        read_message_history=True, embed_links=True),
    }
    if moderator is not None:
        permissions[moderator] = discord.PermissionOverwrite(
            view_channel=True, send_messages=True, read_message_history=True,
        )
    return permissions


def public_panel_permissions(guild: discord.Guild, me: discord.Member,
                             moderator: discord.Role | None) -> dict:
    permissions = {
        guild.default_role: discord.PermissionOverwrite(
            view_channel=True, send_messages=False, read_message_history=True,
            create_private_threads=False, create_public_threads=False,
            send_messages_in_threads=False,
        ),
        me: discord.PermissionOverwrite(
            view_channel=True, send_messages=True, read_message_history=True,
            embed_links=True, attach_files=True, create_private_threads=True,
            send_messages_in_threads=True, manage_threads=True,
        ),
    }
    if moderator is not None:
        permissions[moderator] = discord.PermissionOverwrite(
            view_channel=True, manage_threads=True, read_message_history=True,
            send_messages_in_threads=True,
        )
    return permissions


async def ensure_application_category(bot: "ApprovalVoiceBot", guild: discord.Guild):
    cfg = bot.db.config(guild.id)
    for key in ("application_channel_id", "log_channel_id"):
        channel = guild.get_channel(cfg[key]) if cfg[key] else None
        if isinstance(channel, discord.TextChannel) and channel.category:
            return channel.category
    return (discord.utils.get(guild.categories, name="📨・Заявки")
            or await guild.create_category("📨・Заявки", reason="Восстановление категории заявок"))


async def ensure_voice_category(bot: "ApprovalVoiceBot", guild: discord.Guild):
    cfg = bot.db.config(guild.id)
    channel = guild.get_channel(cfg["voice_category_id"])
    if isinstance(channel, discord.CategoryChannel):
        return channel
    new = await guild.create_category("🎧・Приватные комнаты", reason="Восстановление голосовой категории")
    bot.db.update_channel_ids(guild.id, voice_category_id=new.id)
    return new


async def refresh_panel(bot: "ApprovalVoiceBot", channel: discord.TextChannel,
                        kind: str) -> discord.Message:
    cfg = bot.db.config(channel.guild.id)
    if kind == "application":
        old_key, field = "application_panel_message_id", "application_panel_message_id"
        new_message = await publish_application_panel(bot, channel)
    else:
        old_key, field = "voice_panel_message_id", "voice_panel_message_id"
        new_message = await channel.send(embed=voice_panel_embed(), view=VoiceControlView(bot))
    old_message_id = cfg[old_key]
    bot.db.update_channel_ids(channel.guild.id, **{field: new_message.id})
    if old_message_id and old_message_id != new_message.id:
        try:
            old_message = await channel.fetch_message(old_message_id)
            if old_message.author.id == bot.user.id:
                await old_message.delete(reason="DYNASTY: замена панели новым сообщением")
        except discord.NotFound:
            pass
        except discord.HTTPException:
            LOG.exception("Could not delete the older %s panel on guild %s", kind, channel.guild.id)
    return new_message


async def publish_single_component(bot: "ApprovalVoiceBot", guild: discord.Guild,
                                   component: str) -> str:
    cfg = bot.db.config(guild.id)
    if cfg is None:
        raise RuntimeError("Сначала выполните первичную настройку через /menu.")
    me = guild.me or await guild.fetch_member(bot.user.id)
    moderator = guild.get_role(cfg["moderator_role_id"])
    if component == "application":
        channel = guild.get_channel(cfg["application_channel_id"])
        if not isinstance(channel, discord.TextChannel):
            category = await ensure_application_category(bot, guild)
            channel = await guild.create_text_channel(
                "подать-заявку", category=category,
                overwrites=public_panel_permissions(guild, me, moderator),
                reason="Восстановление канала заявок",
            )
            bot.db.update_channel_ids(guild.id, application_channel_id=channel.id,
                                      review_channel_id=channel.id)
        await prepare_application_channel(guild, bot.db.config(guild.id), me)
        await refresh_panel(bot, channel, "application")
        return f"✅ Панель подачи заявки опубликована в {channel.mention}."
    if component == "voice":
        category = await ensure_voice_category(bot, guild)
        cfg = bot.db.config(guild.id)
        lobby = guild.get_channel(cfg["lobby_channel_id"])
        if not isinstance(lobby, discord.VoiceChannel):
            lobby = await guild.create_voice_channel(
                "➕ Создать комнату", category=category,
                overwrites={guild.default_role: discord.PermissionOverwrite(view_channel=True, connect=True),
                            me: discord.PermissionOverwrite(view_channel=True, connect=True,
                                                             move_members=True, manage_channels=True)},
                reason="Восстановление входа в голосовые комнаты",
            )
            bot.db.update_channel_ids(guild.id, lobby_channel_id=lobby.id)
        control = guild.get_channel(cfg["control_channel_id"])
        if not isinstance(control, discord.TextChannel):
            control = await guild.create_text_channel(
                "управление-комнатой", category=category,
                overwrites=public_panel_permissions(guild, me, moderator),
                reason="Восстановление управления голосовыми комнатами",
            )
            bot.db.update_channel_ids(guild.id, control_channel_id=control.id)
        await refresh_panel(bot, control, "voice")
        return (f"✅ Пульт голосовых комнат размещён в {control.mention}. "
                f"Создание комнаты: {lobby.mention}.")
    if component == "application_log":
        channel = guild.get_channel(cfg["log_channel_id"])
        if not isinstance(channel, discord.TextChannel):
            category = await ensure_application_category(bot, guild)
            channel = await guild.create_text_channel(
                "журнал-заявок", category=category,
                overwrites=staff_permissions(guild, me, moderator),
                reason="Восстановление журнала заявок",
            )
            bot.db.update_channel_ids(guild.id, log_channel_id=channel.id)
        await channel.send(embed=discord.Embed(
            title="📋 Журнал заявок DYNASTY",
            description="Здесь фиксируются полные анкеты, решения и результаты обзвонов. "
                        "Исторические записи восстановить из прошлых сообщений автоматически нельзя.",
            color=discord.Color.gold(),
        ))
        return f"✅ Журнал заявок готов: {channel.mention}. Остальные панели не затронуты."
    if component == "voice_log":
        channel = guild.get_channel(cfg["voice_log_channel_id"]) if cfg["voice_log_channel_id"] else None
        if not isinstance(channel, discord.TextChannel):
            category = await ensure_voice_category(bot, guild)
            channel = await guild.create_text_channel(
                "журнал-комнат", category=category,
                overwrites=staff_permissions(guild, me, moderator),
                reason="Восстановление журнала голосовых комнат",
            )
            bot.db.update_channel_ids(guild.id, voice_log_channel_id=channel.id)
        await channel.send(embed=discord.Embed(
            title="🎙️ Журнал голосовых комнат DYNASTY",
            description="Здесь фиксируются создание, удаление и передача приватных комнат. "
                        "Старые записи не восстанавливаются автоматически.",
            color=discord.Color.dark_teal(),
        ))
        return f"✅ Журнал комнат готов: {channel.mention}. Остальные панели не затронуты."
    raise ValueError("Неизвестный компонент")


class AdminPublishView(AdminOnlyView):
    async def publish(self, interaction: discord.Interaction, component: str):
        if self.bot.db.config(interaction.guild.id) is None:
            return await say(interaction, "Сначала выполните первичную настройку через /menu.")
        await interaction.response.defer(ephemeral=True, thinking=True)
        try:
            result = await publish_single_component(self.bot, interaction.guild, component)
        except (discord.HTTPException, RuntimeError, sqlite3.Error, OSError) as exc:
            LOG.exception("Could not republish %s on %s", component, interaction.guild.id)
            return await say(interaction, f"❌ Не удалось опубликовать: {exc}")
        await say(interaction, result)

    @discord.ui.button(label="📝 Заявки", style=discord.ButtonStyle.primary, row=0)
    async def application(self, i: discord.Interaction, _: discord.ui.Button):
        await self.publish(i, "application")

    @discord.ui.button(label="🎧 Голосовые", style=discord.ButtonStyle.primary, row=0)
    async def voice(self, i: discord.Interaction, _: discord.ui.Button):
        await self.publish(i, "voice")

    @discord.ui.button(label="📋 Логи заявок", style=discord.ButtonStyle.secondary, row=1)
    async def application_log(self, i: discord.Interaction, _: discord.ui.Button):
        await self.publish(i, "application_log")

    @discord.ui.button(label="📒 Логи комнат", style=discord.ButtonStyle.secondary, row=1)
    async def voice_log(self, i: discord.Interaction, _: discord.ui.Button):
        await self.publish(i, "voice_log")


class AdminHomeView(AdminOnlyView):
    def __init__(self, bot: "ApprovalVoiceBot", user_id: int):
        super().__init__(bot, user_id)
        # This view is the root; the common Back button makes no sense here.
        for item in list(self.children):
            if isinstance(item, discord.ui.Button) and item.label == "← В меню":
                self.remove_item(item)

    async def switch(self, interaction: discord.Interaction, title: str,
                     view: AdminOnlyView):
        await interaction.response.edit_message(content=title, view=view)

    @discord.ui.button(label="⚙️ Первичная настройка", style=discord.ButtonStyle.primary, row=0)
    async def setup(self, i: discord.Interaction, _: discord.ui.Button):
        await self.switch(i, "Выберите четыре роли и сохраните. Каналы создаются только один раз.",
                          AdminRolesView(self.bot, self.user_id, "initial"))

    @discord.ui.button(label="🎭 Crime / State", style=discord.ButtonStyle.secondary, row=0)
    async def directions(self, i: discord.Interaction, _: discord.ui.Button):
        await self.switch(i, "Выберите две роли для заявок.",
                          AdminRolesView(self.bot, self.user_id, "directions"))

    @discord.ui.button(label="👑 Общая роль", style=discord.ButtonStyle.secondary, row=0)
    async def common(self, i: discord.Interaction, _: discord.ui.Button):
        await self.switch(i, "Выберите роль, получаемую всеми принятыми кандидатами.",
                          AdminRolesView(self.bot, self.user_id, "common"))

    @discord.ui.button(label="🛡️ Модераторы", style=discord.ButtonStyle.secondary, row=0)
    async def moderators(self, i: discord.Interaction, _: discord.ui.Button):
        await self.switch(i, "Выберите роль, которая рассматривает заявки.",
                          AdminRolesView(self.bot, self.user_id, "moderator"))

    @discord.ui.button(label="📞 Канал обзвона", style=discord.ButtonStyle.secondary, row=1)
    async def call(self, i: discord.Interaction, _: discord.ui.Button):
        await self.switch(i, "Выберите голосовой канал, куда нужно прийти за пять минут.",
                          AdminCallChannelView(self.bot, self.user_id))

    @discord.ui.button(label="🔔 Упоминания", style=discord.ButtonStyle.secondary, row=1)
    async def pings(self, i: discord.Interaction, _: discord.ui.Button):
        await self.switch(i, "Выберите роли, затем добавьте или уберите их. Максимум 50.",
                          AdminPingsView(self.bot, self.user_id))

    @discord.ui.button(label="📤 Публикация", style=discord.ButtonStyle.success, row=1)
    async def publishing(self, i: discord.Interaction, _: discord.ui.Button):
        await self.switch(i, "Выберите **один** элемент для отдельной публикации или восстановления:",
                          AdminPublishView(self.bot, self.user_id))

    @discord.ui.button(label="🧹 Сброс заявки", style=discord.ButtonStyle.danger, row=1)
    async def reset(self, i: discord.Interaction, _: discord.ui.Button):
        await self.switch(i, "Выберите кандидата; сброс удалит активную заявку и её ветку.",
                          AdminResetView(self.bot, self.user_id))

    @discord.ui.button(label="🔄 Обновить статус", style=discord.ButtonStyle.secondary, row=2)
    async def status(self, i: discord.Interaction, _: discord.ui.Button):
        await i.response.edit_message(content=config_overview(self.bot, i.guild), view=self)



bot = ApprovalVoiceBot()


@bot.tree.command(name="setup_server", description="Первичная настройка заявок и временных голосовых комнат")
@app_commands.describe(crime_role="Роль после одобрения направления Crime",
                       state_role="Роль после одобрения направления State",
                       moderator_role="Роль, которой разрешено рассматривать анкеты",
                       common_role="Общая обязательная роль всем принятым (можно указать позже)")
@app_commands.guild_only()
@app_commands.default_permissions(administrator=True)
@app_commands.checks.has_permissions(administrator=True)
async def setup_server(interaction: discord.Interaction, crime_role: discord.Role,
                       state_role: discord.Role, moderator_role: discord.Role,
                       common_role: discord.Role | None = None):
    guild = interaction.guild
    if guild is None:
        return await say(interaction, "Команда доступна только на сервере.")
    if bot.db.config(guild.id) is not None:
        return await say(interaction,
            "Сервер уже настроен. Откройте /menu для редактирования настроек. "
            "Повторная первичная настройка не нужна.")
    if any(role.is_default() for role in (crime_role, state_role, moderator_role)) or len(
            {crime_role.id, state_role.id, moderator_role.id}) != 3:
        return await say(interaction, "Выберите три разные роли, не @everyone.")
    me = guild.me or await guild.fetch_member(bot.user.id)
    if common_role and (common_role.is_default() or common_role >= me.top_role):
        return await say(interaction, "Общая роль должна быть ниже роли бота и не @everyone.")
    if any(role >= me.top_role for role in (crime_role, state_role)):
        return await say(interaction, "Переместите роль бота ВЫШЕ ролей Crime и State.")

    await interaction.response.defer(ephemeral=True, thinking=True)
    try:
        everyone = guild.default_role
        bot_perms = discord.PermissionOverwrite(
            view_channel=True, send_messages=True, read_message_history=True,
            embed_links=True, create_private_threads=True, create_public_threads=True,
            send_messages_in_threads=True, manage_threads=True, manage_messages=True, attach_files=True,
        )
        panel_perms = {
            everyone: discord.PermissionOverwrite(
                view_channel=True, send_messages=False, read_message_history=True,
                create_private_threads=False, create_public_threads=False,
                send_messages_in_threads=False,
            ),
            moderator_role: discord.PermissionOverwrite(
                view_channel=True, read_message_history=True, manage_threads=True,
                send_messages_in_threads=True,
            ),
            me: bot_perms,
        }
        staff_perms = {
            everyone: discord.PermissionOverwrite(view_channel=False),
            moderator_role: discord.PermissionOverwrite(view_channel=True, send_messages=True,
                                                       read_message_history=True),
            me: bot_perms,
        }
        application_category = await guild.create_category("📨・Заявки", reason="Установка системы заявок")
        application_channel = await guild.create_text_channel(
            "подать-заявку", category=application_category, overwrites=panel_perms,
        )
        log_channel = await guild.create_text_channel(
            "журнал-заявок", category=application_category, overwrites=staff_perms,
        )
        voice_category = await guild.create_category("🎧・Приватные комнаты", reason="Временные голосовые комнаты")
        lobby = await guild.create_voice_channel(
            "➕ Создать комнату", category=voice_category,
            overwrites={
                everyone: discord.PermissionOverwrite(view_channel=True, connect=True),
                me: discord.PermissionOverwrite(view_channel=True, connect=True,
                                                move_members=True, manage_channels=True),
            },
        )
        control_channel = await guild.create_text_channel(
            "управление-комнатой", category=voice_category, overwrites=panel_perms,
        )
        voice_log_channel = await guild.create_text_channel(
            "журнал-комнат", category=voice_category, overwrites=staff_perms,
        )
        bot.db.save_config({
            "guild_id": guild.id,
            "approved_role_id": crime_role.id,  # legacy column (not used for new applications)
            "crime_role_id": crime_role.id,
            "state_role_id": state_role.id,
            "moderator_role_id": moderator_role.id,
            "application_channel_id": application_channel.id,
            "review_channel_id": application_channel.id,  # legacy column
            "log_channel_id": log_channel.id,
            "control_channel_id": control_channel.id,
            "lobby_channel_id": lobby.id,
            "voice_category_id": voice_category.id,
            "voice_log_channel_id": voice_log_channel.id,
            "ping_role_ids": str(moderator_role.id),  # default notification on new servers
            "common_role_id": common_role.id if common_role else None,
        })
        application_message = await publish_application_panel(bot, application_channel)
        voice_message = await control_channel.send(embed=voice_panel_embed(), view=VoiceControlView(bot))
        bot.db.update_channel_ids(guild.id, application_panel_message_id=application_message.id,
                                  voice_panel_message_id=voice_message.id)
        await say(interaction, (
            "✅ Система настроена!\n"
            f"• Анкеты и приватные ветки модераторов: {application_channel.mention}\n"
            f"• Журнал: {log_channel.mention}\n"
            f"• Crime: {crime_role.mention}\n"
            f"• State: {state_role.mention}\n"
            f"• Общая роль: {common_role.mention if common_role else 'назначьте через /menu'}\n"
            f"• Создание голоса: {lobby.mention}\n"
            f"• Управление: {control_channel.mention}\n"
            f"• Уведомления о новых заявках: {moderator_role.mention} "
            "(изменить: /set_application_ping_roles)"
        ))
    except (discord.HTTPException, sqlite3.Error, OSError):
        LOG.exception("Initial guild setup failed")
        await say(interaction, "❌ Ошибка настройки. Проверьте права бота и журнал. Возможно, часть каналов создана.")


@bot.tree.command(name="set_application_roles", description="Назначить роли Crime и State (для уже настроенного сервера)")
@app_commands.describe(crime_role="Роль для заявителей Crime", state_role="Роль для заявителей State")
@app_commands.guild_only()
@app_commands.default_permissions(administrator=True)
@app_commands.checks.has_permissions(administrator=True)
async def set_application_roles(interaction: discord.Interaction, crime_role: discord.Role,
                                state_role: discord.Role):
    guild = interaction.guild
    if guild is None or bot.db.config(guild.id) is None:
        return await say(interaction, "Сначала выполните первоначальную настройку через /menu.")
    if crime_role.is_default() or state_role.is_default() or crime_role.id == state_role.id:
        return await say(interaction, "Выберите две РАЗНЫЕ роли, не @everyone.")
    me = guild.me or await guild.fetch_member(bot.user.id)
    if crime_role >= me.top_role or state_role >= me.top_role:
        return await say(interaction, "Роль бота должна находиться выше обеих выдаваемых ролей.")
    await interaction.response.defer(ephemeral=True, thinking=True)
    try:
        await prepare_application_channel(guild, bot.db.config(guild.id), me)
        bot.db.set_application_roles(guild.id, crime_role.id, state_role.id)
    except (discord.HTTPException, RuntimeError, sqlite3.Error) as exc:
        LOG.exception("Could not set application roles/permissions")
        return await say(interaction, f"❌ Не удалось настроить доступ к веткам: {exc}")
    await say(interaction, (
        f"✅ Назначено: Crime → {crime_role.mention}, State → {state_role.mention}.\n"
        "Приватные ветки будут появляться прямо в #подать-заявку.\n"
        "Панель заявок можно обновить отдельно: /menu → Публикация → Заявки.\n"
        "Существующие голосовые комнаты и настройки останутся без изменений."
    ))


@bot.tree.command(name="set_application_ping_roles", description="Настроить до 50 ролей для уведомлений о заявках")
@app_commands.describe(roles="Упоминания или ID ролей через пробел/запятую; максимум 50")
@app_commands.guild_only()
@app_commands.default_permissions(administrator=True)
@app_commands.checks.has_permissions(administrator=True)
async def set_application_ping_roles(interaction: discord.Interaction, roles: str):
    guild = interaction.guild
    settings = bot.db.config(guild.id) if guild else None
    if settings is None:
        return await say(interaction, "Сначала выполните первоначальную настройку через /menu.")
    try:
        role_ids = parse_role_list(roles)
    except ValueError as exc:
        return await say(interaction, f"❌ {exc}")
    selected = []
    for role_id in role_ids:
        role = guild.get_role(role_id)
        if role is None or role.is_default():
            return await say(interaction, f"❌ Роль с ID {role_id} не найдена или это @everyone.")
        selected.append(role)
    bot.db.set_application_ping_roles(guild.id, [role.id for role in selected])
    channel = guild.get_channel(settings["application_channel_id"])
    missing = []
    if isinstance(channel, discord.TextChannel):
        me = guild.me or await guild.fetch_member(bot.user.id)
        can_ping = channel.permissions_for(me).mention_everyone
        missing = [role.name for role in selected if not role.mentionable and not can_ping]
    response = (f"✅ Настроено {len(selected)} ролей для упоминания в новых заявках. "
                "Настройки сохранены в базе данных. Панель обновлять не нужно.")
    if missing:
        response += ("\n⚠️ Некоторые роли недоступны для уведомлений: "
                     "включите «Разрешить упоминание роли» или право бота на упоминания.")
    response += ("\n⚠️ Ролям необходим доступ к приватным веткам, "
                 "например право «Управлять ветками» и «Просматривать канал».")
    await say(interaction, response)


@bot.tree.command(name="set_application_call_channel", description="Выбрать голосовой канал для обзвона кандидатов")
@app_commands.describe(channel="Голосовой канал, куда кандидат должен зайти за 5 минут")
@app_commands.guild_only()
@app_commands.default_permissions(administrator=True)
@app_commands.checks.has_permissions(administrator=True)
async def set_application_call_channel(interaction: discord.Interaction, channel: discord.VoiceChannel):
    guild = interaction.guild
    if guild is None or bot.db.config(guild.id) is None:
        return await say(interaction, "Сначала выполните первоначальную настройку через /menu.")
    await interaction.response.defer(ephemeral=True, thinking=True)
    try:
        me = guild.me or await guild.fetch_member(bot.user.id)
        await prepare_application_channel(guild, bot.db.config(guild.id), me)
        bot.db.set_call_channel(guild.id, channel.id)
    except (discord.HTTPException, RuntimeError, sqlite3.Error) as exc:
        LOG.exception("Could not configure application call channel")
        return await say(interaction, f"❌ Не удалось настроить обзвон и доступ к веткам: {exc}")
    await say(interaction, f"✅ Голосовой канал обзвона: {channel.mention}. "
              "После вызова кандидату даётся 5 минут на вход.")


@bot.tree.command(name="set_application_common_role", description="Общая роль каждому одобренному кандидату")
@app_commands.describe(role="Роль, добавляемая вместе с Crime или State")
@app_commands.guild_only()
@app_commands.default_permissions(administrator=True)
@app_commands.checks.has_permissions(administrator=True)
async def set_application_common_role(interaction: discord.Interaction, role: discord.Role):
    guild = interaction.guild
    if guild is None or bot.db.config(guild.id) is None:
        return await say(interaction, "Сначала выполните первоначальную настройку через /menu.")
    me = guild.me or await guild.fetch_member(bot.user.id)
    if role.is_default() or role >= me.top_role:
        return await say(interaction, "❌ Общая роль должна быть ниже роли бота и не @everyone.")
    bot.db.set_common_role(guild.id, role.id)
    await say(interaction, f"✅ При любом одобрении бот выдаст {role.mention} "
              "вместе с ролью Crime или State.")


@bot.tree.command(name="clear_application_ping_roles", description="Выключить упоминания ролей в новых заявках")
@app_commands.guild_only()
@app_commands.default_permissions(administrator=True)
@app_commands.checks.has_permissions(administrator=True)
async def clear_application_ping_roles(interaction: discord.Interaction):
    guild = interaction.guild
    if guild is None or bot.db.config(guild.id) is None:
        return await say(interaction, "Сначала выполните первоначальную настройку через /menu.")
    bot.db.set_application_ping_roles(guild.id, [])
    await say(interaction, "✅ Упоминания ролей для новых заявок отключены. Остальные функции не изменены.")


@bot.tree.command(name="republish_panels", description="Повторно разместить кнопки заявок и голосовых комнат")
@app_commands.guild_only()
@app_commands.default_permissions(administrator=True)
@app_commands.checks.has_permissions(administrator=True)
async def republish_panels(interaction: discord.Interaction):
    guild = interaction.guild
    settings = bot.db.config(guild.id) if guild else None
    if settings is None:
        return await say(interaction, "Сначала выполните первоначальную настройку через /menu.")
    application_channel = guild.get_channel(settings["application_channel_id"])
    control_channel = guild.get_channel(settings["control_channel_id"])
    if not isinstance(application_channel, discord.TextChannel) or not isinstance(control_channel, discord.TextChannel):
        return await say(interaction, "Один из каналов удалён. Нужно восстановить конфигурацию.")
    await interaction.response.defer(ephemeral=True, thinking=True)
    try:
        await prepare_application_channel(guild, settings, guild.me or await guild.fetch_member(bot.user.id))
        await publish_application_panel(bot, application_channel)
        await control_channel.send(embed=voice_panel_embed(), view=VoiceControlView(bot))
        await say(interaction, "✅ Обе панели опубликованы повторно. Старые сообщения можно удалить вручную.")
    except (discord.HTTPException, RuntimeError, OSError) as exc:
        await say(interaction, f"Ошибка отправки панелей или настройки прав: {exc}")


@bot.tree.command(name="reset_application", description="Снять зависшую заявку с рассмотрения (администратор)")
@app_commands.describe(user="Заявитель, заявку которого нужно сбросить")
@app_commands.guild_only()
@app_commands.default_permissions(administrator=True)
@app_commands.checks.has_permissions(administrator=True)
async def reset_application(interaction: discord.Interaction, user: discord.Member):
    guild = interaction.guild
    entry = bot.db.active_application(guild.id, user.id) if guild else None
    if entry is None:
        return await say(interaction, "У этого участника нет активной заявки.")
    if entry["status"] == "processing":
        return await say(interaction, "Сейчас выполняется решение по заявке. Повторите позже.")
    bot.db.reset_application(guild.id, user.id)
    if entry["thread_id"]:
        try:
            thread = guild.get_thread(entry["thread_id"]) or await guild.fetch_channel(entry["thread_id"])
            if isinstance(thread, discord.Thread):
                await thread.delete(reason="Административный сброс заявки")
        except (discord.NotFound, discord.Forbidden, discord.HTTPException):
            pass
    settings = bot.db.config(guild.id)
    if settings and entry["anchor_message_id"]:
        anchor_channel = guild.get_channel(settings["review_channel_id"])
        if isinstance(anchor_channel, discord.TextChannel):
            try:
                await (await anchor_channel.fetch_message(entry["anchor_message_id"])).delete()
            except (discord.NotFound, discord.Forbidden, discord.HTTPException):
                pass
    await say(interaction, "✅ Заявка сброшена. Пользователь может отправить анкету заново.")


@bot.tree.command(name="bot_status", description="Проверить настройку системы заявок и голосовых комнат")
@app_commands.guild_only()
async def bot_status(interaction: discord.Interaction):
    guild = interaction.guild
    cfg = bot.db.config(guild.id) if guild else None
    if cfg is None:
        return await say(interaction, "Бот запущен, но не настроен. Администратору нужно открыть /menu.")
    if not configured_application_roles(guild, cfg):
        return await say(interaction, "⚠️ Бот работает, но роли Crime и State не настроены. Администратор: /menu.")
    ping_roles = application_ping_roles(guild, bot)
    ping_summary = ", ".join(role.name for role in ping_roles) if ping_roles else "не назначены"
    call_channel = guild.get_channel(cfg["call_channel_id"]) if cfg["call_channel_id"] else None
    common_role = guild.get_role(cfg["common_role_id"]) if cfg["common_role_id"] else None
    await say(interaction, "✅ Бот работает. Роли Crime/State настроены, заявки и голосовые комнаты доступны."
              f"\n🔔 Упоминания: {ping_summary}."
              f"\n📞 Канал обзвона: {call_channel.mention if call_channel else 'не назначен'}."
              f"\n👑 Обязательная роль: {common_role.name if common_role else 'не назначена'}.")



@bot.tree.command(name="menu", description="Единая панель администратора DYNASTY FAMILY")
@app_commands.guild_only()
@app_commands.default_permissions(administrator=True)
@app_commands.checks.has_permissions(administrator=True)
async def menu(interaction: discord.Interaction):
    guild = interaction.guild
    if guild is None or not isinstance(interaction.user, discord.Member):
        return await say(interaction, "Команда доступна только администраторам сервера.")
    await interaction.response.send_message(
        config_overview(bot, guild),
        view=AdminHomeView(bot, interaction.user.id), ephemeral=True,
    )


# Old admin commands stay as internal implementation helpers for a safe migration.
# They are NOT registered publicly: every admin operation starts with /menu.
for _old_command in (
    "setup_server", "set_application_roles", "set_application_ping_roles",
    "set_application_call_channel", "set_application_common_role",
    "clear_application_ping_roles", "republish_panels", "reset_application", "bot_status",
):
    bot.tree.remove_command(_old_command)


@bot.tree.error
async def slash_error(interaction: discord.Interaction, error: app_commands.AppCommandError):
    if isinstance(error, app_commands.MissingPermissions):
        return await say(interaction, "❌ Для этой команды необходимы права администратора сервера.")
    LOG.error("Slash command failed: %r", error, exc_info=(type(error), error, error.__traceback__))
    try:
        await say(interaction, "❌ Не удалось выполнить команду. Проверьте права бота и консоль.")
    except discord.HTTPException:
        pass


if __name__ == "__main__":
    if not TOKEN or TOKEN == "PASTE_YOUR_BOT_TOKEN_HERE":
        raise SystemExit("DISCORD_TOKEN отсутствует. Создайте .env по примеру .env.example.")
    bot.run(TOKEN, log_handler=None)
