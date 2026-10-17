"""Inline keyboards and their callback data."""

from __future__ import annotations

from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

from reels.bot import texts

APPROVE = "approve"
CHANGE = "change"


def preview_keyboard(version_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text=texts.BUTTON_APPROVE, callback_data=f"{APPROVE}:{version_id}"
                ),
                InlineKeyboardButton(
                    text=texts.BUTTON_CHANGE, callback_data=f"{CHANGE}:{version_id}"
                ),
            ]
        ]
    )


def parse_callback(data: str | None) -> tuple[str, int] | None:
    """'approve:12' -> ('approve', 12). Anything else (forged, outdated format) -> None."""
    action, sep, raw_id = (data or "").partition(":")
    if not sep or action not in (APPROVE, CHANGE) or not raw_id.isdigit():
        return None
    return action, int(raw_id)
