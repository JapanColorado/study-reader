"""Live check of the shared-context design: prime once, fork two chats.

Usage: pixi run python scripts/check_forking.py
Makes real Claude requests through the local `claude` login.
"""

import asyncio
import tempfile
from pathlib import Path

import pymupdf

from study_reader import claude_client as cc
from study_reader.chat_service import BookRef, ChatService, Job

TITLE = "The Zorblatt Manual"
PAGES = [
    "The Zorblatt Manual. Chapter 1. A zorblatt is a seven-legged device used to calibrate marmalade.",
    "Chapter 2. Every zorblatt must be rinsed in cold tea for exactly 41 seconds before use.",
] + [  # Padding so the book is big enough for prompt caching to be observable.
    f"Appendix entry {i}: marmalade batch {i * 37} was calibrated without incident." for i in range(3, 1500)
]


def write_book(path: Path) -> None:
    doc = pymupdf.open()
    for text in PAGES:
        doc.new_page().insert_textbox(pymupdf.Rect(40, 40, 555, 800), text, fontsize=12)
    doc.save(path)
    doc.close()


async def main() -> None:
    tmp = Path(tempfile.mkdtemp(prefix="study-reader-check-"))
    write_book(tmp / "zorblatt.pdf")
    # Primed the way the app primes a book on its first message.
    service = ChatService(lambda name, *args: print(f"[{name}]", *args), tmp / "sessions")
    book = BookRef(id=1, path=str(tmp / "zorblatt.pdf"), title=TITLE, base_session_id=None, context_mode=None)
    base, mode = await service.ensure_base(Job(chat_id=0, book=book, chat_session_id=None, message=""))
    cwd = service.cwd(book.id)
    print("base session:", base, mode, "exists:", cc.session_exists(base, cwd))
    assert mode == "full", "the book should fit in context"
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
