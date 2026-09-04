"""Normalization boundary for untrusted source text."""

from __future__ import annotations

import re
import unicodedata

_ANSI_ESCAPE_RE = re.compile(
    r"(?:\x1b\[|\x9b)[0-?]{0,16}[ -/]{0,4}[@-~]"
    r"|(?:\x1b\]|\x9d)[^\x07\x1b\x9c]{0,1024}(?:\x07|\x1b\\|\x9c)"
)
_EXPLICIT_FORMAT_CONTROLS = frozenset(
    {
        "\u200b",  # zero-width space
        "\u200c",  # zero-width non-joiner
        "\u200d",  # zero-width joiner
        "\u202a",  # left-to-right embedding
        "\u202b",  # right-to-left embedding
        "\u202c",  # pop directional formatting
        "\u202d",  # left-to-right override
        "\u202e",  # right-to-left override
        "\u2060",  # word joiner
        "\u2066",  # left-to-right isolate
        "\u2067",  # right-to-left isolate
        "\u2068",  # first strong isolate
        "\u2069",  # pop directional isolate
        "\ufeff",  # zero-width no-break space
    }
)
_FRAMING_TRANSLATION = str.maketrans(
    {
        "`": "｀",
        "{": "｛",
        "}": "｝",
        "[": "［",
        "]": "］",
        "<": "＜",
        ">": "＞",
    }
)


def _canonical_source_text(value: str) -> str:
    normalized = unicodedata.normalize("NFC", value)
    without_ansi = _ANSI_ESCAPE_RE.sub("", normalized)
    with_spaces = without_ansi.replace("\r", " ").replace("\t", " ")
    without_controls = "".join(
        character
        for character in with_spaces
        if character not in _EXPLICIT_FORMAT_CONTROLS
        and (unicodedata.category(character) not in {"Cc", "Cf"} or character == "\n")
    )
    return without_controls.translate(_FRAMING_TRANSLATION)


def sanitize_source_text(value: str, *, limit: int = 4096) -> str:
    """Return a deterministic, inert representation of untrusted source text."""
    if not isinstance(value, str):
        raise TypeError("value must be a string")
    if type(limit) is not int:
        raise TypeError("limit must be an integer")
    if not 1 <= limit <= 4096:
        raise ValueError("limit must be between 1 and 4096")

    return _canonical_source_text(value)[:limit]
