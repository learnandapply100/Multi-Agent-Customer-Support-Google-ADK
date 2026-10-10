"""Login check. Plain code, no model involved (M-2, M-6).

It calls the `check-login` tool from the `login` toolset. The agent never loads that
toolset, so it cannot call this tool.
"""

import json
import os

from toolbox_core import ToolboxClient

TOOLBOX_URL = os.getenv("TOOLBOX_URL", "http://127.0.0.1:5001")


def parse_rows(text) -> list[dict]:
    """Turn a Toolbox tool result into a list of row dicts.

    Toolbox returns the rows as a JSON string. Depending on the version that string is
    empty (no rows), one object, a JSON array, or several objects stuck together.
    """
    if not isinstance(text, str):
        return []
    text = text.strip()
    if not text or text == "null":
        return []
    decoder = json.JSONDecoder()
    rows, i = [], 0
    while i < len(text):
        while i < len(text) and text[i].isspace():
            i += 1
        if i >= len(text):
            break
        value, i = decoder.raw_decode(text, i)
        rows.extend(value if isinstance(value, list) else [value])
    return rows


async def check_login(email: str, password: str) -> dict | None:
    """Return {"email", "full_name", "is_premium_customer"} if the login is right, else None.

    A Toolbox or database error is NOT turned into "wrong password": it is raised, so the
    caller can fail loudly (SPEC P-4 spirit).
    """
    async with ToolboxClient(TOOLBOX_URL) as client:
        tools = await client.load_toolset("login")
        tool = next(t for t in tools if t.__name__ == "check-login")
        rows = parse_rows(await tool(email=email, password=password))
    return rows[0] if rows else None
