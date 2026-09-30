"""Live check of the shared-context design: prime once, fork two chats.

Usage: pixi run python scripts/check_forking.py
Makes real Claude requests through the local `claude` login.
"""

import asyncio
import tempfile

from study_reader import claude_client as cc

BOOK = "\n".join(
    [
        "[[page 1]]",
        "The Zorblatt Manual. Chapter 1. A zorblatt is a seven-legged device used to calibrate marmalade.",
        "[[page 2]]",
        "Chapter 2. Every zorblatt must be rinsed in cold tea for exactly 41 seconds before use.",
    ]
    # Padding so the book is big enough for prompt caching to be observable.
    + [f"[[page {i}]]\nAppendix entry {i}: marmalade batch {i * 37} was calibrated without incident." for i in range(3, 1500)]
)


async def main() -> None:
    cwd = tempfile.mkdtemp(prefix="study-reader-check-")
    base = await cc.prime_book("The Zorblatt Manual", BOOK, cwd=cwd)
    print("base session:", base, "exists:", cc.session_exists(base, cwd))
    assert cc.session_exists(base, cwd)
    assert not cc.session_exists("00000000-0000-0000-0000-000000000000", cwd)

    a = await cc.run_turn(
        cc.build_blocks("How many legs does a zorblatt have? One word.", []),
        cwd=cwd, resume=base, fork=True, on_delta=lambda t: print(t, end="", flush=True),
    )
    print("\nchat A:", a.session_id, a.usage)
    b = await cc.run_turn(
        cc.build_blocks("How long is the rinse, and what did I ask you before this?", []),
        cwd=cwd, resume=base, fork=True,
    )
    print("chat B:", b.session_id, b.text, b.usage)
    a2 = await cc.run_turn(cc.build_blocks("What did I just ask you?", []), cwd=cwd, resume=a.session_id)
    print("chat A follow-up:", a2.session_id, a2.text)

    assert len({base, a.session_id, b.session_id}) == 3, "forks must get their own sessions"
    assert "seven" in a.text.lower() or "7" in a.text
    assert "41" in b.text
    assert "legs" in a2.text.lower()
    print("OK")


asyncio.run(main())
