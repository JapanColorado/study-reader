"""The Review page's chat panel, throwaway chats, stopping a reply, and panels sharing a hub."""

import asyncio
import json
import sqlite3

from study_reader import chat_service as cs
from study_reader import claude_client as cc
from study_reader import quiz
from study_reader.db import Database

QUIZ = '''```quiz Zorblatts
{"questions": [{"type": "flashcard", "question": "Rinse time?", "answer": "41 seconds"}]}
```'''


def card_of(db, note_id):
    return db.conn.execute("SELECT id FROM cards WHERE note_id=?", (note_id,)).fetchone()[0]


def review_panel(chat_panel):
    """The fixture's worker and database with a second, Review page panel on the same hub."""
    from study_reader.chat_panel import ChatPanel

    panel, worker, db, book_id = chat_panel
    review = ChatPanel(db, worker, hub=panel.hub, review=True)
    return panel, review, worker, db, book_id


def card(db, card_id, revealed=False, chat_id=None):
    """What ReviewPanel.chat_card hands the panel."""
    row = db.card(card_id)
    q = {"type": "flashcard", "question": "Front?", "answer": "Back"}
    return {"id": card_id, "book_id": row["book_id"], "chat_id": chat_id, "title": "Deck", "revealed": revealed,
            "context": quiz.card_context(q, "Deck", {"response": {}} if revealed else None)}


# -- the database ----------------------------------------------------------------------------
def test_old_chats_table_is_rebuilt_keeping_messages(tmp_path):
    path = tmp_path / "old.db"
    Database(path).conn.close()
    conn = sqlite3.connect(path)
    conn.executescript("""
        PRAGMA foreign_keys = OFF;
        DROP TABLE chats;
        CREATE TABLE chats (id INTEGER PRIMARY KEY, book_id INTEGER NOT NULL REFERENCES books(id) ON DELETE CASCADE,
            title TEXT NOT NULL, session_id TEXT, model TEXT NOT NULL DEFAULT '', created_at REAL NOT NULL,
            updated_at REAL NOT NULL);
        INSERT INTO books (id, hash, path, title, page_count, opened_at) VALUES (1, 'h', '/a.pdf', 'A', 1, 0);
        INSERT INTO chats VALUES (1, 1, 'old chat', 's', 'opus', 0, 0);
        INSERT INTO messages (chat_id, role, text, created_at) VALUES (1, 'user', 'hi', 0);
    """)
    conn.commit()
    conn.close()
    db = Database(path)
    assert db.chat(1)["model"] == "opus" and db.chat(1)["card_id"] is None and not db.chat(1)["throwaway"]
    assert [m["text"] for m in db.messages(1)] == ["hi"] and not db.messages(1)[0]["stopped"]
    bookless = db.create_chat(None, "About a card")  # book_id is optional now
    assert [c["id"] for c in db.chats(None)] == [bookless] and [c["id"] for c in db.chats(1)] == [1]
    db.delete_book(1)  # still cascades
    assert db.chat(1) is None and db.messages(1) == []


def test_throwaway_chats_are_never_listed(tmp_path):
    db = Database(tmp_path / "t.db")
    book = db.upsert_book("h", "/a.pdf", "A", 1)["id"]
    kept = db.create_chat(book, "kept")
    gone = db.create_chat(book, "gone", throwaway=True)
    db.add_message(gone, "assistant", "a", session_id="s1")
    db.update_chat(gone, session_id="s2")
    assert [c["id"] for c in db.chats(book)] == [kept]
    assert [c["id"] for c in db.throwaway_chats()] == [gone] and db.chat_sessions(gone) == ["s1", "s2"]


# -- the service -------------------------------------------------------------------------------
def test_bookless_chats_resume_their_session_or_replay(monkeypatch, tmp_path):
    calls = []

    async def run_turn(blocks, *, cwd, model=None, resume=None, fork=False, resume_at=None, on_delta=None, **kw):
        calls.append({"text": "\n".join(b.get("text", "") for b in blocks), "resume": resume, "fork": fork,
                      "cwd": cwd})
        return cc.TurnResult("s-new", "answer", message_uuid="u")

    monkeypatch.setattr(cc, "run_turn", run_turn)
    monkeypatch.setattr(cc, "session_exists", lambda sid, cwd: sid == "mine")
    events = []
    svc = cs.ChatService(lambda *a: events.append(a), tmp_path)
    asyncio.run(svc.ask(cs.Job(chat_id=1, book=None, chat_session_id="mine", question="Q?")))
    assert calls[-1]["resume"] == "mine" and not calls[-1]["fork"] and calls[-1]["cwd"] == tmp_path / "cards"
    asyncio.run(svc.ask(cs.Job(chat_id=1, book=None, chat_session_id="lost", question="Q2?",
                               history=[("user", "Q?"), ("assistant", "A.")])))
    assert calls[-1]["resume"] is None and "<transcript>" in calls[-1]["text"] and "A." in calls[-1]["text"]
    assert ("done", 1, "s-new", "answer", "u") in events


def test_a_stopped_reply_reports_nothing(monkeypatch, tmp_path):
    async def slow(blocks, *, on_delta=None, **kw):
        on_delta("par")
        await asyncio.sleep(10)

    monkeypatch.setattr(cc, "run_turn", slow)
    events = []
    svc = cs.ChatService(lambda *a: events.append(a), tmp_path)

    async def go():
        task = asyncio.create_task(svc.ask(cs.Job(chat_id=1, book=None, chat_session_id=None, question="Q")))
        await asyncio.sleep(0.01)
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)

    asyncio.run(go())
    assert events == [("delta", 1, "par")]


# -- stopping ------------------------------------------------------------------------------------
def test_stop_keeps_the_partial_reply_and_tells_claude(chat_panel):
    panel, worker, db, book_id = chat_panel
    panel.input.setPlainText("Explain zorblatts")
    panel.send()
    chat = panel.chat_id
    assert panel.send_btn.toolTip().startswith("Stop")
    worker.delta.emit(chat, "Zorblatts are")
    panel.stop()
    assert worker.stopped == [chat] and chat not in panel.hub.streams
    reply = db.messages(chat)[-1]
    assert (reply["role"], reply["text"], reply["stopped"]) == ("assistant", "Zorblatts are", 1)
    assert panel.send_btn.toolTip().startswith("Send")
    worker.done.emit(chat, "late", "whole reply", "u")  # arrived as it stopped: ignored
    assert len(db.messages(chat)) == 2 and db.chat(chat)["session_id"] is None

    panel.input.setPlainText("go on")
    panel.send()
    context = worker.jobs[-1].context
    assert "<stopped_reply>\nZorblatts are\n</stopped_reply>" in context and "Explain zorblatts" in context


def test_escape_stops_a_reply(chat_panel):
    panel, worker, db, book_id = chat_panel
    panel.input.setPlainText("q")
    panel.send()
    panel.input.cancelled.emit()
    assert worker.stopped == [panel.chat_id] and db.messages(panel.chat_id)[-1]["stopped"]


# -- throwaway chats -----------------------------------------------------------------------------
def test_throwaway_chats_go_when_left(chat_panel):
    panel, worker, db, book_id = chat_panel
    panel.throwaway_btn.click()
    assert panel._throwaway and panel.throwaway_bar.isVisibleTo(panel)
    panel.input.setPlainText("secret question")
    panel.send()
    chat = panel.chat_id
    assert db.chat(chat)["throwaway"] and db.chats(book_id) == []
    worker.done.emit(chat, "s1", QUIZ, "u1")
    assert db.cards(book_id) == [] and worker.titles == []  # no cards, and no title asked for

    panel.new_chat()  # leaving it deletes it, with its Claude session
    assert db.chat(chat) is None and worker.forgotten == [(book_id, ["s1"])]
    assert panel._throwaway  # new chats stay throwaway until the toggle is turned off

    # Turning it off in a throwaway chat leaves (and so deletes) the chat; one still streaming is stopped.
    panel.input.setPlainText("another")
    panel.send()
    other = panel.chat_id
    panel.throwaway_btn.click()
    assert db.chat(other) is None and worker.stopped == [other] and other not in panel.hub.streams
    assert not panel._throwaway and panel.chat_id is None and not panel.throwaway_bar.isVisibleTo(panel)


def test_saving_a_throwaway_chat_keeps_it(chat_panel):
    panel, worker, db, book_id = chat_panel
    panel.set_throwaway(True)
    panel.input.setPlainText("quiz me")
    panel.send()
    chat = panel.chat_id
    worker.done.emit(chat, "s1", QUIZ, "u1")
    panel.save_throwaway()
    assert not db.chat(chat)["throwaway"] and [c["id"] for c in db.chats(book_id)] == [chat]
    assert len(db.cards(book_id)) == 1 and not panel.throwaway_btn.isChecked()
    assert worker.titles[-1][0] == chat  # named now
    panel.new_chat()
    assert db.chat(chat) is not None


def test_throwaway_chats_left_by_a_crash_are_dropped(tmp_path):
    from study_reader.chat_panel import drop_throwaway_chats

    db = Database(tmp_path / "t.db")
    chat = db.create_chat(None, "x", throwaway=True)
    db.update_chat(chat, session_id="s")
    forgotten = []

    class Worker:
        def forget_sessions(self, book_id, ids, wait=True):
            forgotten.append((book_id, ids, wait))

    drop_throwaway_chats(db, Worker())
    assert db.chat(chat) is None and forgotten == [(None, ["s"], False)]


# -- two panels ------------------------------------------------------------------------------------
def test_panels_share_replies(chat_panel):
    panel, review, worker, db, book_id = review_panel(chat_panel)
    review.set_book(book_id)
    panel.input.setPlainText("q")
    panel.send()
    chat = panel.chat_id
    assert review.chat_list.count() == 1  # the other panel lists the new chat
    review.open_chat(chat)
    review.input.setPlainText("again")
    review.send()  # already waiting for a reply: not sent twice
    assert len(worker.jobs) == 1
    worker.delta.emit(chat, "part")
    assert panel.hub.streams[chat] == "part"  # counted once
    worker.done.emit(chat, "s1", "answer", "u1")
    assert [m["text"] for m in db.messages(chat)] == ["q", "answer"]  # saved once
    review.stop()  # nothing on the way: nothing happens
    assert worker.stopped == []

    # A reply is stopped from either panel by the one that asked for it.
    review.input.setPlainText("more")
    review.send()
    worker.delta.emit(chat, "so")
    panel.stop()
    assert db.messages(chat)[-1]["text"] == "so" and db.messages(chat)[-1]["stopped"]


# -- the Review page's panel --------------------------------------------------------------------
def test_review_chats_follow_the_card(chat_panel):
    panel, review, worker, db, book_id = review_panel(chat_panel)
    deck = db.ensure_deck("Deck")
    first = card_of(db, db.add_card_note("flashcard", {"front": "a", "back": "b"}, deck, book_id=book_id))
    loose = card_of(db, db.add_card_note("flashcard", {"front": "c", "back": "d"}, deck))

    review.set_card(card(db, first))
    assert review.book_id == book_id and review.chat_id is None and review.isEnabled()
    assert review.chips.count() == 1  # the card goes with the question
    review.input.setPlainText("hint please")
    review.send()
    chat = review.chat_id
    assert db.chat(chat)["card_id"] == first and db.chat(chat)["book_id"] == book_id
    job = worker.jobs[-1]
    assert "<review_card>" in job.context and "Don't give the answer away" in job.context
    worker.done.emit(chat, "s1", "Think tea.", "u1")
    review.set_card(card(db, first, revealed=True))  # answered: the same card, now with its answer
    assert review.chat_id == chat
    review.input.setPlainText("why?")
    review.send()
    assert "Answer: Back" in worker.jobs[-1].context and "Don't give" not in worker.jobs[-1].context
    worker.done.emit(chat, "s1", "Because.", "u2")

    # A card with no book: the chats about such cards, in a session with no book.
    review.set_card(card(db, loose))
    assert review.book_id is None and review.chat_id is None and review.chat_list.count() == 0
    review.input.setPlainText("what is c?")
    review.send()
    bookless = review.chat_id
    assert db.chat(bookless)["book_id"] is None and worker.jobs[-1].book is None
    worker.done.emit(bookless, "s3", QUIZ, "u3")
    made = db.conn.execute("SELECT deck_id FROM cards WHERE message_id IS NOT NULL").fetchall()
    assert [r[0] for r in made] == [deck]  # its quiz cards join the card's deck

    # Back to the first card: its chat comes back up. Removing the chip leaves the card out.
    review.set_card(card(db, first))
    assert review.chat_id == chat and review.book_id == book_id
    review._drop_card()
    review.input.setPlainText("unrelated")
    review.send()
    assert "<review_card>" not in worker.jobs[-1].context
    worker.done.emit(chat, "s1", "ok", "u4")

    # The overview: the chat stays, with no card to send.
    review.set_card(None)
    assert review.chat_id == chat and review.chips.count() == 0


def test_review_opens_the_quiz_chat_a_card_came_from(chat_panel):
    panel, review, worker, db, book_id = review_panel(chat_panel)
    from study_reader import srs

    source = db.create_chat(book_id, "Quiz")
    reply = db.add_message(source, "assistant", QUIZ)
    (card_id,) = srs.add_quiz_cards(db, book_id, reply, QUIZ)
    review.set_card(card(db, card_id, chat_id=source))
    assert review.chat_id == source
    assert review.chips.count() == 1  # questions there carry the card too
    review.set_card(None)
    other = db.create_chat(book_id, "Other")
    review.show_chat(other)  # Open chat, or picking another chat from the list
    assert review.chat_id == other


def test_card_context_hides_the_answer_until_shown():
    q = {"type": "choice", "question": "Which?", "choices": ["red", "blue"], "answer": 1, "explanation": "Sky."}
    hidden = quiz.card_context(q, "Colors, question 2", None)
    assert "Which?" in hidden and "- blue" in hidden and "Sky." not in hidden and "Don't give" in hidden
    shown = quiz.card_context(q, "Colors, question 2", {"response": {"choice": 0}, "verdict": "incorrect"})
    assert "Answer: blue" in shown and "I chose: red (incorrect)" in shown and "Sky." in shown
    written = {"type": "written", "question": "Why?", "answer": "Because.", "rubric": "Mentions cause."}
    assert "I wrote:\n<answer>\nhmm\n</answer>" in quiz.card_context(written, "", {"response": {"text": "hmm"}})
    assert "I've seen the answer." in quiz.card_context(written, "", {"response": {}})
    assert "stopped it before any" in quiz.stopped_context("q", "")


def test_review_page_chat_panel(pdf_path, tmp_path, monkeypatch):
    monkeypatch.setenv("QT_QPA_PLATFORM", "offscreen")
    monkeypatch.setenv("STUDY_READER_DATA", str(tmp_path / "data"))
    from PySide6.QtCore import Qt
    from PySide6.QtTest import QTest
    from PySide6.QtWidgets import QApplication

    from study_reader.main_window import MainWindow

    app = QApplication.instance() or QApplication([])
    db = Database(tmp_path / "t.db")
    db.set_setting("review_calc", "1")  # from before the chat panel: the calculator was open
    leftover = db.create_chat(None, "crashed", throwaway=True)
    win = MainWindow(db)
    assert db.chat(leftover) is None
    win.show()
    win.activateWindow()
    win.open_book(pdf_path)
    db.add_card_note("flashcard", {"front": "q", "back": "a"}, db.ensure_deck("D"), book_id=win.book_id)
    win.show_page("review")
    assert win.review_calc.isVisible() and not win.review_chat.isVisible()
    app.processEvents()
    QTest.keyClick(win, Qt.Key.Key_F10)  # the chat takes the calculator's place
    assert win.review_chat.isVisible() and not win.review_calc_action.isChecked()
    assert db.get_setting("review_panel") == "chat" and win.chat_action.isChecked()  # the book's left alone
    win.review.start()
    assert win.review_chat._card["id"] == win.review.card_id and win.review_chat.book_id == win.book_id
    win.review.check(win.review.card_id, {})
    assert win.review_chat._card["revealed"]

    shown = []
    monkeypatch.setattr(win.review, "_js", lambda f, *args: shown.append((f, args)))
    win.review.refresh()
    assert shown[-1][1][0]["chat_panel"] is True
    win.review.channel.registeredObjects()["review"].open("chat")  # the Chat button
    assert not win.review_chat.isVisible() and win.review.chat_panel is False and db.get_setting("review_panel") == ""
    win.close()
