"""Login check. Plain code, no model involved (M-2, M-6).

It calls the `check-login` tool from the `login` toolset. The agent never loads that
toolset, so it cannot call this tool.
"""

import os

from toolbox_core import ToolboxClient

from support.toolresult import parse_rows

# Same default as support/agent.py: this project's Toolbox runs on port 5001.
TOOLBOX_URL = os.getenv("TOOLBOX_URL", "http://127.0.0.1:5001")


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
