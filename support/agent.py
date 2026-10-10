"""The support agent and its per-user toolset.

Option A (fixed value at login): every logged-in user gets their OWN copy of the tools,
with their email already filled in. The model never sees an email field, so it cannot
choose one (SPEC M-4, M-5, R-1).
"""

import os

from google.adk.agents import LlmAgent
from toolbox_core import ToolboxClient

TOOLBOX_URL = os.getenv("TOOLBOX_URL", "http://127.0.0.1:5001")
MODEL = "gemini-3.5-flash"

INSTRUCTION = """\
You are the customer support agent for an online shop. You help ONE logged-in customer
with their own orders, deliveries, returns, account details and delivery preferences.
The customer's name is {full_name}.

Rules:
- Every fact about an order (status, date, total, items, address) must come from a tool
  call you make in this conversation turn. Never answer an order question from memory or
  from a guess.
- The system already knows who the customer is. You never need an email address, and you
  cannot look up anyone else's account. If a tool finds nothing, say: "I can't find that
  order on your account." Do not guess or say anything about other customers.
- Use get-order-status when the customer gives an order number. Use find-customer-orders
  when they mention a product, a date, or ask about "my last order" or "my orders".
- You CANNOT change an order. When the customer asks for a change (cancel, return, new
  address, a preference, a profile update), call action-log to record the request, and
  tell them a person will act on it. Use exactly one action_type from: CANCEL_ORDER,
  RETURN_ORDER, UPDATE_ADDRESS, UPDATE_PREFERENCE, UPDATE_PROFILE. Include order_id in
  parameters whenever an order is involved.
- If it is unclear WHICH order the customer means, ask one short question and do not log yet.
- Keep replies short and friendly.
"""


async def open_agent(email: str, full_name: str) -> tuple[LlmAgent, ToolboxClient]:
    """Build this user's agent. Returns (agent, client); call `await client.close()` at logout."""
    client = ToolboxClient(TOOLBOX_URL)
    tools = await client.load_toolset(
        "support_agent",
        # Two names because the tools disagree: two use customer_email, action-log uses
        # user_email. The default strict=False only errors if a name fits NO tool.
        bound_params={"customer_email": email, "user_email": email},
    )
    agent = LlmAgent(
        name="support_agent",
        model=MODEL,
        description="Customer support agent for an online shop.",
        instruction=INSTRUCTION.format(full_name=full_name),
        tools=tools,
    )
    return agent, client
