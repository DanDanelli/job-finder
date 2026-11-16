"""Telegram bot (python-telegram-bot, long polling). A thin layer over `actions`.

Only chats linked to a profile (`telegram_chat_id`) are served; the rest is ignored,
except /start, which replies with the chat_id for sign-up.
"""

import asyncio
import logging
from collections.abc import Awaitable, Callable, Coroutine, Sequence
from typing import Any

from sqlalchemy import Engine
from telegram import Bot, InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.constants import ParseMode
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

from job_finder.config import Settings
from job_finder.domain import MatchStatus
from job_finder.i18n import command, command_aliases, t
from job_finder.scheduler import Scheduler
from job_finder.telegram import actions
from job_finder.telegram.actions import ChatProfile
from job_finder.telegram.digest import Button, job_buttons

log = logging.getLogger(__name__)

Context = ContextTypes.DEFAULT_TYPE
Handler = Callable[[Update, Context, ChatProfile], Coroutine[Any, Any, None]]


def keyboard(buttons: Sequence[Button]) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [[InlineKeyboardButton(b.text, callback_data=b.callback, url=b.url) for b in buttons]]
    )


class TelegramSender:
    def __init__(self, bot: Bot) -> None:
        self.bot = bot

    async def send(self, chat_id: int, text: str, buttons: Sequence[Button] | None = None) -> None:
        await self.bot.send_message(
            chat_id,
            text,
            parse_mode=ParseMode.HTML,
            reply_markup=keyboard(buttons) if buttons else None,
            disable_web_page_preview=True,
        )


class Batcher:
    """Joins consecutive messages from the same chat before processing them.

    Telegram splits a long pasted text into several messages of up to 4096 characters;
    without joining them, each piece would become an evaluation.
    """

    def __init__(self, process: Callable[[int, str], Awaitable[None]], wait_s: float = 2.0):
        self.process = process
        self.wait_s = wait_s
        self._parts: dict[int, list[str]] = {}
        self._tasks: dict[int, asyncio.Task[None]] = {}

    def add(self, chat_id: int, text: str) -> None:
        self._parts.setdefault(chat_id, []).append(text)
        previous = self._tasks.pop(chat_id, None)
        if previous is not None:
            previous.cancel()
        self._tasks[chat_id] = asyncio.create_task(self._later(chat_id))

    async def _later(self, chat_id: int) -> None:
        await asyncio.sleep(self.wait_s)
        # Leave the registry before processing: a new message does not cancel what started.
        self._tasks.pop(chat_id, None)
        text = "\n".join(self._parts.pop(chat_id, []))
        try:
            await self.process(chat_id, text)
        except Exception:
            log.exception("failed to process a message from chat %s", chat_id)


def register_handlers(
    app: Application,
    engine: Engine,
    settings: Settings,
    scheduler: Scheduler,
) -> None:
    lang = settings.language

    def authorized(handler: Handler) -> Callable[[Update, Context], Coroutine[Any, Any, None]]:
        async def wrapper(update: Update, context: Context) -> None:
            chat = update.effective_chat
            if chat is None:
                return
            profile = await asyncio.to_thread(actions.profile_for_chat, engine, chat.id)
            if profile is None:
                log.warning("chat %s not authorized", chat.id)
                return
            await handler(update, context, profile)

        return wrapper

    async def reply(update: Update, text: str) -> None:
        if update.effective_message:
            await update.effective_message.reply_text(
                text, parse_mode=ParseMode.HTML, disable_web_page_preview=True
            )

    async def start(update: Update, context: Context) -> None:
        chat = update.effective_chat
        if chat is None:
            return
        profile = await asyncio.to_thread(actions.profile_for_chat, engine, chat.id)
        if profile is None:
            log.info("/start from an unregistered chat: %s (%s)", chat.id, chat.full_name)
            await reply(update, t(lang, "start.unknown", chat_id=chat.id))
            return
        commands = " · ".join(
            f"/{command(c, lang)}" for c in ("search", "saved", "pause", "status")
        )
        await reply(
            update,
            t(
                lang,
                "start.known",
                name=profile.name,
                times=", ".join(h.strftime("%H:%M") for h in settings.run_times),
                commands=commands,
                how=t(lang, "evaluate.how"),
            ),
        )

    sender = TelegramSender(app.bot)

    async def evaluate_and_reply(chat_id: int, text: str) -> None:
        profile = await asyncio.to_thread(actions.profile_for_chat, engine, chat_id)
        if profile is None:
            return
        await sender.send(chat_id, t(lang, "evaluate.working"))
        try:
            answer, buttons = await asyncio.to_thread(
                actions.evaluate_in_chat, engine, settings, profile.id, text
            )
        except Exception:
            log.exception("ad-hoc evaluation failed")
            answer, buttons = t(lang, "evaluate.error"), []
        await sender.send(chat_id, answer, buttons)

    batcher = Batcher(evaluate_and_reply)

    @authorized
    async def evaluate(update: Update, context: Context, profile: ChatProfile) -> None:
        # The whole message text, not `context.args`, to keep the line breaks.
        message = update.effective_message.text if update.effective_message else None
        parts = (message or "").split(maxsplit=1)
        text = parts[1] if len(parts) > 1 else ""
        if not text or update.effective_chat is None:
            await reply(update, t(lang, "evaluate.how"))
            return
        await evaluate_and_reply(update.effective_chat.id, text)

    @authorized
    async def free_text(update: Update, context: Context, profile: ChatProfile) -> None:
        message, chat = update.effective_message, update.effective_chat
        if not (chat and message and message.text):
            return
        # A reply to a job's message (one with 👍/👎) is a note on that job. Any other
        # reply to the bot is not a job to evaluate either (that would spend Claude on
        # a note): it gets a hint instead.
        target = message.reply_to_message
        if target is not None and target.from_user is not None and target.from_user.is_bot:
            markup = target.reply_markup
            callbacks = [
                b.callback_data
                for row in (markup.inline_keyboard if markup else ())
                for b in row
                if isinstance(b.callback_data, str)
            ]
            match_id = actions.match_in_buttons(callbacks)
            if match_id is None:
                await reply(update, t(lang, "feedback.reply_hint"))
                return
            note = await asyncio.to_thread(
                actions.record_note, engine, chat.id, match_id, message.text
            )
            if note is None:
                await reply(update, t(lang, "feedback.invalid"))
            else:
                await reply(update, t(lang, "feedback.noted" if note else "feedback.note_cleared"))
            return
        batcher.add(chat.id, message.text)

    @authorized
    async def search(update: Update, context: Context, profile: ChatProfile) -> None:
        if scheduler.busy:
            await reply(update, t(lang, "search.busy"))
            return
        await reply(update, t(lang, "search.started"))
        report = await scheduler.run_and_notify([profile.name], kind="on_demand")
        if report is None:
            await reply(update, t(lang, "search.no_profile"))

    @authorized
    async def saved(update: Update, context: Context, profile: ChatProfile) -> None:
        await reply(update, await asyncio.to_thread(actions.saved_text, engine, profile.id, lang))

    @authorized
    async def pause(update: Update, context: Context, profile: ChatProfile) -> None:
        paused = await asyncio.to_thread(actions.toggle_pause, engine, profile.id)
        await reply(
            update,
            t(lang, "pause.on", command=command("pause", lang)) if paused else t(lang, "pause.off"),
        )

    @authorized
    async def status(update: Update, context: Context, profile: ChatProfile) -> None:
        text = await asyncio.to_thread(actions.status_text, engine, profile.id, settings)
        await reply(update, text)

    async def feedback(update: Update, context: Context) -> None:
        query = update.callback_query
        chat = update.effective_chat
        if query is None or chat is None or not query.data:
            return
        result = await asyncio.to_thread(actions.record_feedback, engine, chat.id, query.data)
        if result is None:
            await query.answer(t(lang, "feedback.invalid"))
            return
        saved_now = result.status == MatchStatus.SAVED
        await query.answer(t(lang, "feedback.saved" if saved_now else "feedback.dismissed"))
        await query.edit_message_reply_markup(
            keyboard(job_buttons(result.match_id, result.url, lang, result.status))
        )

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler(command_aliases("search"), search, block=False))
    app.add_handler(CommandHandler(command_aliases("saved"), saved))
    app.add_handler(CommandHandler(command_aliases("pause"), pause))
    app.add_handler(CommandHandler(command_aliases("status"), status))
    app.add_handler(CommandHandler(command_aliases("evaluate"), evaluate, block=False))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, free_text))
    app.add_handler(CallbackQueryHandler(feedback, pattern=r"^fb:"))


def make_application(token: str) -> Application:
    # Timeouts above the default (5 s): occasional slow connections do not drop a send.
    return (
        Application.builder()
        .token(token)
        .connect_timeout(20)
        .read_timeout(20)
        .write_timeout(20)
        .build()
    )
