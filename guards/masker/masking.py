"""The Masker's core: find personal data in a reply and hide it.

This file has no network code and no model call. It is plain Python, so it
always gives the same answer for the same text, and it is easy to test.

SPEC K-2: change ONLY PII (emails other than the user's own, phone numbers,
card-like numbers). Never change case, whitespace or anything else.
SPEC K-3: say what was masked, or "nothing to mask".
"""

import re

# What we put in place of each kind of personal data.
PLACEHOLDERS = {
    "email": "[EMAIL]",
    "phone": "[PHONE]",
    "card": "[CARD]",
}

# Words used in the human-readable detail line (singular, plural).
LABELS = {
    "email": ("email address", "email addresses"),
    "phone": ("phone number", "phone numbers"),
    "card": ("card number", "card numbers"),
}

# --- The three patterns -----------------------------------------------------
#
# They are joined into ONE regular expression and the text is scanned once.
# Order matters: the email pattern is tried first, so the digits inside an
# email address can never be mistaken for a phone number.

# name@domain.tld
_EMAIL = r"(?P<email>[A-Za-z0-9._%+-]+@[A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)+)"

# 13 to 19 digits, optionally split by single spaces or hyphens.
# Example: 4111 1111 1111 1111
_CARD = r"(?P<card>(?<![\d-])\d(?:[ -]?\d){12,18}(?!-?\d))"

# Two phone shapes:
#   long : +1 (206) 555-0100, 206-555-0100, 206 555 0100, 206.555.0100
#   short: 555-0100, 555.0100   (a hyphen or dot is required, so "250 2026"
#          in a sentence is not treated as a phone number)
# The checks at both ends stop us from cutting a piece out of a longer
# number such as a price (1500.00) or a date (2026-10-11).
_PHONE = (
    r"(?P<phone>(?<![\d.\-])"
    r"(?:"
    r"(?:\+?\d{1,3}[ .-]?)?\(?\d{3}\)?[ .-]?\d{3}[ .-]\d{4}"
    r"|"
    r"\d{3}[-.]\d{4}"
    r")"
    r"(?!\d|[-.]\d))"
)

_PII = re.compile(f"{_EMAIL}|{_CARD}|{_PHONE}")


def mask_text(text: str, user_email: str) -> dict:
    """Hide PII in `text`. The logged-in user's own email is left alone.

    Returns a dict:
        masked_text : the text with PII replaced by placeholders
        counts      : {"email": n, "phone": n, "card": n}
        total       : how many things were masked in all
        detail      : "masked 1 phone number" or "nothing to mask"
    """
    counts = {"email": 0, "phone": 0, "card": 0}
    own_email = (user_email or "").strip().lower()

    def replace(match: re.Match) -> str:
        kind = match.lastgroup  # "email", "phone" or "card"
        found = match.group(0)

        # The customer's own email is not a leak. Give it back untouched.
        if kind == "email" and found.lower() == own_email:
            return found

        counts[kind] += 1
        return PLACEHOLDERS[kind]

    # re.sub only replaces the matched pieces. Everything else in the reply
    # (capital letters, spaces, line breaks) comes back exactly as it was.
    masked_text = _PII.sub(replace, text)

    return {
        "masked_text": masked_text,
        "counts": counts,
        "total": sum(counts.values()),
        "detail": _describe(counts),
    }


def _describe(counts: dict) -> str:
    """Turn the counts into the step's detail line (SPEC K-3)."""
    parts = []
    for kind in ("email", "phone", "card"):
        n = counts[kind]
        if n:
            singular, plural = LABELS[kind]
            parts.append(f"{n} {singular if n == 1 else plural}")
    if not parts:
        return "nothing to mask"
    return "masked " + ", ".join(parts)
