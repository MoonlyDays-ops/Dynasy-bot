"""Discord application approvals + self-managed temporary voice rooms.

Python 3.11+; discord.py 2.7.1. Start with: python bot.py
"""
from __future__ import annotations

import asyncio
import logging
import os
import re
import sqlite3
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
    await channel.send(
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
                  manage_threads=True, embed_links=True, attach_files=True)
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
                              role_name: str | None = None) -> bool:
    """Best effort: closed DMs must never cancel an already-processed decision."""
    if status == "pending":
        title = "📨 Заявка получена"
        body = f"Ваша заявка №{app_id} на сервер **{guild_name}** принята к рассмотрению."
        color = discord.Color.gold()
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
            return await say(interaction, "Администратору нужно настроить роли командой /set_application_roles.")
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
            return await say(interaction, "Администрации нужно назначить роли Crime и State: /set_application_roles.")
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

    async def decide(self, interaction: discord.Interaction, accepted: bool):
        guild = interaction.guild
        if guild is None or not isinstance(interaction.user, discord.Member) or interaction.message is None:
            return await say(interaction, "Команда работает только внутри ветки заявки.")
        settings = self.bot.db.config(guild.id)
        entry = self.bot.db.application_for_message(interaction.message.id)
        if settings is None or entry is None or interaction.channel_id != entry["thread_id"]:
            return await say(interaction, "Заявка не найдена или уже удалена.")
        moderator_role = guild.get_role(settings["moderator_role_id"])
        if not (interaction.user.guild_permissions.administrator or
                (moderator_role is not None and moderator_role in interaction.user.roles)):
            return await say(interaction, "Рассматривать заявки могут только модераторы.")
        if not self.bot.db.claim_application(entry["id"]):
            return await say(interaction, "Эту заявку уже рассматривают или решение принято.")

        await interaction.response.defer(ephemeral=True, thinking=True)
        selected_role = None
        direction = entry["direction"]
        dm_sent = False
        try:
            applicant = await get_member(guild, entry["applicant_id"])
            me = guild.me or await guild.fetch_member(self.bot.user.id)
            if accepted:
                if direction == "Crime":
                    role_id = settings["crime_role_id"]
                elif direction == "State":
                    role_id = settings["state_role_id"]
                else:
                    # Support old pending applications created before the update.
                    role_id = settings["approved_role_id"]
                selected_role = guild.get_role(role_id) if role_id else None
                if selected_role is None:
                    raise RuntimeError("Роль для выбранного направления не найдена. Обновите настройки.")
                if selected_role >= me.top_role:
                    raise RuntimeError("Роль бота должна находиться выше выдаваемой роли.")
                if applicant is None:
                    raise RuntimeError("Заявитель уже покинул сервер; назначить роль нельзя.")
                await applicant.add_roles(
                    selected_role, reason=f"Заявка #{entry['id']} ({direction or 'старая'}) одобрена {interaction.user}",
                )
                decision = "accepted"
            else:
                if applicant is not None:
                    if applicant.id == guild.owner_id or applicant.top_role >= me.top_role:
                        raise RuntimeError("Не могу исключить владельца или пользователя выше роли бота.")
                    if not me.guild_permissions.kick_members:
                        raise RuntimeError("У бота отсутствует разрешение Kick Members.")
                    # Send before kick: without a shared server, the DM may be
                    # rejected by Discord even when the user's DMs are enabled.
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
            return await say(interaction, "❌ Внутренняя ошибка. Проверьте журнал бота.")

        status = "✅ Принята" if accepted else "❌ Отклонена"
        # Accepted: notify after role assignment; rejected: already notified
        # before kick. When the applicant had already left, try fetching a user.
        if accepted or applicant is None:
            applicant_user = applicant
            if applicant_user is None:
                try:
                    applicant_user = await self.bot.fetch_user(entry["applicant_id"])
                except discord.HTTPException:
                    applicant_user = None
            if applicant_user is not None:
                dm_sent = await send_application_dm(
                    applicant_user, app_id=entry["id"], guild_name=guild.name,
                    status=decision, direction=direction,
                    role_name=selected_role.name if selected_role else None,
                )

        log_channel = guild.get_channel(settings["log_channel_id"])
        if isinstance(log_channel, discord.TextChannel):
            try:
                embed = discord.Embed(
                    title=f"Заявка №{entry['id']}: {status}",
                    color=discord.Color.green() if accepted else discord.Color.red(),
                    description=(f"**Заявитель:** <@{entry['applicant_id']}> (`{entry['applicant_id']}`)\n"
                                 f"**Направление:** {direction or 'старая анкета'}\n"
                                 f"**Модератор:** <@{interaction.user.id}>\n"
                                 f"**ЛС:** {'доставлено' if dm_sent else 'не удалось доставить'}"),
                )
                await log_channel.send(embed=embed, allowed_mentions=discord.AllowedMentions.none())
            except discord.HTTPException:
                LOG.exception("Could not write moderation log")

        thread = interaction.channel
        if isinstance(thread, discord.Thread):
            try:
                await thread.delete(reason=f"Заявка #{entry['id']}: {status}")
            except discord.HTTPException:
                LOG.exception("Application handled, but thread could not be deleted")
                return await say(interaction, f"{status}. Действие выполнено, но ветку нужно удалить вручную.")
        # Legacy applications used an anchor message in another channel.
        if entry["anchor_message_id"]:
            anchor_channel = guild.get_channel(settings["review_channel_id"])
            if isinstance(anchor_channel, discord.TextChannel):
                try:
                    anchor = await anchor_channel.fetch_message(entry["anchor_message_id"])
                    await anchor.delete()
                except (discord.NotFound, discord.Forbidden, discord.HTTPException):
                    LOG.warning("Review anchor could not be deleted for #%s", entry["id"])
        extra = "" if dm_sent else " Участник не получил ЛС (возможно, сообщения закрыты)."
        await say(interaction, f"{status}. Ветка удалена, решение сохранено в журнале.{extra}")

    @discord.ui.button(label="Принять", emoji="✅", style=discord.ButtonStyle.success,
                       custom_id="avb:application:accept")
    async def accept(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self.decide(interaction, True)

    @discord.ui.button(label="Отклонить", emoji="❌", style=discord.ButtonStyle.danger,
                       custom_id="avb:application:reject")
    async def reject(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self.decide(interaction, False)


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
                await channel.edit(name=new_name, reason=f"Изменение названия владельцем {interaction.user.id}")
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

    async def setup_hook(self):
        # Release operations left half-finished by a crashed previous process.
        self.db.recover_incomplete_operations()
        # These views work even for messages that were sent before the last restart.
        self.add_view(ApplicationPanelView(self))
        self.add_view(ReviewView(self))
        self.add_view(VoiceControlView(self))
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
        except (discord.HTTPException, sqlite3.IntegrityError):
            LOG.exception("Room %s could not be reassigned", channel.id)


bot = ApprovalVoiceBot()


@bot.tree.command(name="setup_server", description="Первичная настройка заявок и временных голосовых комнат")
@app_commands.describe(crime_role="Роль после одобрения направления Crime",
                       state_role="Роль после одобрения направления State",
                       moderator_role="Роль, которой разрешено рассматривать анкеты")
@app_commands.guild_only()
@app_commands.default_permissions(administrator=True)
@app_commands.checks.has_permissions(administrator=True)
async def setup_server(interaction: discord.Interaction, crime_role: discord.Role,
                       state_role: discord.Role, moderator_role: discord.Role):
    guild = interaction.guild
    if guild is None:
        return await say(interaction, "Команда доступна только на сервере.")
    if bot.db.config(guild.id) is not None:
        return await say(interaction,
            "Сервер уже настроен. Используйте /set_application_roles, затем /republish_panels. "
            "Повторный /setup_server не нужен.")
    if any(role.is_default() for role in (crime_role, state_role, moderator_role)) or len(
            {crime_role.id, state_role.id, moderator_role.id}) != 3:
        return await say(interaction, "Выберите три разные роли, не @everyone.")
    me = guild.me or await guild.fetch_member(bot.user.id)
    if any(role >= me.top_role for role in (crime_role, state_role)):
        return await say(interaction, "Переместите роль бота ВЫШЕ ролей Crime и State.")

    await interaction.response.defer(ephemeral=True, thinking=True)
    try:
        everyone = guild.default_role
        bot_perms = discord.PermissionOverwrite(
            view_channel=True, send_messages=True, read_message_history=True,
            embed_links=True, create_private_threads=True, create_public_threads=True,
            send_messages_in_threads=True, manage_threads=True, attach_files=True,
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
            "ping_role_ids": str(moderator_role.id),  # default notification on new servers
        })
        await publish_application_panel(bot, application_channel)
        await control_channel.send(embed=voice_panel_embed(), view=VoiceControlView(bot))
        await say(interaction, (
            "✅ Система настроена!\n"
            f"• Анкеты и приватные ветки модераторов: {application_channel.mention}\n"
            f"• Журнал: {log_channel.mention}\n"
            f"• Crime: {crime_role.mention}\n"
            f"• State: {state_role.mention}\n"
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
        return await say(interaction, "Сначала выполните /setup_server.")
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
        "Теперь вызовите /republish_panels, чтобы разместить новый Embed с изображением.\n"
        "Существующие голосовые комнаты и настройки останутся без изменений."
    ))


@bot.tree.command(name="set_application_ping_roles", description="Роли для уведомления в новых ветках заявок")
@app_commands.describe(
    role1="Первая роль, которую упоминать при новой заявке",
    role2="Вторая роль (необязательно)",
    role3="Третья роль (необязательно)",
)
@app_commands.guild_only()
@app_commands.default_permissions(administrator=True)
@app_commands.checks.has_permissions(administrator=True)
async def set_application_ping_roles(interaction: discord.Interaction, role1: discord.Role,
                                     role2: discord.Role | None = None,
                                     role3: discord.Role | None = None):
    guild = interaction.guild
    settings = bot.db.config(guild.id) if guild else None
    if settings is None:
        return await say(interaction, "Сначала выполните /setup_server.")
    roles = [role for role in (role1, role2, role3) if role is not None]
    if any(role.is_default() for role in roles):
        return await say(interaction, "❌ Роль @everyone выбирать нельзя.")
    if len({role.id for role in roles}) != len(roles):
        return await say(interaction, "❌ Нельзя указывать одну и ту же роль несколько раз.")
    try:
        bot.db.set_application_ping_roles(guild.id, [role.id for role in roles])
    except (sqlite3.Error, ValueError) as exc:
        LOG.exception("Could not save application notification roles")
        return await say(interaction, f"❌ Не удалось сохранить роли уведомлений: {exc}")
    channel = guild.get_channel(settings["application_channel_id"])
    missing_mention_permission = []
    if isinstance(channel, discord.TextChannel):
        me = guild.me or await guild.fetch_member(bot.user.id)
        can_mention_any_role = channel.permissions_for(me).mention_everyone
        missing_mention_permission = [r.name for r in roles
                                      if not r.mentionable and not can_mention_any_role]
    response = ("✅ При каждой новой заявке в приватной ветке будет сообщение с упоминанием: "
                + ", ".join(role.mention for role in roles)
                + ".\nОбновлять панель /republish_panels не требуется.")
    if missing_mention_permission:
        response += ("\n⚠️ Для настоящего уведомления разрешите упоминание указанных ролей "
                     "или предоставьте боту право «Упоминать @everyone, @here и все роли» "
                     "в канале заявок. Сейчас не все упоминания могут уведомлять участников.")
    response += ("\n⚠️ Ветка приватная: участникам этих ролей нужен доступ к ней. "
                 "Модераторам можно предоставить «Просматривать канал» и «Управлять ветками».")
    await say(interaction, response)


@bot.tree.command(name="clear_application_ping_roles", description="Выключить упоминания ролей в новых заявках")
@app_commands.guild_only()
@app_commands.default_permissions(administrator=True)
@app_commands.checks.has_permissions(administrator=True)
async def clear_application_ping_roles(interaction: discord.Interaction):
    guild = interaction.guild
    if guild is None or bot.db.config(guild.id) is None:
        return await say(interaction, "Сначала выполните /setup_server.")
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
        return await say(interaction, "Сначала запустите /setup_server.")
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
        return await say(interaction, "Бот запущен, но не настроен. Администратору нужно вызвать /setup_server.")
    if not configured_application_roles(guild, cfg):
        return await say(interaction, "⚠️ Бот работает, но роли Crime и State не настроены. Администратор: /set_application_roles.")
    ping_roles = application_ping_roles(guild, bot)
    ping_summary = ", ".join(role.name for role in ping_roles) if ping_roles else "не назначены"
    await say(interaction, "✅ Бот работает. Роли Crime/State настроены, заявки и голосовые комнаты доступны."
              f"\n🔔 Упоминания в новых заявках: {ping_summary}.")


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
