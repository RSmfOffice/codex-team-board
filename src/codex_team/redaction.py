from __future__ import annotations

import re


_SENSITIVE_KEY = (
    r"(?:claim[_-]?token|access[_-]?token|refresh[_-]?token|"
    r"id[_-]?token|api[_-]?key|client[_-]?secret|password|passwd|"
    r"authorization|secret|token)"
)
_QUOTED_VALUE_RE = re.compile(
    rf'(?i)(?P<prefix>(?:\\?")?{_SENSITIVE_KEY}(?:\\?")?\s*[:=]\s*\\?")'
    r'(?P<value>[^"\\\r\n]*)(?P<suffix>\\?")'
)
_ASSIGNMENT_RE = re.compile(
    rf"(?i)(?P<prefix>\b(?:[A-Za-z0-9]+_)?{_SENSITIVE_KEY}\s*=\s*)"
    r"(?P<value>[^\s,;&]+)"
)
_AUTHORIZATION_RE = re.compile(
    r"(?i)(?P<prefix>\bauthorization\s*[:=]\s*(?:bearer\s+)?)"
    r"(?P<value>[^\s,;&]+)"
)
_COMMAND_VALUE_RE = re.compile(
    rf"(?i)(?P<prefix>--{_SENSITIVE_KEY}(?:\s+|=))"
    r"(?P<value>[^\s,;&]+)"
)


def redact_sensitive_text(text: str) -> str:
    """Remove common credential values while preserving diagnostic context."""

    redacted = _QUOTED_VALUE_RE.sub(
        lambda match: match.group("prefix") + "[REDACTED]" + match.group("suffix"),
        str(text),
    )
    redacted = _AUTHORIZATION_RE.sub(
        lambda match: match.group("prefix") + "[REDACTED]",
        redacted,
    )
    redacted = _COMMAND_VALUE_RE.sub(
        lambda match: match.group("prefix") + "[REDACTED]",
        redacted,
    )
    return _ASSIGNMENT_RE.sub(
        lambda match: match.group("prefix") + "[REDACTED]",
        redacted,
    )
