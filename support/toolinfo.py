"""What each tool's SQL looks like, read from mcp_toolbox/tools.yaml (SPEC section 7.1).

The `tool_call` event carries the SQL statement and the names of its parameters, so a
screen can show the query with the values filled in (W-3).

This file only READS the SQL text to display it. It never runs any SQL: the Toolbox is still
the only thing that talks to the database (M-2). Reading it from tools.yaml, instead of
typing it again here, means the SQL on screen is always the SQL that really ran.
"""

from pathlib import Path

import yaml

TOOLS_YAML = Path(__file__).resolve().parent.parent / "mcp_toolbox" / "tools.yaml"


def _load() -> dict[str, dict]:
    """Return {tool name: {"statement": "...", "params": ["order_id", "customer_email"]}}."""
    # tools.yaml holds several YAML documents separated by '---', so use safe_load_all.
    documents = yaml.safe_load_all(TOOLS_YAML.read_text())
    info = {}
    for doc in documents:
        if isinstance(doc, dict) and doc.get("kind") == "tool":
            info[doc["name"]] = {
                "statement": str(doc["statement"]).strip(),
                # The order in the file is the $1, $2, ... order of the statement.
                "params": [p["name"] for p in doc.get("parameters") or []],
            }
    return info


# Read once, when the program starts. If tools.yaml is missing or broken, the program stops
# here with a clear error instead of showing events with no SQL in them.
_TOOLS = _load()


def tool_info(name: str) -> dict:
    """The statement and parameter names for one tool. Raises if the tool is not in tools.yaml."""
    if name not in _TOOLS:
        raise RuntimeError(f"tool {name!r} is not declared in {TOOLS_YAML.name}, so its SQL cannot be shown")
    return dict(_TOOLS[name])
