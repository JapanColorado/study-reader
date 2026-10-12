# Study Reader

A Linux PDF/EPUB/web-page reader where any selected passage or screenshot region can be sent to a Claude chat. Each question gets its own chat, and every chat shares the whole book as context.

## Run

```sh
pixi run app                  # library screen
pixi run app path/to/book.pdf # open a book directly
pixi run app https://www.lesswrong.com/posts/…  # open a web page
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
| Read a footnote or cross-reference | Footnote markers are marked with a violet pill, linked references with a fainter one. Hover one to preview the target; click to jump there. **← Back** (bottom left), `Alt+Left` or the mouse's back button returns. Works with the PDF's own links and with plain superscript footnote numbers |
| Jump back to a snippet's source | Click the quote or thumbnail in the chat |
| Zoom | `Ctrl+=` / `Ctrl+-` or `Ctrl`+wheel |
| Crop the page margins so the text fills the view | **Crop margins** (`Ctrl+Shift+0`), remembered per book |
| Library / open file / new chat | `Ctrl+L` / `Ctrl+O` / `Ctrl+N` |
| Mark a book finished | **Finished** in the toolbar, or right-click it in the library. Finished books move to a collapsible **Finished** section; toggle it off or choose **Move back to reading** to undo |
| Open a web page | **Open web page…** (`Ctrl+Shift+O`; a copied link is filled in). Opening the same address again opens the saved copy; right-click it in the library → **Fetch a fresh copy** to update it in place, keeping its chats and notes |
| Show or hide contents / chats | `F9` / `F10` |
| Calculator (takes the chat panel's place) | `F11`. Desmos-style math input: `/` makes a stacked fraction, `^` an exponent, `sqrt` a root, and → steps out of them. Type e.g. `v = 3.2`, `12v / ans`, `sin(30)` in **DEG**. Arithmetic is exact, and the button beside a result switches between decimal and fraction (`0.1 + 0.2` = 3/10). ↑ recalls earlier input; history is kept per book |
| Quiz yourself | **Quiz me** (`Ctrl+Shift+Q`, or in the selection popup): on the selection, else on the section you're reading. The arrow beside it lists the chapter, section and subsection around the page (and the page alone); picking one quizzes on it and makes that depth the button's default for the book. Documents of 50 pages or fewer also offer the whole document. Or **Quiz me** under any of Claude's replies. Opens a quiz chat with multiple-choice, true/false, numeric (typed like the calculator), free-response and flashcard questions (recall the answer, **Show answer**, then rate yourself). Answers are checked as you go, free responses are graded by Claude, and the score and answers are kept; **Retake** starts over (your first answers still count for review). Asking "quiz me on…" or "flashcards on…" in any chat works too, and follow-up questions see your answers |
| Review with spaced repetition | Every quiz question joins your review cards, scheduled with [FSRS](https://github.com/open-spaced-repetition/py-fsrs), in a deck named after the book; your first answer in the chat counts as its first review. **Review** (`F8`, in the chat panel's place) shows what's due in the open book and then one card at a time: answer it (checked as in the quiz; free responses by **Grade with Claude** or **Show answer**), then rate it **Again / Hard / Good / Easy** (`1`–`4`, `Space` for the suggested rating). Cards link back to their page and chat; **Edit** opens a card in the browser, **Suspend** leaves one out, and a quiz's **In review** toggle leaves out all its questions. **Review** on the library screen lists every deck with its due and new cards: pick one (with its subdecks) or all. At most 20 new cards a day are introduced across all decks (Settings → **Review settings**) |
| Browse and edit cards | **Browse cards** (`Ctrl+Shift+B`, or on the library screen) opens the card browser: a sidebar of decks, tags, books, flags and saved searches (click to search; `Ctrl`+click narrows, `Shift`+click widens), a sortable table (right-click the header for columns: due, interval, stability, difficulty, retrievability, reviews, lapses…), and an editor with a live preview for the current card. Search like Anki: words, `"a phrase"`, `deck:Physics` (with subdecks), `tag:exam`, `is:due` / `new` / `learn` / `review` / `suspended`, `flag:1`, `note:cloze`, `type:free`, `book:Perloff`, `added:7`, `rated:1:1`, `prop:ivl>30`, `prop:r<0.8`, `re:…`, `-` to negate, `OR` and parentheses. On a selection: change deck (`Ctrl+D`), add or remove tags, suspend (`Ctrl+J`), flag (`Ctrl+1`–`7`), forget, set due date, find and replace, card info with the full review history (`Ctrl+Shift+I`), delete. Right-click a deck to review it, rename or move it (`Parent::Child`), or delete it |
| Make cards | **Add** in the browser (`Ctrl+E`): Basic, Basic and reversed, Cloze (select text and press `Ctrl+Shift+C` for `{{c1::…}}`; a hint goes after `::`), or a quiz question (multiple choice, true/false, numeric, free response graded by Claude, flashcard). Fields take Markdown with `$math$` and inline HTML; paste or drop images into them |
| Improve cards with Claude | Select cards in the browser → **Claude → Convert with Claude…** (`Ctrl+Shift+K`). Claude (Sonnet by default) proposes a better question type for each where one fits, e.g. turning a recall card into a free response it grades, or keeps it. **Conversions** shows each proposal next to the card as it is; accept or reject one at a time or all at once. An accepted card keeps its deck, tags, schedule and history, and **Claude → Undo conversion** brings back the old version |
| Migrate from Anki | Settings → **Migrate from Anki…** (or the browser's **File** menu): pick a profile and tick the decks to bring over. Notes come over as basic, reversed, cloze or image-occlusion cards with their tags, images, schedule (FSRS memory state and due date), suspended state, flags and full review history, plus Anki's FSRS parameters and desired retention. Anki is only read, from a copy, so it can stay open; running it again skips cards already brought over |
| Statistics and FSRS settings | **Statistics** (Settings, the browser, or the review screen): today's reviews, card counts, a review calendar, reviews and forecast charts, true retention, and interval, stability, difficulty and retrievability distributions, for all decks, one deck or one book. Settings → **Review settings**: new cards per day, desired retention, **Optimize FSRS parameters** (fits them to your review history; needs a few hundred reviews), or back to the defaults |
| Revisit a passage you asked about | Click its highlight → pick one of its chats, or start a new one |
| Annotate a passage | Drag over text → **Note** (`Ctrl+Shift+N`). The passage is highlighted in blue and the **Notes** panel (`F12`, in the chat panel's place) opens on its note. Notes take Markdown and `$math$`; `Ctrl+Enter` saves. Hover a blue highlight to read its note, or click it to open the note, ask about it, or delete it. The panel lists the book's notes in page order, with a search box, and **Export** (↓) saves them as a Markdown file |
| Redo a reply | **Regenerate** under the chat's last reply (**Retry** after an error) |
| Edit a question | Hover it → **Edit**: it loads into the box below, where you can change the text or its passages; sending replaces it and every reply after it. `Esc` cancels |
| Rename a chat | Double-click it (or `F2`, or right-click → **Rename…**) |
| Model | The chat header's dropdown sets the open chat's model. **Settings** (toolbar, right) → **Default model for new chats** sets what new chats start with |
| Theme | **Settings** → **Theme**: match the system, light, or dark. **Dark pages** recolors the book itself in dark mode |

Claude can answer with interactive visualizations (plots, diagrams, simulations with sliders). They render inline in the chat. **Open ↗** on a visualization's header shows it in a larger window. Asking for one ("visualize this", "plot it so I can vary k") helps.

Every passage and screenshot region you ask about stays highlighted in the book. Right-click a chat or a library entry to delete it; deleting a chat removes its highlights.

## Web pages

A web page is saved as a PDF snapshot (in `web/` under the data directory) and then reads like any book, so its highlights and page numbers stay put. Images are fetched once, when the snapshot is made.

- **LessWrong**, the **Alignment Forum** and the **EA Forum** (and GreaterWrong links) are read through the sites' API:
  - a post comes with its 30 best comments, threaded, each with the comments it replies to;
  - a sequence (`/s/…`) becomes one book with a chapter per post;
  - a wiki page (`/w/…`, `/tag/…`) includes every tab, each starting a new page.
- Other sites go through readability-style article extraction, which drops menus and sidebars. A link straight to a PDF or EPUB opens that file.
- Math is re-typeset with MathJax, and each formula's TeX source is kept in the page's text, so selections and Claude see `$\frac{a}{b}$` rather than scattered glyphs.

## How the shared context works

The first question for a book sends its full text to Claude once, in a "base" session. Every new chat forks that session, so all chats start from the same prefix (which the prompt cache serves cheaply) and stay independent of each other. Follow-ups resume the chat's own session. Regenerating or editing forks the chat's session at the reply just before the changed question, so Claude doesn't see the replaced turns.

- Books too large for the context window fall back to sending the pages around the snippet. The status line under the transcript says which mode a book is in.
- If Claude Code has pruned a session, the app re-shares the book or replays the saved transcript automatically.
- Context is extracted text, so figures and scanned pages only reach Claude through screenshots.

## Notes

- Data lives in `~/.local/share/study-reader/` (database, screenshots, card images in `media/`, session working directories). Set `STUDY_READER_DATA` to move it.
- An exported `ANTHROPIC_API_KEY` is ignored so requests use the Claude login rather than per-token API billing. Set `STUDY_READER_USE_API_KEY=1` to use the key instead.
- Chats run with no tools and no MCP servers, including claude.ai connectors.
- Visualizations run in a sandboxed frame with no network access. Bundled d3 v7 and Chart.js v4 (in `assets/viz/`) are inlined when a visualization uses them.
- The chat view uses software rendering because GPU compositing in QtWebEngine leaves it blank on some Wayland compositors (seen on COSMIC). Set `STUDY_READER_WEB_GPU=1` to re-enable GPU rendering.
- EPUBs are rendered by MuPDF as fixed pages, with limited CSS support.
