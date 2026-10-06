# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

Study Reader is a PySide6 desktop PDF/EPUB/web-page reader (Linux only) where selected passages or screenshot regions are sent to per-snippet Claude chats that all share the whole book as context. See README.md for user-facing behavior and keyboard shortcuts.

## Commands

Pixi manages the environment; `PYTHONPATH` is set to `src/` by pixi activation, so run everything through `pixi run`.

```sh
pixi run app [book.pdf | URL]      # launch (library screen, or open a book or web page)
pixi run test                      # pytest -q tests
pixi run pytest -q tests/test_core.py::test_first_chat_primes_then_forks   # single test
pixi run check-claude              # live priming+forking check; makes real Claude requests
```

Tests never hit Claude: `FakeClaude` in `tests/test_core.py` monkeypatches `claude_client.run_turn` and `claude_client.session_exists`. Test PDFs/EPUBs are generated on the fly in `tests/conftest.py`.

## Architecture

**Shared book context via session forking** (the core idea, spread across `claude_client.py` and `chat_service.py`):
- Claude is reached through the Claude Agent SDK (`query()`), which drives the local `claude` CLI and its login. Each book gets its own session working directory (`<data>/sessions/book-<id>`), since SDK sessions are scoped by `cwd`.
- The first question for a book "primes" a base session containing the full extracted text (`[[page N]]` markers). Each new chat resumes that base session with `fork_session=True`; follow-ups resume the chat's own session without forking.
- `ChatService.ask` decides the route: chat session exists → resume it; otherwise `ensure_base` (re-priming if the base session was pruned) then fork, replaying saved DB history as a transcript if the chat's own session was lost.
- Regenerate / edit truncate the chat in the DB, then branch: each assistant message stores its `session_id` and `session_uuid` (last transcript entry), and the job resumes that session with `resume_at` (SDK `resume_session_at`) plus `fork=True`. With no usable branch point the job falls back to fork-from-base plus history replay, and `ask` retries that way if the branch point fails to load.
- Books over `MAX_BOOK_TOKENS` (estimated at 3 chars/token), or whose priming fails with a "too long" error, switch to `context_mode="partial"`: no base session, and each question carries a `text_window` of pages around the current page.
- `_options()` deliberately strips all tools, MCP servers (including claude.ai connectors via `ENABLE_CLAUDEAI_MCP_SERVERS=false`), and setting sources, and uses `max_turns=1`. `_prefer_subscription_login()` removes `ANTHROPIC_API_KEY`/`ANTHROPIC_AUTH_TOKEN` from the environment unless `STUDY_READER_USE_API_KEY=1`.
- A resumed or forked session keeps the system prompt it was created with; the `system_prompt` passed on later calls is ignored. So `SYSTEM_PROMPT` edits only reach newly primed books. Put behavior that must apply everywhere in `READER_INSTRUCTIONS`, which `build_blocks` sends with every question.

**Threading:** `ChatWorker` runs `ChatService` on a dedicated asyncio loop in a daemon thread. `ChatService` is Qt-free and reports through an `emit(name, *args)` callback; `ChatWorker._emit` maps names to Qt signals (`status`, `base`, `delta`, `done`, `failed`) that `ChatPanel` connects to. Keep new events in both places. Persisting results (session ids, `context_mode`, messages) happens on the GUI side in `ChatPanel`, not in the service.

**UI modules:**
- `main_window.py` wires everything: library vs. reader, toolbar/settings menu, TOC dock, highlights → chat navigation.
- `reader_view.py` is a custom `QAbstractScrollArea` that renders pages with PyMuPDF, handles word-level text selection, region screenshots, persistent highlights, and the flash-on-jump effect.
- `chat_panel.py` holds the chat list, composer, and per-chat model selector, and builds `Job`s for the worker.
- `calculator.py` has a Qt-free `Calculator` (safe `ast`-based evaluator; never `eval`; ints and decimals stay exact as `Fraction`s, functions give floats) and the `CalculatorPanel` that shares the right-hand `QStackedWidget` slot with `ChatPanel` (F10/F11 are mutually exclusive). The panel's history and input are `assets/calculator.html`: a MathLive math field (bundled in `assets/mathlive/`) talking to `_Bridge` over `QWebChannel`. `calc_latex.py` turns the field's LaTeX into plain expressions (inserting implied multiplication) and results back into LaTeX/fractions. History is per book in the `calculations` table (`expr` evaluated, `latex` as typed); `ans` and variables are restored from stored values, not by re-evaluating.
- `transcript_view.py` is a `QWebEngineView` loading `assets/transcript.html` (markdown-it + KaTeX, bundled offline in `assets/`). Snippet clicks come back as `jump:<snippet_id>` navigations intercepted in `_Page.acceptNavigationRequest`.
- Visualizations: `READER_INSTRUCTIONS` tells Claude to write ```` ```visualization <title> ```` fences of self-contained HTML/JS. `transcript.html` renders each fence in an `<iframe sandbox="allow-scripts" srcdoc>` built by `vizDoc()`. That doc carries a no-network CSP, the theme as CSS variables plus `vizTheme`, and d3/Chart.js inlined from `assets/viz/` when the code references them (sandboxed frames can't load `file://`). Frames `postMessage` their height so they auto-size. During streaming, fences show a placeholder. `viz://open/<n>` opens `VisualizationWindow` with the same doc, fetched via `runJavaScript` as JSON.
- Quizzes: `READER_INSTRUCTIONS` also defines ```` ```quiz <title> ```` fences of JSON. `quiz.py` (Qt-free) parses them (forgiving undoubled LaTeX backslashes), grades choice/true-false/numeric answers locally (numeric through `Calculator`), and builds the grading prompt and the `<quiz_answers>` context (`Job.context`) sent with later questions. `ChatPanel` replaces each fence with a `#<index>` placeholder plus parsed data that `transcript.html` renders; answers come back over a `QWebChannel` (`_QuizBridge`) and are stored in `quiz_answers`. Free responses are graded by `ChatService.grade` as a `detached` job that always forks (at the quiz reply's `session_uuid`), so the chat's own session never sees it. `assets/mathfield.js` holds the MathLive setup shared with the calculator.
- Notes: `notes.py` has Qt-free search/Markdown export and the `NotesPanel`, the third panel in the right-hand `QStackedWidget` (`MainWindow.right_panels` maps `books.right_panel` names to their toggle actions; at most one is shown). Cards render in `assets/notes.html` and edits come back over `QWebChannel`. Notes live in the `notes` table (passage text + rects, like snippets) and are passed to `ReaderView.set_highlights` alongside snippets; a mark holding notes draws in blue. They are never sent to Claude. `assets/markdown.js` holds the Markdown + KaTeX rendering shared with `transcript.html`.
- `theme.py` has a global `theme()` singleton (light/dark/system plus "dark pages" recoloring via `darken_page`) that emits `changed`, and draws line icons from inline SVG.

**Documents:** `document.py` wraps PyMuPDF for both PDF and EPUB. EPUBs are laid out at a fixed size (`REFLOW_WIDTH/HEIGHT/FONT`) so page numbers stay stable across runs, which matters because snippet rects and page numbers are persisted. Books are identified by content hash (`file_hash`), so a moved file maps back to the same library entry.

**Web pages:** `web_import.py` (Qt-free) turns a URL into an `Article` of `Section`s: LessWrong/Alignment Forum/EA Forum (`FORUMS`) through their GraphQL API (posts + top comments, sequences, wiki pages with their tabs, called "lenses" in the API), anything else through `readability`. `build_html` makes one printable document; math from any source is normalized to `<span class="math" data-tex>` and re-typeset by bundled MathJax SVG (`assets/mathjax/`), with the TeX also printed nearly invisibly in the flow (`.tex-src`) because that is the only math text that extracts cleanly and in reading order. `web_render.WebImporter` fetches in a thread, prints with `QWebEnginePage.printToPdf` at `REFLOW_WIDTH×HEIGHT`, then `finish_pdf` adds metadata and a TOC by searching for each heading. The snapshot then opens through `open_book`; `books.source_url` maps a URL back to its snapshots. Tests stub `web_import.http_get`.

**Persistence:** `db.py` is plain `sqlite3` (books → chats → messages → snippets, plus a key/value `settings` table). The schema is `CREATE TABLE IF NOT EXISTS`. For new columns on existing tables, add an `ALTER TABLE` step in `Database._migrate` (see the `chats.model` migration and its test). Snippet `rects` are JSON in PDF page points. Data lives in `$STUDY_READER_DATA` or `$XDG_DATA_HOME/study-reader`.

**QtWebEngine:** `__main__.configure_webengine` forces `--disable-gpu` (blank transcript on some Wayland compositors) unless `STUDY_READER_WEB_GPU=1`.
