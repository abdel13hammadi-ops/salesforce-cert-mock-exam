"""User-facing Supabase password-update error classification for Reset Password."""

from __future__ import annotations

import logging
from typing import Tuple

try:
    from supabase_auth.errors import AuthApiError, AuthWeakPasswordError
except ImportError:  # pragma: no cover - older client installs
    try:
        from gotrue.errors import AuthApiError, AuthWeakPasswordError
    except ImportError:
        AuthApiError = Exception  # type: ignore[misc, assignment]
        AuthWeakPasswordError = Exception  # type: ignore[misc, assignment]

logger = logging.getLogger(__name__)

CATEGORY_SAME_PASSWORD = "same_password"
CATEGORY_VALIDATION = "validation"
CATEGORY_RECOVERY_INVALID = "recovery_invalid"
CATEGORY_UNEXPECTED = "unexpected"

SAME_PASSWORD_MESSAGE = "Your new password must be different from your current password."
EXPIRED_RECOVERY_LINK_MESSAGE = (
    "Could not update password. The reset link may be expired or already used."
)
UNEXPECTED_PASSWORD_UPDATE_MESSAGE = (
    "Could not update password right now. Please try again in a moment."
)
WEAK_PASSWORD_MESSAGE = (
    "Password does not meet security requirements. Use at least 8 characters and "
    "avoid common or leaked passwords."
)
VALIDATION_PASSWORD_MESSAGE = (
    "Password update rejected. Check the password requirements and try again."
)

RECOVERY_INVALID_ERROR_CODES = frozenset({
    "session_not_found",
    "flow_state_expired",
    "flow_state_not_found",
    "otp_expired",
    "invalid_jwt",
    "bad_jwt",
    "reauthentication_not_valid",
    "reauthentication_needed",
})

VALIDATION_ERROR_CODES = frozenset({
    "weak_password",
    "validation_failed",
})


def _error_message(exc: Exception) -> str:
    message = getattr(exc, "message", None)
    if message:
        return str(message).strip()
    return str(exc or "").strip()


def _error_code(exc: Exception) -> str:
    code = getattr(exc, "code", None)
    if code in (None, ""):
        return ""
    return str(code).strip().lower()


def _normalized_error_text(exc: Exception) -> str:
    return f"{_error_code(exc)} {_error_message(exc)}".strip().lower()


def is_same_password_error(exc: Exception) -> bool:
    if _error_code(exc) == "same_password":
        return True
    text = _normalized_error_text(exc)
    return (
        "same_password" in text
        or "same password" in text
        or (
            "different" in text
            and "password" in text
            and "current" in text
        )
        or (
            "different" in text
            and "password" in text
            and "old" in text
        )
    )


def is_recovery_session_invalid_error(exc: Exception) -> bool:
    if _error_code(exc) in RECOVERY_INVALID_ERROR_CODES:
        return True
    text = _normalized_error_text(exc)
    markers = (
        "invalid refresh token",
        "refresh token not found",
        "already been used",
        "already used",
        "jwt expired",
        "token expired",
        "auth session missing",
        "session expired",
        "otp expired",
        "invalid claim",
        "flow state expired",
        "session not found",
    )
    return any(marker in text for marker in markers)


def is_password_validation_error(exc: Exception) -> bool:
    if isinstance(exc, AuthWeakPasswordError):
        return True
    if _error_code(exc) in VALIDATION_ERROR_CODES:
        return True
    text = _normalized_error_text(exc)
    return "weak password" in text or "password strength" in text


def classify_password_update_error(exc: Exception) -> Tuple[str, str]:
    """Map a Supabase password update failure to a safe user-facing message."""
    if is_same_password_error(exc):
        return CATEGORY_SAME_PASSWORD, SAME_PASSWORD_MESSAGE
    if is_password_validation_error(exc):
        if isinstance(exc, AuthWeakPasswordError) or _error_code(exc) == "weak_password":
            return CATEGORY_VALIDATION, WEAK_PASSWORD_MESSAGE
        return CATEGORY_VALIDATION, VALIDATION_PASSWORD_MESSAGE
    if is_recovery_session_invalid_error(exc):
        return CATEGORY_RECOVERY_INVALID, EXPIRED_RECOVERY_LINK_MESSAGE
    return CATEGORY_UNEXPECTED, UNEXPECTED_PASSWORD_UPDATE_MESSAGE


def classify_recovery_session_error(exc: Exception) -> Tuple[str, str]:
    """Map a Supabase recovery session bootstrap failure to a safe user-facing message."""
    if is_recovery_session_invalid_error(exc):
        return CATEGORY_RECOVERY_INVALID, EXPIRED_RECOVERY_LINK_MESSAGE
    return CATEGORY_UNEXPECTED, UNEXPECTED_PASSWORD_UPDATE_MESSAGE


def log_password_reset_failure(context: str, exc: BaseException) -> None:
    """Log password-reset failures server-side without secrets or recovery tokens."""
    logger.error(
        "%s error_type=%s error_code=%s",
        context,
        type(exc).__name__,
        _error_code(exc) or "unknown",
    )
