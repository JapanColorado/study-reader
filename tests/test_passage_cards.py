import asyncio
import json
import os
import sqlite3

import pytest

from study_reader import chat_service as cs
from study_reader import passage_cards, srs
from study_reader.db import Database

from test_core import FakeClaude

REPLY = r'''Here you go.
```quiz Rinsing zorblatts
{"questions": [
  {"type": "flashcard", "question": "How long is a zorblatt rinsed?", "answer": "41 seconds", "tags": ["rinsing"]},
  {"type": "cloze", "text": "A zorblatt is rinsed in {{c1::cold tea}} for {{c2::41}} seconds, $t = {{c2::41}}$.", "page": 3},
  {"type": "choice", "question": "What is a zorblatt rinsed in?", "choices": ["Milk", "Cold tea"], "answer": 1,
   "section": "1.1"},
  {"type": "cloze", "text": "No deletion here"},
  {"type": "choice", "question": "Broken", "choices": ["only one"], "answer": 0}
]}
```'''
TOC = [[1, "Chapter 1 Rinsing", 0], [2, "1.1 Tea", 1], [2, "1.2 Time", 2]]


def setup(tmp_path):
    db = Database(tmp_path / "t.db")
    book_id = db.upsert_book("h", "/a.pdf", "A", 3)["id"]
    db.update_book(book_id, toc=json.dumps(TOC))
    return db, book_id


def test_reply_parses_into_cards_skipping_unreadable_ones():
    title, cards = passage_cards.cards(REPLY)
    assert title == "Rinsing zorblatts"
    assert [c["kind"] for c in cards] == ["basic", "cloze", "quiz"]
    assert cards[0]["fields"]["front"] == "How long is a zorblatt rinsed?" and cards[0]["tags"] == ["rinsing"]
    assert cards[1]["page"] == 3 and cards[2]["section"] == "1.1"
    with pytest.raises(ValueError):
        passage_cards.cards("Sorry, I can't.")


def test_cards_join_the_books_deck_as_new_cards(tmp_path):
    db, book_id = setup(tmp_path)
    snippet = {"kind": "text", "page": 0, "text": "Rinse it in cold tea.", "rects": [[1, 2, 3, 4]]}
    ids = passage_cards.add_cards(db, book_id, snippet, REPLY)
    assert len(ids) == 4  # the flashcard, two cloze cards from one note, the choice
    cards = [db.card(i) for i in ids]
    assert all(c["deck_id"] == db.book(book_id)["deck_id"] and srs.is_new(c) for c in cards)
    assert {c["title"] for c in cards} == {"Rinsing zorblatts"} and all(c["message_id"] is None for c in cards)
    # Their own page, else the passage's; the section named wins over the page.
    assert [c["page"] for c in cards] == [0, 2, 2, 0]
    tags = [db.card_note(c["note_id"])["tags"].split() for c in cards]
    assert tags[0] == ["rinsing", "A::Chapter_1_Rinsing"] and tags[1] == ["A::Chapter_1_Rinsing::1.2_Time"]
    assert tags[3] == ["A::Chapter_1_Rinsing::1.1_Tea"]
    assert [srs.question(c)["type"] for c in cards] == ["flashcard", "cloze", "cloze", "choice"]
    # The passage stays highlighted while it has cards; one that gave none isn't kept.
    (passage,) = db.card_passages(book_id)
    assert passage["card_ids"] == sorted(ids) and passage["rects"] == [[1, 2, 3, 4]]
    assert passage["title"] == "Rinsing zorblatts" and passage["text"] == "Rinse it in cold tea."
    assert passage_cards.add_cards(db, book_id, snippet, '```quiz Nothing\n{"questions": []}\n```') == []
    assert len(db.card_passages(book_id)) == 1
    db.delete_card_passage(passage["id"])  # its cards stay
    assert db.card_passages(book_id) == [] and all(db.card(i)["passage_id"] is None for i in ids)


def test_a_passage_goes_once_its_cards_do(tmp_path):
    db, book_id = setup(tmp_path)
    ids = passage_cards.add_cards(db, book_id, {"kind": "text", "page": 0, "rects": []}, REPLY)
    db.conn.execute(f"DELETE FROM cards WHERE id IN ({','.join(map(str, ids))})")
    assert db.card_passages(book_id) == []


def test_migration_adds_the_passage_column(tmp_path):
    Database(tmp_path / "t.db").conn.close()
    conn = sqlite3.connect(tmp_path / "t.db")
    conn.execute("DROP INDEX cards_passage")
    conn.execute("ALTER TABLE cards DROP COLUMN passage_id")
    conn.commit()
    conn.close()
    db = Database(tmp_path / "t.db")
    assert "passage_id" in {r["name"] for r in db.conn.execute("PRAGMA table_info(cards)")}


def test_request_asks_for_cards_without_a_fixed_count():
    text = passage_cards.request({"kind": "text", "page": 4, "text": "x"})
    assert "page 5" in text and "cloze" in text and "selected passage" in text
    assert "screenshot" in passage_cards.request({"kind": "image", "page": 0})


def test_making_cards_forks_from_the_book(monkeypatch, tmp_path, pdf_path):
    fake = FakeClaude(monkeypatch)
    events = []
    svc = cs.ChatService(lambda *a: events.append(a), tmp_path)
    job = cs.Job(chat_id=0, book=cs.BookRef(1, pdf_path, "Zorblatt", None, None), chat_session_id=None,
                 question="Make cards", detached=True)
    asyncio.run(svc.make_cards(job, 7))
    assert "<book>" in fake.calls[0]["text"] and fake.calls[1]["resume"] == "s1" and fake.calls[1]["fork"]
    assert ("carded", 7, "answer") in events


def test_make_cards_from_the_reader(pdf_path, tmp_path, monkeypatch):
    monkeypatch.setenv("QT_QPA_PLATFORM", "offscreen")
    monkeypatch.setenv("STUDY_READER_DATA", str(tmp_path / "data"))
    from PySide6.QtWidgets import QApplication
    from study_reader.main_window import MainWindow

    QApplication.instance() or QApplication([])
    db = Database(tmp_path / "t.db")
    win = MainWindow(db)
    jobs = []
    monkeypatch.setattr(win.worker, "submit_cards", lambda job, rid: jobs.append((job, rid)))
    win.show()
    win.open_book(pdf_path)
    win.reader.cardsChosen.emit({"kind": "image", "page": 0, "text": "", "rects": [[0, 0, 9, 9]], "png": b"png"})
    (job, rid), = jobs
    assert job.detached and job.book.id == win.book_id and job.chat_session_id is None
    image = job.snippets[0].image_path
    assert open(image, "rb").read() == b"png"
    assert win.notice.isVisible() and "Making cards" in win.notice.text()

    marks = [m for m in win.reader._marks.get(0, []) if m["passages"]]
    assert len(marks) == 1 and marks[0]["passages"][0]["id"] is None  # marked while Claude works
    win.worker.carded.emit(rid, REPLY)
    (mark,) = [m for m in win.reader._marks.get(0, []) if m["passages"]]
    assert len(mark["passages"][0]["card_ids"]) == 4
    assert "Added 4 cards from p. 1" in win.notice.text()
    assert len(db.cards(win.book_id)) == 4 and "4 cards to review" in win.review_action.toolTip()
    assert not os.path.exists(image)  # the screenshot was only for the request

    win.reader.cardsChosen.emit({"kind": "text", "page": 1, "text": "Zorblatts", "rects": [], "png": None})
    win.worker.cardsFailed.emit(jobs[-1][1], "offline")
    assert "Couldn't make cards from p. 2: offline" in win.notice.text()
    assert not win.reader._marks.get(1)  # a request that failed leaves no highlight

    from PySide6.QtWidgets import QMenu
    from study_reader import main_window
    shown = []

    class Menu(QMenu):
        def exec(self, pos):
            shown.append([a.text() for a in self.actions()])

    monkeypatch.setattr(main_window, "QMenu", Menu)
    win._highlight_menu([], [], mark["passages"], None)
    assert shown[0][0] == "4 cards: Rinsing zorblatts" and "Make cards from this" in shown[0]
    assert "Remove card highlight (keep the cards)" in shown[0]
    win.show_passage_cards(mark["passages"][0]["card_ids"])
    assert win.page() == "cards" and win.browser.model.rowCount() == 4
    win.close()
