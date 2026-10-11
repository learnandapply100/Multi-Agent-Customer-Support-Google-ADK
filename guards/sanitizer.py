"""The Sanitizer: the first and cheapest guard (SPEC S-1, S-2).

Plain Python, no model call, so it takes well under a millisecond. It does three checks:
a length limit, a character allow-list, and two very obvious patterns. Anything subtle
is left to the Judge.
"""

import re
import unicodedata
from typing import NamedTuple

MAX_CHARS = 1000  # a support message is a few sentences; the web UI will answer 413 above this

# S-2: apostrophes, # $ : ; @ and ordinary punctuation are ALLOWED. The second line is what
# phones type for you: curly quotes, long dashes and "...".
ALLOWED_PUNCTUATION = set(
    " \n\t.,!?;:'\"@#$%&*()-_+=/[]>~"
    "‘’“”–—…‍"
)
# Letters, accents, digits, spaces, currency signs and emoji, in any language.
ALLOWED_CATEGORIES = ("L", "M", "N", "Zs", "Sc", "So")
# What is left out on purpose:  < { } \ ` | ^  and invisible control characters.
# A customer almost never needs them, and markup and template attacks cannot be written
# without them.

CHEAP_PATTERNS = [
    ("path traversal", re.compile(r"\.\./|\.\.\\")),
    ("system file path", re.compile(r"/etc/(passwd|shadow)\b", re.IGNORECASE)),
]


class SanitizeResult(NamedTuple):
    ok: bool      # True = the message may go on to the Judge
    detail: str   # shown in the step event, so say exactly what was checked or found


def _allowed(ch: str) -> bool:
    return ch in ALLOWED_PUNCTUATION or unicodedata.category(ch).startswith(ALLOWED_CATEGORIES)


def sanitize(message: str) -> SanitizeResult:
    if len(message) > MAX_CHARS:
        return SanitizeResult(False, f"message too long: {len(message)} characters (limit {MAX_CHARS})")
    for ch in message:
        if not _allowed(ch):
            return SanitizeResult(False, f"character not allowed: {ch!r}")
    for name, pattern in CHEAP_PATTERNS:
        if pattern.search(message):
            return SanitizeResult(False, f"matched pattern: {name}")
    return SanitizeResult(True, f"{len(message)} characters, all allowed, no obvious patterns")
