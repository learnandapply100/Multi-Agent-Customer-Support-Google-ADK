"""Throwaway CLI for Stage 3: log in, then chat. Stage 4 replaces the loop with the pipeline.

Run from the project folder:  python -m support.cli
"""

import asyncio
import getpass
import sys

from dotenv import load_dotenv
from google.adk.runners import Runner
from google.adk.sessions import InMemorySessionService
from google.genai import types

load_dotenv()  # reads GOOGLE_API_KEY, DB passwords etc. from .env

from support.agent import open_agent  # noqa: E402  (after load_dotenv on purpose)
from support.login import check_login  # noqa: E402

APP_NAME = "customer_support"


async def ask(runner: Runner, user_id: str, session_id: str, text: str) -> str:
    message = types.Content(role="user", parts=[types.Part(text=text)])
    reply = ""
    async for event in runner.run_async(
        user_id=user_id, session_id=session_id, new_message=message
    ):
        if event.is_final_response() and event.content and event.content.parts:
            reply = "".join(p.text or "" for p in event.content.parts)
    return reply or "(no reply)"


async def main() -> None:
    email = input("Email: ").strip()
    password = getpass.getpass("Password: ")  # hidden input (C-1)

    user = await check_login(email, password)
    if user is None:
        print("Invalid email or password.")
        return
    print(f"Welcome, {user['full_name']}! Type quit to leave.")

    # One toolset per logged-in user, with their email bound (Option A).
    agent, toolbox = await open_agent(user["email"], user["full_name"])
    sessions = InMemorySessionService()
    runner = Runner(app_name=APP_NAME, agent=agent, session_service=sessions)
    session = await sessions.create_session(app_name=APP_NAME, user_id=user["email"])

    try:
        while True:
            try:
                line = input("You: ").strip()
            except EOFError:
                break
            if line.lower() in {"quit", "exit", "q"}:
                break
            if not line:
                continue
            print("Agent:", await ask(runner, user["email"], session.id, line))
    finally:
        await toolbox.close()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        sys.exit(0)
