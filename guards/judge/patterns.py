"""The Judge's deterministic pattern tool (SPEC J-2).

Each pattern matches the SHAPE of an attack, never a single word. "drop" is an ordinary
word; "DROP TABLE" is not. Stage 6 decision: one match here is enough to block, and the
model is not asked. So every pattern must be narrow enough to trust on its own.
"""

import re

_TABLES = r"(users|customer_orders|actions_log|information_schema|pg_\w+)"

_RAW = [
    # --- SQL injection ---
    ("sql: drop table", r"\bDROP\s+(TABLE|DATABASE|SCHEMA)\b"),
    ("sql: union select", r"\bUNION\s+(ALL\s+)?SELECT\b"),
    ("sql: select from table", rf"\bSELECT\s+\*\s+FROM\b|\bSELECT\b.{{0,60}}\bFROM\s+{_TABLES}\b"),
    ("sql: delete from", rf";\s*DELETE\s+FROM\b|\bDELETE\s+FROM\s+{_TABLES}\b"),
    ("sql: update set", r"\bUPDATE\s+\w+\s+SET\s+\w+\s*="),
    ("sql: insert into", r";\s*INSERT\s+INTO\b"),
    ("sql: always-true condition", r"\bOR\s+'?(\w+)'?\s*=\s*'?\1\b"),   # OR 1=1, OR 'a'='a
    ("sql: quote then comment", r"'\s*--|;\s*--"),                       # admin'--
    # --- markup ---
    ("markup: html tag", r"<\s*/?\s*(script|iframe|img|svg|object|embed|style|link|body)\b"),
    ("markup: javascript url", r"\bjavascript\s*:"),
    ("markup: event handler", r"\bon(error|load|click|mouseover|focus)\s*="),
    # --- template injection ---
    ("template: expression", r"\{\{.*?\}\}|\$\{.*?\}|<%.*?%>"),
    # --- shell and path traversal ---
    ("shell: command substitution", r"\$\(.+?\)"),
    ("shell: rm -rf", r"\brm\s+-[rf]{1,2}\b"),
    ("shell: chained command", r"(;|\||&&)\s*(cat|ls|rm|wget|curl|nc|bash|sh|chmod)\s+[-/~.]"),
    ("path: traversal", r"\.\./|\.\.\\"),
    ("path: system file", r"/etc/(passwd|shadow)\b"),
    # --- prompt injection and prompt extraction: fixed phrases only ---
    ("prompt: ignore instructions",
     r"\b(ignore|forget|disregard)\s+(all\s+)?(your\s+(previous\s+)?|(the\s+)?(previous|prior|above|earlier)\s+)"
     r"(instructions|rules|prompts?)\b"),
    ("prompt: system prompt", r"\bsystem\s+prompt\b"),
    ("prompt: reveal instructions", r"\b(print|show|reveal|repeat|quote)\s+(me\s+)?your\s+(instructions|rules|prompt)\b"),
    ("prompt: repeat everything above", r"\brepeat\s+everything\s+above\b"),
    ("prompt: fake role line", r"^\s*(system|assistant|developer)\s*:"),
    ("prompt: new instructions", r"\bnew\s+instructions\s*:"),
    ("prompt: no-rules persona", r"\bAI\s+without\s+(any\s+)?(rules|restrictions|limits)\b"),
    # --- internal names a customer has no reason to type ---
    ("internal: table or tool name", r"\b(customer_orders|actions_log|update-order-status)\b|\busers\s+table\b"),
    ("internal: other users' passwords", r"\bpasswords?\s+(of|for)\s+(all|every|other)\b"),
]

PATTERNS = [(name, re.compile(regex, re.IGNORECASE)) for name, regex in _RAW]


def find_attack_patterns(text: str) -> list[str]:
    """Return the names of every pattern that matches. An empty list means no match."""
    return [name for name, pattern in PATTERNS if pattern.search(text)]
