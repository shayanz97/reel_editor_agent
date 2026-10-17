"""Inline keyboards and their callback data."""

from __future__ import annotations

from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

from reels.bot import texts

APPROVE = "approve"  # + version id
CHANGE = "change"  # + version id
APPLY = "apply"  # + correction id
RETRY = "retry"  # + correction id
GLOSSARY_ADD = "gladd"  # + correction id + operation index
GLOSSARY_SKIP = "glskip"  # + correction id + operation index
VOCAB_KEEP = "vkeep"  # + version id + question id
VOCAB_SUGGESTION = "vsugg"  # + version id + question id

_ACTIONS = {APPROVE: 1, CHANGE: 1, APPLY: 1, RETRY: 1, GLOSSARY_ADD: 2, GLOSSARY_SKIP: 2,
            VOCAB_KEEP: 2, VOCAB_SUGGESTION: 2}  # fmt: skip


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


def correction_keyboard(correction_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text=texts.BUTTON_APPLY, callback_data=f"{APPLY}:{correction_id}"
                ),
                InlineKeyboardButton(
                    text=texts.BUTTON_RETRY, callback_data=f"{RETRY}:{correction_id}"
                ),
            ]
        ]
    )


def glossary_keyboard(correction_id: int, index: int) -> InlineKeyboardMarkup:
    data = f"{correction_id}:{index}"
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text=texts.BUTTON_GLOSSARY_YES, callback_data=f"{GLOSSARY_ADD}:{data}"
                ),
                InlineKeyboardButton(
                    text=texts.BUTTON_GLOSSARY_NO, callback_data=f"{GLOSSARY_SKIP}:{data}"
                ),
            ]
        ]
    )


def vocab_keyboard(
    version_id: int, question_id: int, suggestion: str | None
) -> InlineKeyboardMarkup:
    data = f"{version_id}:{question_id}"
    row = [InlineKeyboardButton(text=texts.BUTTON_VOCAB_KEEP, callback_data=f"{VOCAB_KEEP}:{data}")]
    if suggestion:
        row.append(
            InlineKeyboardButton(
                text=texts.BUTTON_VOCAB_SUGGESTION.format(suggestion=suggestion)[:60],
                callback_data=f"{VOCAB_SUGGESTION}:{data}",
            )
        )
    return InlineKeyboardMarkup(inline_keyboard=[row])


def parse_callback(data: str | None) -> tuple[str, tuple[int, ...]] | None:
    """'approve:12' -> ('approve', (12,)); 'gladd:5:0' -> ('gladd', (5, 0)).

    Unknown actions, wrong number of ids or non-numeric ids (forged data) -> None.
    """
    action, *raw = (data or "").split(":")
    if action not in _ACTIONS or len(raw) != _ACTIONS[action]:
        return None
    if not all(part.isdigit() for part in raw):
        return None
    return action, tuple(int(part) for part in raw)
