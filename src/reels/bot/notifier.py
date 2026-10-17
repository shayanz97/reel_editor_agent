"""Tells clients and the admin what the worker did, by polling the database.

Every notice is marked as sent only after Telegram accepted it (at-least-once). A notice
that keeps failing (e.g. the client blocked the bot) is given up after a few tries.
"""

from __future__ import annotations

import asyncio
from collections import Counter
from collections.abc import Callable
from contextlib import suppress

from aiogram import Bot
from aiogram.exceptions import TelegramAPIError, TelegramBadRequest
from aiogram.types import FSInputFile
from sqlalchemy import Engine
from sqlalchemy.orm import Session

from reels.bot import service, texts
from reels.bot.common import FILE_TIMEOUT_S, db
from reels.bot.keyboards import correction_keyboard, preview_keyboard, vocab_keyboard
from reels.bot.service import (
    AdminJobNotice,
    CorrectionFailedNotice,
    CorrectionPlannedNotice,
    CorrectionQuestionNotice,
    FailureNotice,
    Notice,
    PreviewNotice,
    ProcessingNotice,
    VocabNotice,
)
from reels.config import Settings
from reels.log import get_logger, log_context

log = get_logger(__name__)

MAX_SEND_FAILURES = 3


class Notifier:
    def __init__(self, bot: Bot, engine: Engine, app: Settings, interval_s: float = 2.0) -> None:
        self.bot = bot
        self.engine = engine
        self.app = app
        self.interval_s = interval_s
        self.failures: Counter[tuple[str, int]] = Counter()

    async def run(self) -> None:
        while True:
            try:
                await self.tick()
            except Exception:
                log.exception("notifier_tick_failed")
            await asyncio.sleep(self.interval_s)

    async def tick(self) -> int:
        notices = await db(self.engine, lambda s: service.pending_notices(s, self.app))
        sent = 0
        for notice in notices:
            try:
                message_id = await self.send(notice)
            except TelegramAPIError:
                key = _key(notice)
                self.failures[key] += 1
                log.exception("notice_failed", extra={"notice": type(notice).__name__})
                if self.failures[key] < MAX_SEND_FAILURES:
                    continue
                log.error("notice_given_up", extra={"notice": repr(notice)})
                message_id = None
            await db(self.engine, _marker(notice, message_id))
            sent += 1
        return sent

    async def send(self, notice: Notice) -> int | dict[int, int] | None:
        match notice:
            case VocabNotice():
                return await self._vocab(notice)
            case ProcessingNotice():
                with log_context(project_id=notice.project_id):
                    return await self._status(
                        notice.chat_id, notice.status_message_id, texts.PROCESSING
                    )
            case PreviewNotice():
                with log_context(project_id=notice.project_id, version_id=notice.version_id):
                    return await self._preview(notice)
            case FailureNotice():
                text = texts.failure_text(notice.error_code, notice.error_details)
                await self._status(notice.chat_id, notice.status_message_id, text)
                return None
            case CorrectionPlannedNotice():
                sent = await self.bot.send_message(
                    notice.chat_id,
                    texts.correction_summary(notice.operations, notice.version_number),
                    reply_markup=correction_keyboard(notice.correction_id),
                )
                return sent.message_id
            case CorrectionQuestionNotice():
                sent = await self.bot.send_message(
                    notice.chat_id,
                    texts.correction_question(
                        notice.issues, notice.clarification, notice.version_number
                    ),
                )
                return sent.message_id
            case CorrectionFailedNotice():
                text = (
                    texts.CORRECTION_UNAVAILABLE
                    if notice.error == "llm_unavailable"
                    else texts.CORRECTION_FAILED
                )
                await self.bot.send_message(notice.chat_id, text)
                return None
            case AdminJobNotice():
                if self.app.admin_chat_id is None:
                    log.warning("admin_chat_missing", extra={"job_id": notice.job_id})
                    return None
                await self.bot.send_message(
                    self.app.admin_chat_id,
                    texts.admin_job_failed(
                        notice.job_id,
                        notice.step,
                        notice.attempts,
                        notice.error,
                        notice.client_name,
                        notice.project_id,
                        notice.version_number,
                    ),
                )
                return None

    async def _vocab(self, n: VocabNotice) -> dict[int, int]:
        """Intro + one message per word, each with its own buttons."""
        await self.bot.send_message(n.chat_id, texts.VOCAB_INTRO.format(count=len(n.questions)))
        sent: dict[int, int] = {}
        for q in n.questions:
            message = await self.bot.send_message(
                n.chat_id,
                texts.VOCAB_QUESTION.format(segment=q.segment, context=q.context, text=q.text),
                reply_markup=vocab_keyboard(n.version_id, q.id, q.suggestion),
            )
            sent[q.id] = message.message_id
        return sent

    async def _status(self, chat_id: int, message_id: int | None, text: str) -> int:
        """Edit the project's status message; send a new one if that is not possible.

        Returns the id of the message that now shows the status."""
        if message_id is not None:
            try:
                await self.bot.edit_message_text(text=text, chat_id=chat_id, message_id=message_id)
                return message_id
            except TelegramBadRequest:
                log.info("status_edit_failed", extra={"message_id": message_id})
        sent = await self.bot.send_message(chat_id, text)
        return sent.message_id

    async def _preview(self, n: PreviewNotice) -> int:
        await self._status(
            n.chat_id,
            n.status_message_id,
            texts.PREVIEW_READY_STATUS.format(number=n.version_number),
        )
        await self.bot.send_video(
            n.chat_id,
            FSInputFile(n.export, filename=f"vorschau_v{n.version_number}.mp4"),
            caption=texts.PREVIEW_CAPTION.format(number=n.version_number),
            width=1080,
            height=1920,
            duration=round(n.duration_s) if n.duration_s else None,
            supports_streaming=True,
            request_timeout=FILE_TIMEOUT_S,
        )
        messages = texts.preview_messages(n.segments, n.removed)
        for text in messages[:-1]:
            await self.bot.send_message(n.chat_id, text)
        last = await self.bot.send_message(
            n.chat_id, messages[-1], reply_markup=preview_keyboard(n.version_id)
        )
        return last.message_id


def _key(notice: Notice) -> tuple[str, int]:
    """Stable identity of a notice (the dataclasses hold lists, so they are not hashable)."""
    match notice:
        case VocabNotice():
            return ("vocab", notice.version_id)
        case ProcessingNotice():
            return ("processing", notice.project_id)
        case PreviewNotice():
            return ("preview", notice.version_id)
        case FailureNotice():
            return ("failure", notice.version_id)
        case AdminJobNotice():
            return ("admin", notice.job_id)
        case CorrectionPlannedNotice() | CorrectionQuestionNotice() | CorrectionFailedNotice():
            return ("correction", notice.correction_id)


def _marker(notice: Notice, message_id: int | dict[int, int] | None) -> Callable[[Session], None]:
    return lambda session: service.mark_notice_sent(session, notice, message_id)


async def stop(task: asyncio.Task[None]) -> None:
    task.cancel()
    with suppress(asyncio.CancelledError):
        await task
