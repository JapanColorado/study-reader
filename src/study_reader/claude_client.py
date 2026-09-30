"""Claude access through the Claude Agent SDK.

Shared book context works by session forking: one "priming" session per book
holds the full text, and every chat forks from it so they all share the same
(cacheable) prefix while staying independent of each other.
"""

from __future__ import annotations

import base64
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from claude_agent_sdk import (
    AssistantMessage,
    ClaudeAgentOptions,
    ResultMessage,
    StreamEvent,
    TextBlock,
    get_session_info,
    query,
)

SYSTEM_PROMPT = (
    "You are a study companion embedded in a PDF/EPUB reader. The user is reading "
    "a book and asks short, one-off questions about passages or screenshots they "
    "select from it. Answer directly and concisely, grounded in the book where it "
    "is relevant, and mention page numbers when you refer to other parts of it. "
    "Use Markdown, and LaTeX ($...$ or $$...$$) for math. You have no tools."
)

# Rough budget for the book text inside the priming session, leaving room for chats.
MAX_BOOK_TOKENS = 150_000
# Conservative: technical and numeric text tokenizes far denser than prose.
CHARS_PER_TOKEN = 3.0


def estimate_tokens(text: str) -> int:
    return int(len(text) / CHARS_PER_TOKEN)


def fits_in_context(text: str) -> bool:
    return estimate_tokens(text) <= MAX_BOOK_TOKENS


@dataclass
class Snippet:
    """A piece of the book attached to a question."""

    kind: str  # "text" | "image"
    page: int  # 0-based
    text: str = ""
    image_path: str = ""


@dataclass
class TurnResult:
    session_id: str
    text: str
    usage: dict[str, Any] = field(default_factory=dict)


class ClaudeError(RuntimeError):
    pass


def build_blocks(question: str, snippets: list[Snippet], extra_context: str = "") -> list[dict]:
    """Turn a question plus its snippets into Messages-API content blocks."""
    blocks: list[dict] = []
    if extra_context:
        blocks.append({"type": "text", "text": extra_context})
    for s in snippets:
        if s.kind == "image":
            data = base64.standard_b64encode(Path(s.image_path).read_bytes()).decode()
            blocks.append({"type": "text", "text": f"Screenshot from page {s.page + 1}:"})
            blocks.append(
                {"type": "image", "source": {"type": "base64", "media_type": "image/png", "data": data}}
            )
        else:
            blocks.append(
                {"type": "text", "text": f"Selected passage from page {s.page + 1}:\n<passage>\n{s.text}\n</passage>"}
            )
    blocks.append({"type": "text", "text": question})
    return blocks


def priming_blocks(title: str, book_text: str) -> list[dict]:
    return [
        {
            "type": "text",
            "text": (
                f"Here is the full text of the book I am reading, \"{title}\". "
                "Page boundaries are marked as [[page N]].\n\n"
                f"<book>\n{book_text}\n</book>\n\n"
                "I will ask questions about it. Reply with just: Ready."
            ),
        }
    ]


def session_exists(session_id: str | None, cwd: str | Path) -> bool:
    if not session_id:
        return False
    try:
        return get_session_info(session_id, directory=str(cwd)) is not None
    except Exception:
        return False


def _prefer_subscription_login() -> None:
    """An exported API key would silently take precedence over the claude.ai
    login and bill per token; drop it unless the user opts in."""
    if os.environ.get("STUDY_READER_USE_API_KEY") == "1":
        return
    for var in ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN"):
        os.environ.pop(var, None)


def _options(cwd: str | Path, model: str | None, resume: str | None, fork: bool) -> ClaudeAgentOptions:
    _prefer_subscription_login()
    Path(cwd).mkdir(parents=True, exist_ok=True)
    return ClaudeAgentOptions(
        system_prompt=SYSTEM_PROMPT,
        # No tools at all: tools=[] only drops the built-ins, so also shut out
        # MCP servers (including claude.ai connectors like Gmail/Drive, which
        # otherwise load from the account and add ~40k tokens of schemas).
        tools=[],
        mcp_servers={},
        strict_mcp_config=True,
        env={"ENABLE_CLAUDEAI_MCP_SERVERS": "false"},
        permission_mode="dontAsk",
        setting_sources=[],
        cwd=str(cwd),
        model=model or None,
        resume=resume,
        fork_session=fork,
        include_partial_messages=True,
        max_turns=1,
    )


async def run_turn(
    blocks: list[dict],
    *,
    cwd: str | Path,
    model: str | None = None,
    resume: str | None = None,
    fork: bool = False,
    on_delta: Callable[[str], None] | None = None,
) -> TurnResult:
    """Send one user message and return the reply, streaming text through on_delta."""

    async def prompt():
        yield {
            "type": "user",
            "message": {"role": "user", "content": blocks},
            "parent_tool_use_id": None,
        }

    streamed: list[str] = []
    final_text: list[str] = []
    result: ResultMessage | None = None
    try:
        async for msg in query(prompt=prompt(), options=_options(cwd, model, resume, fork)):
            if isinstance(msg, StreamEvent):
                ev = msg.event
                if ev.get("type") == "content_block_delta" and ev.get("delta", {}).get("type") == "text_delta":
                    piece = ev["delta"].get("text", "")
                    streamed.append(piece)
                    if on_delta and piece:
                        on_delta(piece)
            elif isinstance(msg, AssistantMessage):
                final_text.extend(b.text for b in msg.content if isinstance(b, TextBlock))
            elif isinstance(msg, ResultMessage):
                result = msg
    except ClaudeError:
        raise
    except Exception as e:  # CLI missing, not logged in, process failure...
        raise ClaudeError(str(e)) from e

    if result is None:
        raise ClaudeError("Claude returned no result")
    text = "".join(final_text) or "".join(streamed) or (result.result or "")
    if result.is_error:
        raise ClaudeError(text or result.subtype or "Claude reported an error")
    return TurnResult(session_id=result.session_id, text=text, usage=result.usage or {})


async def prime_book(title: str, book_text: str, *, cwd: str | Path, model: str | None = None) -> str:
    """Create the shared base session holding the whole book; returns its session id."""
    res = await run_turn(priming_blocks(title, book_text), cwd=cwd, model=model)
    return res.session_id
