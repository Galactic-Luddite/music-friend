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


# Display names (artist/track/release/event/venue names, and imported listening
# history track/artist/album names) allow two zero-width format characters that
# the general `sanitize_source_text` boundary below strips along with every
# other Unicode control (Cc) and format (Cf) character: ZERO WIDTH JOINER
# (U+200D), required to keep multi-codepoint emoji ZWJ sequences (for example
# skin-tone and gender modifiers, or a family emoji) rendering as one glyph
# instead of several, and ZERO WIDTH NON-JOINER (U+200C), required by some
# scripts -- for example Persian and several Indic scripts -- to select the
# correct joined or disjoined glyph shape. Every other control/format
# character, including the bidirectional override/embedding/isolate controls
# U+202A-U+202E and U+2066-U+2069, is still stripped for display names. This
# allowlist is also documented in docs/limits.md; keep both in step.
_DISPLAY_NAME_ALLOWED_FORMAT_CONTROLS = frozenset({"‌", "‍"})


def _canonical_source_text(
    value: str, *, keep_format_controls: frozenset[str] = frozenset()
) -> str:
    normalized = unicodedata.normalize("NFC", value)
    without_ansi = _ANSI_ESCAPE_RE.sub("", normalized)
    with_spaces = without_ansi.replace("\r", " ").replace("\t", " ")
    without_controls = "".join(
        character
        for character in with_spaces
        if character in keep_format_controls
        or (
            character not in _EXPLICIT_FORMAT_CONTROLS
            and (unicodedata.category(character) not in {"Cc", "Cf"} or character == "\n")
        )
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


def sanitize_display_name(value: str, *, limit: int = 4096) -> str:
    """Return a deterministic display name with bidi/control characters removed.

    Shares `sanitize_source_text`'s boundary (NFC normalization, ANSI-escape
    stripping, framing-character translation to full-width forms, and the
    length limit) but keeps the two zero-width format characters in
    `_DISPLAY_NAME_ALLOWED_FORMAT_CONTROLS` so legitimate emoji ZWJ sequences
    and scripts that require ZWNJ pass through unchanged. This is the single
    shared sanitizer for every place a display name enters Music Friend from
    an external source: Spotify history import, catalog/provider refresh
    (artist, track, release names), and Ticketmaster event/venue names.
    """
    if not isinstance(value, str):
        raise TypeError("value must be a string")
    if type(limit) is not int:
        raise TypeError("limit must be an integer")
    if not 1 <= limit <= 4096:
        raise ValueError("limit must be between 1 and 4096")

    return _canonical_source_text(
        value, keep_format_controls=_DISPLAY_NAME_ALLOWED_FORMAT_CONTROLS
    )[:limit]
