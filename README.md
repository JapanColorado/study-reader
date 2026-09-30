# Study Reader

A Linux PDF/EPUB reader where any selected passage or screenshot region can be sent to a Claude chat. Each question gets its own chat, and every chat shares the whole book as context.

## Run

```sh
pixi run app                  # library screen
pixi run app path/to/book.pdf # open a book directly
pixi run install-desktop      # optional: add a launcher entry
pixi run test
```

Requires the `claude` CLI to be logged in (`claude` → `/login`). The app talks to Claude through the Claude Agent SDK using that login.

## Use

| Action | How |
| --- | --- |
| Ask about a passage | Drag over text → **Ask in new chat** (`Ctrl+Return`) |
| Add a passage to the open chat | **Add to current chat** (`Ctrl+Shift+Return`) |
| Ask about a figure or equation | **Screenshot region** (`Ctrl+Shift+S`), drag a rectangle |
| Send | `Enter` (`Shift+Enter` for a new line). Sending with no text asks "Explain this." |
| Jump back to a snippet's source | Click the quote or thumbnail in the chat |
| Zoom | `Ctrl+=` / `Ctrl+-` or `Ctrl`+wheel |
| Library / open file / new chat | `Ctrl+L` / `Ctrl+O` / `Ctrl+N` |
| Show or hide contents / chats | `F9` / `F10` |
| Revisit a passage you asked about | Click its highlight → pick one of its chats, or start a new one |
| Rename a chat | Double-click it (or `F2`, or right-click → **Rename…**) |
| Model | The chat header's dropdown sets the open chat's model. **Settings** (toolbar, right) → **Default model for new chats** sets what new chats start with |
| Theme | **Settings** → **Theme**: match the system, light, or dark. **Dark pages** recolors the book itself in dark mode |

Every passage and screenshot region you ask about stays highlighted in the book. Right-click a chat or a library entry to delete it; deleting a chat removes its highlights.

## How the shared context works

The first question for a book sends its full text to Claude once, in a "base" session. Every new chat forks that session, so all chats start from the same prefix (which the prompt cache serves cheaply) and stay independent of each other. Follow-ups resume the chat's own session.

- Books too large for the context window fall back to sending the pages around the snippet. The status line under the transcript says which mode a book is in.
- If Claude Code has pruned a session, the app re-shares the book or replays the saved transcript automatically.
- Context is extracted text, so figures and scanned pages only reach Claude through screenshots.

## Notes

- Data lives in `~/.local/share/study-reader/` (database, screenshots, session working directories). Set `STUDY_READER_DATA` to move it.
- An exported `ANTHROPIC_API_KEY` is ignored so requests use the Claude login rather than per-token API billing. Set `STUDY_READER_USE_API_KEY=1` to use the key instead.
- Chats run with no tools and no MCP servers, including claude.ai connectors.
- The chat view uses software rendering because GPU compositing in QtWebEngine leaves it blank on some Wayland compositors (seen on COSMIC). Set `STUDY_READER_WEB_GPU=1` to re-enable GPU rendering.
- EPUBs are rendered by MuPDF as fixed pages, with limited CSS support.
