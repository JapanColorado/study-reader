"""The Practice page's chat panel, throwaway chats, stopping a reply, and panels sharing a hub."""

import asyncio
import json
import sqlite3

from study_reader import chat_service as cs
from study_reader import claude_client as cc
from study_reader import quiz
from study_reader.db import Database

QUIZ = '''```quiz Zorblatts
{"questions": [{"type": "self_check", "question": "Rinse time?", "answer": "41 seconds"}]}
```'''


def question_of(db, group_id):
    return db.conn.execute("SELECT id FROM questions WHERE group_id=?", (group_id,)).fetchone()[0]


def practice_panel(chat_panel):
    """The fixture's worker and database with a second, Practice page panel on the same hub."""
    from study_reader.chat_panel import ChatPanel

    panel, worker, db, book_id = chat_panel
    other = ChatPanel(db, worker, hub=panel.hub, practice=True)
    return panel, other, worker, db, book_id


def shown(db, question_id, revealed=False, chat_id=None):
    """What PracticePanel.chat_question hands the panel."""
    row = db.question(question_id)
    q = {"type": "self_check", "question": "Front?", "answer": "Back"}
    return {"id": question_id, "book_id": row["book_id"], "chat_id": chat_id, "title": "Deck", "revealed": revealed,
            "context": quiz.question_context(q, "Deck", {"response": {}} if revealed else None)}


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
    assert db.chat(1)["model"] == "opus" and db.chat(1)["question_id"] is None and not db.chat(1)["throwaway"]
    assert [m["text"] for m in db.messages(1)] == ["hi"] and not db.messages(1)[0]["stopped"]
    bookless = db.create_chat(None, "About a question")  # book_id is optional now
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
    asyncio.run(svc.ask(cs.Job(chat_id=1, book=None, chat_session_id="mine", message="Q?")))
    assert calls[-1]["resume"] == "mine" and not calls[-1]["fork"] and calls[-1]["cwd"] == tmp_path / "cards"
    asyncio.run(svc.ask(cs.Job(chat_id=1, book=None, chat_session_id="lost", message="Q2?",
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
        task = asyncio.create_task(svc.ask(cs.Job(chat_id=1, book=None, chat_session_id=None, message="Q")))
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
    assert db.questions(book_id) == [] and worker.titles == []  # no questions, and no title asked for

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
    assert len(db.questions(book_id)) == 1 and not panel.throwaway_btn.isChecked()
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
    panel, practice_chat, worker, db, book_id = practice_panel(chat_panel)
    practice_chat.set_book(book_id)
    panel.input.setPlainText("q")
    panel.send()
    chat = panel.chat_id
    assert practice_chat.chat_list.count() == 1  # the other panel lists the new chat
    practice_chat.open_chat(chat)
    practice_chat.input.setPlainText("again")
    practice_chat.send()  # already waiting for a reply: not sent twice
    assert len(worker.jobs) == 1
    worker.delta.emit(chat, "part")
    assert panel.hub.streams[chat] == "part"  # counted once
    worker.done.emit(chat, "s1", "answer", "u1")
    assert [m["text"] for m in db.messages(chat)] == ["q", "answer"]  # saved once
    practice_chat.stop()  # nothing on the way: nothing happens
    assert worker.stopped == []

    # A reply is stopped from either panel by the one that asked for it.
    practice_chat.input.setPlainText("more")
    practice_chat.send()
    worker.delta.emit(chat, "so")
    panel.stop()
    assert db.messages(chat)[-1]["text"] == "so" and db.messages(chat)[-1]["stopped"]


# -- the Practice page's panel ------------------------------------------------------------------
def test_practice_chats_follow_the_question(chat_panel):
    panel, practice_chat, worker, db, book_id = practice_panel(chat_panel)
    deck = db.ensure_deck("Deck")
    first = question_of(db, db.add_question_group("self_check", {"front": "a", "back": "b"}, deck, book_id=book_id))
    loose = question_of(db, db.add_question_group("self_check", {"front": "c", "back": "d"}, deck))

    practice_chat.set_question(shown(db, first))
    assert practice_chat.book_id == book_id and practice_chat.chat_id is None and practice_chat.isEnabled()
    assert practice_chat.chips.count() == 1  # the question goes with the message
    practice_chat.input.setPlainText("hint please")
    practice_chat.send()
    chat = practice_chat.chat_id
    assert db.chat(chat)["question_id"] == first and db.chat(chat)["book_id"] == book_id
    job = worker.jobs[-1]
    assert "<practice_question>" in job.context and "Don't give the answer away" in job.context
    worker.done.emit(chat, "s1", "Think tea.", "u1")
    practice_chat.set_question(shown(db, first, revealed=True))  # answered: the same question, now with its answer
    assert practice_chat.chat_id == chat
    practice_chat.input.setPlainText("why?")
    practice_chat.send()
    assert "Answer: Back" in worker.jobs[-1].context and "Don't give" not in worker.jobs[-1].context
    worker.done.emit(chat, "s1", "Because.", "u2")

    # A question with no book: the chats about such questions, in a session with no book.
    practice_chat.set_question(shown(db, loose))
    assert practice_chat.book_id is None and practice_chat.chat_id is None and practice_chat.chat_list.count() == 0
    practice_chat.input.setPlainText("what is c?")
    practice_chat.send()
    bookless = practice_chat.chat_id
    assert db.chat(bookless)["book_id"] is None and worker.jobs[-1].book is None
    worker.done.emit(bookless, "s3", QUIZ, "u3")
    made = db.conn.execute("SELECT deck_id FROM questions WHERE message_id IS NOT NULL").fetchall()
    assert [r[0] for r in made] == [deck]  # its quiz questions join the question's deck

    # Back to the first question: its chat comes back up. Removing the chip leaves the question out.
    practice_chat.set_question(shown(db, first))
    assert practice_chat.chat_id == chat and practice_chat.book_id == book_id
    practice_chat._drop_question()
    practice_chat.input.setPlainText("unrelated")
    practice_chat.send()
    assert "<practice_question>" not in worker.jobs[-1].context
    worker.done.emit(chat, "s1", "ok", "u4")

    # The overview: the chat stays, with no question to send.
    practice_chat.set_question(None)
    assert practice_chat.chat_id == chat and practice_chat.chips.count() == 0


def test_practice_opens_the_quiz_chat_a_question_came_from(chat_panel):
    panel, practice_chat, worker, db, book_id = practice_panel(chat_panel)
    from study_reader import practice

    source = db.create_chat(book_id, "Quiz")
    reply = db.add_message(source, "assistant", QUIZ)
    (question_id,) = practice.add_quiz_questions(db, book_id, reply, QUIZ)
    practice_chat.set_question(shown(db, question_id, chat_id=source))
    assert practice_chat.chat_id == source
    assert practice_chat.chips.count() == 1  # messages there carry the question too
    practice_chat.set_question(None)
    other = db.create_chat(book_id, "Other")
    practice_chat.show_chat(other)  # Open chat, or picking another chat from the list
    assert practice_chat.chat_id == other


def test_question_context_hides_the_answer_until_shown():
    q = {"type": "choice", "question": "Which?", "choices": ["red", "blue"], "answer": 1, "explanation": "Sky."}
    hidden = quiz.question_context(q, "Colors, question 2", None)
    assert "Which?" in hidden and "- blue" in hidden and "Sky." not in hidden and "Don't give" in hidden
    shown = quiz.question_context(q, "Colors, question 2", {"response": {"choice": 0}, "verdict": "incorrect"})
    assert "Answer: blue" in shown and "I chose: red (incorrect)" in shown and "Sky." in shown
    short = {"type": "short_answer", "question": "Why?", "answer": "Because.", "rubric": "Mentions cause."}
    assert "I wrote:\n<answer>\nhmm\n</answer>" in quiz.question_context(short, "", {"response": {"text": "hmm"}})
    assert "I've seen the answer." in quiz.question_context(short, "", {"response": {}})
    assert "stopped it before any" in quiz.stopped_context("q", "")


def test_practice_page_chat_panel(pdf_path, tmp_path, monkeypatch):
    monkeypatch.setenv("QT_QPA_PLATFORM", "offscreen")
    monkeypatch.setenv("STUDY_READER_DATA", str(tmp_path / "data"))
    from PySide6.QtCore import Qt
    from PySide6.QtTest import QTest
    from PySide6.QtWidgets import QApplication

    from study_reader.main_window import MainWindow

    app = QApplication.instance() or QApplication([])
    db = Database(tmp_path / "t.db")
    db.set_setting("practice_calc", "1")  # from before the chat panel: the calculator was open
    leftover = db.create_chat(None, "crashed", throwaway=True)
    win = MainWindow(db)
    assert db.chat(leftover) is None
    win.show()
    win.activateWindow()
    win.open_book(pdf_path)
    db.add_question_group("self_check", {"front": "q", "back": "a"}, db.ensure_deck("D"), book_id=win.book_id)
    win.show_page("practice")
    assert win.practice_calc.isVisible() and not win.practice_chat.isVisible()
    app.processEvents()
    QTest.keyClick(win, Qt.Key.Key_F10)  # the chat takes the calculator's place
    assert win.practice_chat.isVisible() and not win.practice_calc_action.isChecked()
    assert db.get_setting("practice_panel") == "chat" and win.chat_action.isChecked()  # the book's left alone
    win.practice.start()
    assert win.practice_chat._question["id"] == win.practice.question_id and win.practice_chat.book_id == win.book_id
    win.practice.check(win.practice.question_id, {})
    assert win.practice_chat._question["revealed"]

    drawn = []
    monkeypatch.setattr(win.practice, "_js", lambda f, *args: drawn.append((f, args)))
    win.practice.refresh()
    assert drawn[-1][1][0]["chat_panel"] is True
    win.practice.channel.registeredObjects()["practice"].open("chat")  # the Chat button
    assert not win.practice_chat.isVisible() and win.practice.chat_panel is False and db.get_setting("practice_panel") == ""
    win.close()
