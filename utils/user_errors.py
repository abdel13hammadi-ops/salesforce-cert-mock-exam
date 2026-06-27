"""Sanitized learner-facing error messages and server-side logging helpers."""

from __future__ import annotations

import logging
from typing import Optional

logger = logging.getLogger(__name__)

PRACTICE_SAVE_ERROR_MESSAGE = (
    "Practice completed, but your result could not be saved. Please try again later."
)
PROGRESS_LOAD_ERROR_MESSAGE = (
    "Progress could not be loaded right now. Please refresh and try again."
)
EXAM_BANK_LOAD_ERROR_MESSAGE = (
    "This exam could not be loaded right now. Check your certification and language "
    "settings, then try again."
)


def log_and_get_user_message(
    context: str,
    user_message: str,
    *,
    exc: Optional[BaseException] = None,
) -> str:
    """Log technical details server-side and return a safe user-facing message."""
    if exc is not None:
        logger.exception("%s", context)
    else:
        logger.error("%s", context)
    return user_message
