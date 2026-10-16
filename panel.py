from __future__ import annotations

import asyncio
import logging

import disnake
from disnake.ext import commands

from config import BANNER_PATH
from shared import ALLOWED_MENTIONS, build_task_pages, make_banner_file, make_panel_embed, store
from tasks import TaskRequestModal


logger = logging.getLogger("task_bot")


class TaskPanelActionSelect(disnake.ui.StringSelect):
    def __init__(self, panel: "PanelCog"):
        self.panel = panel
        super().__init__(
            custom_id="task-panel:action",
            placeholder="Выберите действие",
            min_values=1,
            max_values=1,
            options=[
                disnake.SelectOption(
                    label="Tasks",
                    value="tasks",
                    description="Посмотреть все задания и отклики",
                    emoji="📋",
                ),
                disnake.SelectOption(
                    label="Get task",
                    value="get_task",
                    description="Подать заявку на задание",
                    emoji="📝",
                ),
            ],
        )

    async def callback(self, inter: disnake.MessageInteraction) -> None:
        await self.panel.handle_panel_select(inter)


class PersistentTaskPanelView(disnake.ui.View):
    def __init__(self, panel: "PanelCog"):
        super().__init__(timeout=None)
        self.add_item(TaskPanelActionSelect(panel))


class TaskPagesView(disnake.ui.View):
    """Paginate task results by editing one ephemeral response in place."""

    def __init__(self, pages: list[disnake.Embed], user_id: int):
        super().__init__(timeout=900)
        self.pages = pages
        self.user_id = user_id
        self.page_index = 0
        self._sync_buttons()

    def _sync_buttons(self) -> None:
        self.previous_page.disabled = self.page_index == 0
        self.next_page.disabled = self.page_index >= len(self.pages) - 1

    async def interaction_check(self, inter: disnake.MessageInteraction) -> bool:
        # Ephemeral responses are visible only to their requester. This guard
        # also prevents a copied component interaction from changing the view.
        return inter.author.id == self.user_id

    async def _change_page(self, inter: disnake.MessageInteraction, amount: int) -> None:
        self.page_index = min(max(self.page_index + amount, 0), len(self.pages) - 1)
        self._sync_buttons()
        try:
            await inter.response.edit_message(embed=self.pages[self.page_index], view=self)
        except disnake.HTTPException:
            logger.exception(
                "Could not change task-list page: interaction_id=%s user_id=%s page=%s page_count=%s",
                inter.id,
                inter.author.id,
                self.page_index + 1,
                len(self.pages),
            )

    @disnake.ui.button(label="Назад", style=disnake.ButtonStyle.secondary, disabled=True)
    async def previous_page(self, button: disnake.ui.Button, inter: disnake.MessageInteraction) -> None:
        await self._change_page(inter, -1)

    @disnake.ui.button(label="Дальше", style=disnake.ButtonStyle.secondary)
    async def next_page(self, button: disnake.ui.Button, inter: disnake.MessageInteraction) -> None:
        await self._change_page(inter, 1)


class PanelCog(commands.Cog):
    def __init__(self, bot: commands.InteractionBot):
        self.bot = bot
        self._panel_locks: dict[int, asyncio.Lock] = {}
        self._startup_panel_check_done = False

    def _panel_message_options(self, *, include_banner: bool) -> dict:
        options = {
            "embed": make_panel_embed(include_banner=include_banner),
            "view": self._view(),
            "allowed_mentions": ALLOWED_MENTIONS,
        }
        banner = make_banner_file() if include_banner else None
        if banner is not None:
            options["file"] = banner
        return options

    def _view(self) -> PersistentTaskPanelView:
        return PersistentTaskPanelView(self)

    def _log_channel_permissions(
        self,
        channel: disnake.TextChannel,
        action: str,
    ) -> list[str]:
        diagnostic = getattr(self.bot, "log_permission_diagnostics", None)
        if callable(diagnostic):
            required = ["view_channel", "send_messages", "embed_links"]
            return diagnostic(
                channel.guild,
                action=action,
                channel=channel,
                required=tuple(required),
            )
        return []

    async def _delete_queued_panels(self, guild: disnake.Guild) -> bool:
        """Retry old-message cleanup after the new panel ID is safely persisted."""
        all_deleted = True
        active = store.get_panel(guild.id)
        for queued in store.get_panel_cleanup(guild.id):
            if active and (
                active["channel_id"] == queued["channel_id"]
                and active["message_id"] == queued["message_id"]
            ):
                logger.error(
                    "Skipped panel cleanup because it points to the active message: guild_id=%s channel_id=%s message_id=%s",
                    guild.id,
                    queued["channel_id"],
                    queued["message_id"],
                )
                all_deleted = False
                continue

            channel = self.bot.get_channel(queued["channel_id"])
            if channel is None:
                try:
                    channel = await self.bot.fetch_channel(queued["channel_id"])
                except disnake.NotFound:
                    logger.warning(
                        "Old task-panel channel no longer exists; clearing cleanup record: guild_id=%s channel_id=%s message_id=%s",
                        guild.id,
                        queued["channel_id"],
                        queued["message_id"],
                    )
                    store.clear_panel_cleanup(guild.id, queued["channel_id"], queued["message_id"])
                    continue
                except disnake.HTTPException:
                    logger.exception(
                        "Could not fetch old task-panel channel: guild_id=%s channel_id=%s message_id=%s",
                        guild.id,
                        queued["channel_id"],
                        queued["message_id"],
                    )
                    all_deleted = False
                    continue

            if not isinstance(channel, disnake.TextChannel) or channel.guild.id != guild.id:
                logger.error(
                    "Old task-panel channel is unavailable or has wrong type: guild_id=%s channel_id=%s message_id=%s type=%s",
                    guild.id,
                    queued["channel_id"],
                    queued["message_id"],
                    type(channel).__name__,
                )
                all_deleted = False
                continue

            try:
                await channel.get_partial_message(queued["message_id"]).delete()
            except disnake.NotFound:
                logger.info(
                    "Old task panel was already absent: guild_id=%s channel_id=%s message_id=%s",
                    guild.id,
                    queued["channel_id"],
                    queued["message_id"],
                )
            except disnake.HTTPException as exc:
                logger.exception(
                    "Could not delete old task panel: guild_id=%s channel_id=%s message_id=%s http_status=%s discord_code=%s response=%s",
                    guild.id,
                    queued["channel_id"],
                    queued["message_id"],
                    getattr(exc, "status", None),
                    getattr(exc, "code", None),
                    getattr(exc, "text", str(exc)),
                )
                all_deleted = False
                continue
            store.clear_panel_cleanup(guild.id, queued["channel_id"], queued["message_id"])
            logger.info(
                "Deleted previous task panel: guild_id=%s channel_id=%s message_id=%s",
                guild.id,
                queued["channel_id"],
                queued["message_id"],
            )
        return all_deleted

    async def _publish_replacement(
        self,
        guild: disnake.Guild,
        channel: disnake.TextChannel,
        *,
        previous: dict[str, int] | None,
        action: str,
    ) -> disnake.Message:
        missing = self._log_channel_permissions(channel, action)
        if missing:
            raise PermissionError("Missing channel permissions for task panel: " + ", ".join(missing))

        bot_member = guild.me
        if bot_member is None and self.bot.user is not None:
            bot_member = guild.get_member(self.bot.user.id)
        include_banner = BANNER_PATH.is_file() and bot_member is not None
        if include_banner:
            include_banner = channel.permissions_for(bot_member).attach_files
        if BANNER_PATH.is_file() and not include_banner:
            logger.warning(
                "Publishing task panel without banner because attach_files is unavailable: guild_id=%s channel_id=%s",
                guild.id,
                channel.id,
            )

        message = await channel.send(**self._panel_message_options(include_banner=include_banner))
        try:
            store.rotate_panel(guild.id, channel.id, message.id)
        except Exception:
            # Do not leave an untracked fresh message if persistence failed.
            persisted = store.get_panel(guild.id)
            if persisted != {"channel_id": channel.id, "message_id": message.id}:
                try:
                    await message.delete()
                except disnake.HTTPException:
                    logger.exception(
                        "Could not remove an untracked task panel after database failure: guild_id=%s channel_id=%s message_id=%s",
                        guild.id,
                        channel.id,
                        message.id,
                    )
            raise

        logger.info(
            "Task panel published and saved: action=%s guild_id=%s channel_id=%s old_message_id=%s new_message_id=%s embeds=%s component_rows=%s attachments=%s",
            action,
            guild.id,
            channel.id,
            previous["message_id"] if previous else None,
            message.id,
            len(message.embeds),
            len(message.components),
            [attachment.filename for attachment in message.attachments],
        )
        await self._delete_queued_panels(guild)
        return message

    @commands.Cog.listener()
    async def on_ready(self) -> None:
        if self._startup_panel_check_done:
            return
        retry_needed = False
        for guild in self.bot.guilds:
            lock = self._panel_locks.setdefault(guild.id, asyncio.Lock())
            async with lock:
                saved = store.get_panel(guild.id)
                if saved is None:
                    continue
                channel = self.bot.get_channel(saved["channel_id"])
                if channel is None:
                    try:
                        channel = await self.bot.fetch_channel(saved["channel_id"])
                    except disnake.HTTPException:
                        logger.exception(
                            "Could not fetch saved task-panel channel: guild_id=%s channel_id=%s old_message_id=%s",
                            guild.id,
                            saved["channel_id"],
                            saved["message_id"],
                        )
                        retry_needed = True
                        continue
                if not isinstance(channel, disnake.TextChannel) or channel.guild.id != guild.id:
                    logger.error(
                        "Saved task-panel channel is unavailable or has wrong type: guild_id=%s channel_id=%s old_message_id=%s type=%s",
                        guild.id,
                        saved["channel_id"],
                        saved["message_id"],
                        type(channel).__name__,
                    )
                    retry_needed = True
                    continue
                try:
                    await self._publish_replacement(
                        guild,
                        channel,
                        previous=saved,
                        action="replace task panel on startup",
                    )
                    if store.get_panel_cleanup(guild.id):
                        retry_needed = True
                except Exception as exc:
                    logger.exception(
                        "Could not refresh task panel on startup: guild_id=%s channel_id=%s old_message_id=%s exception=%s http_status=%s discord_code=%s response=%s",
                        guild.id,
                        saved["channel_id"],
                        saved["message_id"],
                        type(exc).__name__,
                        getattr(exc, "status", None),
                        getattr(exc, "code", None),
                        getattr(exc, "text", str(exc)),
                    )
                    retry_needed = True
        self._startup_panel_check_done = not retry_needed

    @commands.slash_command(name="panel", description="Опубликовать панель заданий в канале")
    async def panel(
        self,
        inter: disnake.ApplicationCommandInteraction,
        channel: disnake.TextChannel,
    ) -> None:
        if inter.guild is None or not isinstance(inter.author, disnake.Member):
            await inter.response.send_message("Команда доступна только на сервере.", ephemeral=True)
            return
        if not inter.author.guild_permissions.administrator:
            await inter.response.send_message("Команда доступна только администраторам.", ephemeral=True)
            return
        if channel.guild.id != inter.guild.id:
            await inter.response.send_message("Выберите текстовый канал этого сервера.", ephemeral=True)
            return

        missing = self._log_channel_permissions(channel, "publish new task panel")
        if missing:
            await inter.response.send_message(
                "У бота не хватает прав в выбранном канале: " + ", ".join(missing) + ".",
                ephemeral=True,
            )
            return
        logger.info(
            "Panel command received: guild_id=%s channel_id=%s actor_id=%s",
            inter.guild.id,
            channel.id,
            inter.author.id,
        )
        await inter.response.defer(ephemeral=True)
        lock = self._panel_locks.setdefault(inter.guild.id, asyncio.Lock())
        async with lock:
            previous = store.get_panel(inter.guild.id)
            try:
                message = await self._publish_replacement(
                    inter.guild,
                    channel,
                    previous=previous,
                    action="publish new task panel from command",
                )
            except Exception as exc:
                logger.exception(
                    "Could not create task panel: guild_id=%s channel_id=%s actor_id=%s exception=%s http_status=%s discord_code=%s response=%s",
                    inter.guild.id,
                    channel.id,
                    inter.author.id,
                    type(exc).__name__,
                    getattr(exc, "status", None),
                    getattr(exc, "code", None),
                    getattr(exc, "text", str(exc)),
                )
                if isinstance(exc, PermissionError):
                    error_text = str(exc)
                elif isinstance(exc, disnake.HTTPException) and exc.code == 50013:
                    error_text = (
                        "Discord отказал из-за прав канала. Проверьте отправку сообщений, встраивание ссылок "
                        "и прикрепление файлов для роли бота."
                    )
                else:
                    error_text = "Проверьте логи `data/logs/bot.log` и повторите попытку."
                await inter.edit_original_response(content=f"Не удалось опубликовать панель. {error_text}")
                return

        if store.get_panel_cleanup(inter.guild.id):
            await inter.edit_original_response(
                content=f"Новая панель опубликована в {channel.mention}. Удаление старого сообщения будет повторено автоматически."
            )
        else:
            await inter.edit_original_response(content=f"Новая панель опубликована в {channel.mention}.")

    async def handle_panel_select(self, inter: disnake.MessageInteraction) -> None:
        if inter.guild is None:
            await inter.response.send_message("Панель доступна только на сервере.", ephemeral=True)
            return

        selected = inter.values[0] if inter.values else ""
        logger.info(
            "Task panel interaction received: interaction_id=%s guild_id=%s channel_id=%s user_id=%s message_id=%s custom_id=%s selected=%s response_done=%s",
            inter.id,
            inter.guild.id,
            inter.channel_id,
            inter.author.id,
            inter.message.id,
            getattr(getattr(inter, "component", None), "custom_id", None),
            selected,
            inter.response.is_done(),
        )
        if selected == "tasks":
            try:
                # A component interaction defaults to a deferred update of the
                # public panel. with_message=True creates the one ephemeral reply.
                await inter.response.defer(with_message=True, ephemeral=True)
            except disnake.HTTPException as exc:
                if exc.code in {10062, 40060}:
                    logger.warning(
                        "Could not acknowledge task-panel interaction: interaction_id=%s guild_id=%s user_id=%s message_id=%s response_done=%s http_status=%s discord_code=%s response=%s",
                        inter.id,
                        inter.guild.id,
                        inter.author.id,
                        inter.message.id,
                        inter.response.is_done(),
                        getattr(exc, "status", None),
                        exc.code,
                        getattr(exc, "text", str(exc)),
                    )
                    return
                raise

            panel = store.get_panel(inter.guild.id)
            if panel is None or panel["message_id"] != inter.message.id or panel["channel_id"] != inter.channel_id:
                await inter.edit_original_response(
                    content="Эта панель уже не активна. Попросите администратора обновить её."
                )
                return

            try:
                tasks = store.list_tasks(inter.guild.id)
                applications = {
                    task["id"]: store.list_applications(inter.guild.id, task["id"])
                    for task in tasks
                }
                pages = build_task_pages(
                    tasks=tasks,
                    applications_by_task=applications,
                    username=inter.author.display_name,
                    avatar_url=inter.author.display_avatar.url,
                )
                response_options = {
                    "embed": pages[0],
                    "allowed_mentions": ALLOWED_MENTIONS,
                }
                if len(pages) > 1:
                    response_options["view"] = TaskPagesView(pages, inter.author.id)
                first_banner = make_banner_file()
                if first_banner is not None:
                    response_options["file"] = first_banner
                await inter.edit_original_response(**response_options)
                logger.info(
                    "Task list displayed in one ephemeral response: interaction_id=%s guild_id=%s user_id=%s task_count=%s page_count=%s",
                    inter.id,
                    inter.guild.id,
                    inter.author.id,
                    len(tasks),
                    len(pages),
                )
            except Exception:
                logger.exception("Could not display task-panel content for interaction %s", inter.id)
                await inter.edit_original_response(content="Не удалось загрузить список заданий. Повторите попытку.")
        elif selected == "get_task":
            response_done_before_send = inter.response.is_done()
            if response_done_before_send:
                logger.error(
                    "Task-request panel interaction already acknowledged before modal send: interaction_id=%s guild_id=%s user_id=%s message_id=%s",
                    inter.id,
                    inter.guild.id,
                    inter.author.id,
                    inter.message.id,
                )
                try:
                    await inter.followup.send(
                        "Запрос уже был обработан. Если форма не открылась, выберите Get task ещё раз.",
                        ephemeral=True,
                    )
                except disnake.HTTPException:
                    logger.exception("Could not notify about acknowledged task-request interaction %s", inter.id)
                return
            try:
                await inter.response.send_modal(
                    TaskRequestModal(
                        self.bot,
                        inter.author.id,
                        inter.guild.id,
                        inter.id,
                        panel_channel_id=inter.channel_id,
                        panel_message_id=inter.message.id,
                    )
                )
                logger.info(
                    "Task-request modal opened: interaction_id=%s guild_id=%s user_id=%s message_id=%s",
                    inter.id,
                    inter.guild.id,
                    inter.author.id,
                    inter.message.id,
                )
            except disnake.HTTPException as exc:
                logger.exception(
                    "Could not open task-request modal: interaction_id=%s guild_id=%s user_id=%s message_id=%s response_done_before_send=%s http_status=%s discord_code=%s response=%s",
                    inter.id,
                    inter.guild.id,
                    inter.author.id,
                    inter.message.id,
                    response_done_before_send,
                    getattr(exc, "status", None),
                    exc.code,
                    getattr(exc, "text", str(exc)),
                )
                if exc.code == 40060:
                    try:
                        await inter.followup.send(
                            "Запрос уже был обработан. Если форма не открылась, выберите Get task ещё раз.",
                            ephemeral=True,
                        )
                    except disnake.HTTPException:
                        logger.exception("Could not send fallback for task-request interaction %s", inter.id)
                    return
                if exc.code == 10062:
                    logger.warning("Task-request interaction expired before modal open: interaction_id=%s", inter.id)
                    return
                raise
        else:
            await inter.response.send_message("Неизвестный пункт меню.", ephemeral=True)
