from study_reader.db import Database
from study_reader.notes import export_name, matches, to_markdown


def test_notes_roundtrip_in_reading_order(tmp_path):
    db = Database(tmp_path / "t.db")
    book_id = db.upsert_book("h", "/a.pdf", "A", 3)["id"]
    later = db.add_note(book_id, 1, "lower on page two", [[10, 200, 50, 210]])
    first = db.add_note(book_id, 0, "page one", [[10, 40, 50, 50]], "my *note*")
    upper = db.add_note(book_id, 1, "upper on page two", [[10, 20, 50, 30]])
    assert [n["id"] for n in db.notes(book_id)] == [first, upper, later]
    assert db.note(first)["rects"] == [[10, 40, 50, 50]] and db.note(first)["body"] == "my *note*"

    db.update_note(upper, "edited")
    assert db.note(upper)["body"] == "edited"
    db.delete_note(later)
    assert db.note(later) is None
    db.delete_book(book_id)
    assert db.notes(book_id) == []


def test_search_and_export():
    notes = [
        {"page": 0, "text": "A zorblatt is a\nseven-legged device.", "body": "Like a $k$-legged stool"},
        {"page": 0, "text": "used to calibrate marmalade", "body": ""},
        {"page": 2, "text": "Never store a zorblatt near a window.", "body": "Why?\n\n- light"},
    ]
    assert matches(notes[0], "ZORBLATT stool") and not matches(notes[0], "zorblatt window")
    assert matches(notes[1], "") and matches(notes[2], "why")
    assert to_markdown("The Zorblatt Manual", notes) == (
        "# Notes on The Zorblatt Manual\n\n"
        "## Page 1\n\n"
        "> A zorblatt is a seven-legged device.\n\n"
        "Like a $k$-legged stool\n\n"
        "> used to calibrate marmalade\n\n"
        "## Page 3\n\n"
        "> Never store a zorblatt near a window.\n\n"
        "Why?\n\n- light\n"
    )
    assert export_name("Ch. 3: Pumps/Valves?") == "Ch. 3 Pumps Valves notes.md"


def test_taking_a_note_highlights_it(pdf_path, tmp_path, monkeypatch):
    monkeypatch.setenv("QT_QPA_PLATFORM", "offscreen")
    monkeypatch.setenv("STUDY_READER_DATA", str(tmp_path / "data"))
    from PySide6.QtWidgets import QApplication
    from study_reader.main_window import MainWindow

    app = QApplication.instance() or QApplication([])
    db = Database(tmp_path / "t.db")
    win = MainWindow(db)
    win.show()
    win.open_book(pdf_path)

    win.reader._selection = {0: win.doc.words(0)[:3]}
    assert win.reader.choose_note()
    assert win.notes_action.isChecked() and not win.chat_action.isChecked()
    assert win.right.currentWidget() is win.notes
    (note,) = db.notes(win.book_id)
    assert note["text"] == "Chapter One. A" and note["body"] == ""
    (mark,) = win.reader._marks[0]
    assert mark["notes"][0]["id"] == note["id"] and not mark["snippets"]

    # Cancelling a new note's first edit with nothing written drops it.
    win.notes.cancel(note["id"])
    assert db.notes(win.book_id) == [] and not win.reader._marks

    win.reader._selection = {0: win.doc.words(0)[:3]}
    win.reader.choose_note()
    note_id = db.notes(win.book_id)[0]["id"]
    win.notes.save(note_id, "A **seven-legged** device")
    win.notes.cancel(note_id)  # no longer new: kept
    assert win.reader._marks[0][0]["notes"][0]["body"] == "A **seven-legged** device"
    assert win.notes.title.text() == "Notes · 1"

    win.notes.search.setText("nothing like it")
    assert win.notes.shown == []
    win.notes.reveal(note_id)  # clears a search that hides the note
    assert [n["id"] for n in win.notes.shown] == [note_id]

    # The panel is remembered per book, like chats and the calculator.
    win.show_library()
    assert db.book(1)["right_panel"] == "notes"
    win.open_book(pdf_path)
    assert win.notes_action.isChecked() and win.right.currentWidget() is win.notes

    win.notes.remove(note_id)
    assert not win.reader._marks and win.notes.title.text() == "Notes"


def test_ctrl_enter_asks_only_from_the_reader(pdf_path, tmp_path, monkeypatch):
    """Ctrl+Enter sends the selection only while the reader has focus, so the review, quiz and
    note pages (and the composer) still get it."""
    monkeypatch.setenv("QT_QPA_PLATFORM", "offscreen")
    monkeypatch.setenv("STUDY_READER_DATA", str(tmp_path / "data"))
    from PySide6.QtCore import Qt
    from PySide6.QtTest import QTest
    from PySide6.QtWidgets import QApplication
    from study_reader.main_window import MainWindow

    app = QApplication.instance() or QApplication([])
    db = Database(tmp_path / "t.db")
    win = MainWindow(db)
    win.show()
    win.activateWindow()
    win.open_book(pdf_path)
    chosen = []
    win.reader.snippetChosen.connect(lambda snippet, new: chosen.append(new))

    win.reader._selection = {0: win.doc.words(0)[:3]}
    win.chat.input.setFocus()
    app.processEvents()
    QTest.keyClick(win.chat.input, Qt.Key.Key_Return, Qt.KeyboardModifier.ControlModifier)
    assert chosen == [] and win.reader._selection

    win.reader.setFocus()
    app.processEvents()
    QTest.keyClick(win.reader, Qt.Key.Key_Return, Qt.KeyboardModifier.ControlModifier)
    assert chosen == [True]
