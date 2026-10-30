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

TITLE_PROMPT = (
    "You name conversations for a list in a study app. Given a reader's first question about "
    "a book (and the passage it is about) plus the start of the reply, write a short title, "
    "3 to 7 words, naming the topic. Plain text in sentence case: no quotes, Markdown, or "
    "final period. Reply with the title only."
)
TITLE_MODEL = "haiku"
TITLE_LENGTH = 70

# Sent with every question rather than in SYSTEM_PROMPT: a resumed or forked session
# keeps the system prompt it was created with, so edits there never reach books
# primed (or chats started) before the change.
READER_INSTRUCTIONS = (
    "<reader_instructions>\n"
    "These describe how to reply and what the reader app can display. They apply to "
    "every reply and replace any earlier statement about what you can or can't show.\n\n"
    "Be concise: the reply is read in a narrow sidebar. Answer the question directly, "
    "lead with the key point, and skip preamble, restating the question, and closing "
    "summaries. Use short paragraphs or a few bullets; go into detail only when the "
    "user asks for it.\n\n"
    "When a picture would genuinely help (a plot, a diagram, a simulation, something "
    "to explore with sliders), you can include an interactive visualization: a fenced "
    "code block whose info string is `visualization` followed by a short title, e.g. "
    "```visualization Damped oscillator\\n...\\n```. Its content is an HTML fragment "
    "(elements, <svg>, <canvas>, <style>, <script>) that the reader renders inline in a "
    "sandboxed frame, with a button to open it in a larger window. Rules:\n"
    "- Put the visualization first: start the reply with the fence (at most one short "
    "lead-in sentence before it), then the explanation after it. Don't bury it below "
    "the text.\n"
    "- It starts in a narrow sidebar (about 380-480px wide), so size to the container "
    "width (e.g. SVG viewBox with width:100%) and redraw on resize; never use vh units "
    "or fixed full-page heights. Keep it under ~600px tall.\n"
    "- Keep the controls (sliders, inputs, buttons, with their labels and value readouts) "
    "together in their own element, apart from the plot: the larger window pins them in "
    "view while the reader zooms the plot.\n"
    "- No network access: no external scripts, fonts, images or fetch. Plain JS, SVG "
    "and canvas always work; `d3` (v7) and `Chart` (Chart.js v4) are available as "
    "globals when your code uses them. No alert/prompt/confirm, popups or forms.\n"
    "- Match the reader's theme: CSS variables --fg, --muted, --border, --surface, "
    "--accent and --series-1 ... --series-6 (categorical colors), or from JS "
    "`vizTheme.colors` (keys fg, muted, border, surface, accent) and `vizTheme.series` "
    "(array of colors), plus `vizTheme.dark`.\n"
    "- Label axes and units and keep the code self-contained and correct. KaTeX is not "
    "available inside the frame, so put math in the prose after it, which should be a "
    "brief explanation of what to look at, not a repeat of what the visualization shows.\n"
    "Only add a visualization when it adds real understanding, not for every answer, "
    "but always make one when asked to.\n\n"
    "When the user asks to be quizzed or tested, reply with an interactive quiz: a fenced "
    "code block whose info string is `quiz` followed by a short title, e.g. "
    "```quiz Lagrangian mechanics\\n{...}\\n```, holding one JSON object "
    "{\"questions\": [...]}. The reader shows the questions one by one, checks the "
    "answers, keeps score and offers a retake. Each question is an object with:\n"
    "- \"type\": \"choice\", \"truefalse\", \"numeric\", \"free\" or \"flashcard\";\n"
    "- \"question\": Markdown, with $LaTeX$ for math;\n"
    "- \"page\": the book page it draws on (the [[page N]] number), when there is one;\n"
    "- \"section\": only when the page alone would file it under the wrong section, as with "
    "exercises or review problems at a chapter's end: the number of the book section it "
    "practices as the book numbers it (e.g. \"6.4\"), else its title;\n"
    "- \"explanation\": shown after answering: briefly why the right answer is right;\n"
    "- \"tags\": 1-3 short tags naming the concepts it tests, lowercase with underscores for "
    "spaces and :: for nesting (e.g. [\"entropy\", \"thermodynamics::second_law\"]). Reuse the "
    "user's tags in <existing_tags> when they fit, rather than near-duplicates; don't tag the "
    "book, chapter or section (that is added automatically);\n"
    "- \"answer\", by type: for \"choice\" the 0-based index into \"choices\" (2-5 "
    "strings); for \"truefalse\" true or false; for \"numeric\" a number or an expression "
    "like \"3/8\" or \"sqrt(2)/2\", plus optional \"tolerance\" (absolute; default 1% of the "
    "answer) and \"unit\" (shown beside the input); for \"free\" a model answer, plus "
    "\"rubric\" (the key points a full answer must contain); for \"flashcard\" the "
    "back of the card (Markdown), with \"question\" its front;\n"
    "- \"varies\", for \"numeric\" and \"free\": true if it poses a calculation or worked "
    "problem whose given values could be changed to make a fresh version for later review, "
    "else false.\nYou grade free "
    "responses in a later turn; the user reveals a flashcard's back and rates their own recall, "
    "as they do for free responses when they come back in review.\n"
    "Quiz rules: 5 questions unless asked otherwise, on what the user asked about. Ask "
    "about what a reader would most want to remember and be able to use months later: the "
    "central ideas, the intuition behind them, why a result holds or a step works, how "
    "concepts relate, and when and how to apply a method. Skip incidental details (specific "
    "numbers, names, dates, the setup of a particular example or application) unless they "
    "are the point, and don't ask to recompute the book's worked examples or solved "
    "problems; to practice a method, pose a short fresh case that needs the idea rather "
    "than arithmetic. Mix types to suit the material: numeric only for a single-number "
    "answer, choice to tell apart ideas that are easily confused, truefalse for a crisp "
    "claim people often get wrong, flashcard for a definition, result, step or short "
    "explanation worth recalling (keep its back short: one idea, or a few key points). Use "
    "free response only as a last resort, when no other type could fit that specific "
    "question (e.g. a derivation or argument the user must produce in full that no "
    "number, choice or short flashcard back captures): it is the slowest to review. Make wrong "
    "choices plausible and vary where the right one sits. It is JSON, so escape quotes and "
    "double every LaTeX backslash (\"$\\\\frac{a}{b}$\"). Put at most one short sentence "
    "before the fence and nothing after it; never give answers outside the JSON. Later "
    "messages may include the user's answers in <quiz_answers>.\n"
    "Every quiz question also goes into the user's spaced-repetition deck and comes back "
    "for review on its own, weeks later and outside this chat. So each question must stand "
    "alone: name the concept, book section or setup it is about instead of saying \"the "
    "passage\", \"above\" or \"this\", and don't rely on other questions.\n"
    "To revise an earlier quiz of this chat (when asked to change, fix, add to or drop "
    "questions from it), don't start over: write a new quiz fence with the whole revised quiz, "
    "adding \"revises\": the earlier quiz's title at the top level of the JSON, and on each "
    "question carried over from it (changed or not) \"revises\": its 1-based number in the "
    "earlier quiz. Questions without it are new; earlier questions left out are dropped. The "
    "questions carried over keep their review history, so only carry one over when it still "
    "tests the same idea.\n"
    "When the user asks for flashcards (or cards), reply with the same quiz fence, mostly of "
    "\"flashcard\" questions: 5-10 unless asked otherwise, each testing one key definition, "
    "result, step or idea (by the same standard as quizzes: what's worth remembering, not "
    "incidental detail), with a specific front (not \"Explain X\") and a short back.\n"
    "</reader_instructions>"
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
    # Last transcript entry of the reply: resuming here (resume_at) drops anything after it.
    message_uuid: str | None = None


class ClaudeError(RuntimeError):
    pass


def image_block(path: Path, media_type: str = "image/png") -> dict:
    """An image file as a Messages-API content block."""
    data = base64.standard_b64encode(Path(path).read_bytes()).decode()
    return {"type": "image", "source": {"type": "base64", "media_type": media_type, "data": data}}


def build_blocks(question: str, snippets: list[Snippet], extra_context: str = "") -> list[dict]:
    """Turn a question plus its snippets into Messages-API content blocks."""
    blocks: list[dict] = [{"type": "text", "text": READER_INSTRUCTIONS}]
    if extra_context:
        blocks.append({"type": "text", "text": extra_context})
    for s in snippets:
        if s.kind == "image":
            blocks.append({"type": "text", "text": f"Screenshot from page {s.page + 1}:"})
            blocks.append(image_block(s.image_path))
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


def _options(
    cwd: str | Path, model: str | None, resume: str | None, fork: bool, resume_at: str | None = None,
    system_prompt: str = SYSTEM_PROMPT,
) -> ClaudeAgentOptions:
    _prefer_subscription_login()
    Path(cwd).mkdir(parents=True, exist_ok=True)
    return ClaudeAgentOptions(
        system_prompt=system_prompt,
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
        resume_session_at=resume_at,
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
    resume_at: str | None = None,
    on_delta: Callable[[str], None] | None = None,
    system_prompt: str = SYSTEM_PROMPT,
) -> TurnResult:
    """Send one user message and return the reply, streaming text through on_delta.

    resume_at: a TurnResult.message_uuid of the resumed session; only the conversation
    up to it is loaded (use with fork, so the original session stays intact)."""

    async def prompt():
        yield {
            "type": "user",
            "message": {"role": "user", "content": blocks},
            "parent_tool_use_id": None,
        }

    streamed: list[str] = []
    final_text: list[str] = []
    result: ResultMessage | None = None
    last_uuid: str | None = None
    try:
        async for msg in query(prompt=prompt(), options=_options(cwd, model, resume, fork, resume_at, system_prompt)):
            if isinstance(msg, StreamEvent):
                ev = msg.event
                if ev.get("type") == "content_block_delta" and ev.get("delta", {}).get("type") == "text_delta":
                    piece = ev["delta"].get("text", "")
                    streamed.append(piece)
                    if on_delta and piece:
                        on_delta(piece)
            elif isinstance(msg, AssistantMessage):
                final_text.extend(b.text for b in msg.content if isinstance(b, TextBlock))
                last_uuid = msg.uuid or last_uuid
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
    return TurnResult(session_id=result.session_id, text=text, usage=result.usage or {}, message_uuid=last_uuid)


async def prime_book(title: str, book_text: str, *, cwd: str | Path, model: str | None = None) -> str:
    """Create the shared base session holding the whole book; returns its session id."""
    res = await run_turn(priming_blocks(title, book_text), cwd=cwd, model=model)
    return res.session_id


def clean_title(text: str) -> str:
    """The first line of a title reply, without quotes, Markdown, or a final period."""
    line = next((l for l in text.strip().splitlines() if l.strip()), "")
    line = " ".join(line.strip().lstrip("#").replace("**", "").replace("`", "").split())
    if line.lower().startswith("title:"):
        line = line[6:].strip()
    line = line.strip("\"'“”‘’*_ ").rstrip(".")
    return line[:TITLE_LENGTH].strip()


async def chat_title(question: str, passage: str, reply: str, *, cwd: str | Path) -> str:
    """A short title for a chat from its first exchange, written by a small model
    in a fresh session (no book context needed). Empty if nothing usable came back."""
    parts = []
    if passage:
        parts.append(f"<passage>\n{passage[:600]}\n</passage>")
    parts.append(f"<question>\n{question[:600]}\n</question>")
    parts.append(f"<reply>\n{reply[:1500]}\n</reply>")
    res = await run_turn(
        [{"type": "text", "text": "\n".join(parts)}], cwd=cwd, model=TITLE_MODEL, system_prompt=TITLE_PROMPT,
    )
    return clean_title(res.text)
