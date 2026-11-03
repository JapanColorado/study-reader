from study_reader import book_search
from study_reader.book_search import BookSearch, SearchIndex, fold, page_text, section_titles
from study_reader.document import Document, Word


def _w(text, x0, line=0, block=0, y0=0.0):
    return Word(x0, y0 + 10 * line, x0 + 6 * len(text), y0 + 10 * line + 8, text, block, line)


def test_fold_ignores_case_accents_ligatures_and_quotes():
    assert fold("  Café   ﬁnal  “Don’t” – ok ") == 'cafe final "don\'t" - ok'


def test_matches_across_words_with_partial_rects():
    words = [_w("Zorblatts", 0), _w("are", 60), _w("great", 90)]
    pt = page_text(words)
    assert pt.text == "zorblatts are great"
    [m] = book_search.find_in_page(pt, 3, fold("blatts ARE"))
    assert m.page == 3
    # "blatts" is the last 6 of 9 characters of the first word, then all of "are": one line.
    [(x0, y0, x1, y1)] = m.rects
    assert x0 == 6 * 9 * 3 / 9 and x1 == words[1].x1
    assert m.context == ("Zor", "blatts are", " great")


def test_hyphenated_line_break_is_joined_and_rects_split_by_line():
    words = [_w("contin-", 0, line=0), _w("uous", 0, line=1)]
    pt = page_text(words)
    assert pt.text == "continuous"
    [m] = book_search.find_in_page(pt, 0, "continuous")
    assert len(m.rects) == 2


def test_context_is_trimmed_to_words(monkeypatch):
    words = [_w(t, 7 * i * 6) for i, t in enumerate("one two three four five six seven".split())]
    pt = page_text(words)
    [m] = book_search.find_in_page(pt, 0, "four")
    before, hit, after = book_search.context(pt, m.start, m.end, chars=7)
    assert (before, hit, after) == ("…three ", "four", " five…")


def test_book_search_from_the_current_page_wraps_and_stays_in_order(pdf_path):
    doc = Document(pdf_path)
    s = BookSearch(SearchIndex(doc), "ZORBLATT", start_page=1)
    first = s.step(10)
    # The first step stops at the end of the book: pages 1 and 2, inserted at 0.
    assert first[0] == 0 and [m.page for m in first[1]] == [1, 2]
    at, found = s.step(10)
    assert at == 0 and [m.page for m in found] == [0]
    assert s.done and [m.page for m in s.matches] == [0, 1, 2]
    assert s.after(2) == 2 and s.after(3) == 0
    assert s.position(s.matches[1]) == 1
    doc.close()


def test_book_search_limit(pdf_path):
    doc = Document(pdf_path)
    s = BookSearch(SearchIndex(doc), "a", limit=4)
    assert len(s.run()) == 4 and s.truncated and s.done
    assert BookSearch(SearchIndex(doc), "   ").done
    doc.close()


def test_section_titles():
    title = section_titles([(1, "One", 0), (2, "One.A", 2), (1, "Two", 5)])
    assert [title(p) for p in (0, 1, 2, 4, 5, 9)] == ["One", "One", "One.A", "One.A", "Two", "Two"]
    assert section_titles([(1, "Late", 3)])(0) == ""


def test_ctrl_f_in_the_window(tmp_path, monkeypatch, pdf_path):
    monkeypatch.setenv("STUDY_READER_DATA", str(tmp_path / "data"))
    from PySide6.QtCore import Qt
    from PySide6.QtTest import QTest
    from PySide6.QtWidgets import QApplication
    from study_reader.db import Database
    from study_reader.main_window import MainWindow

    app = QApplication.instance() or QApplication([])
    win = MainWindow(Database(tmp_path / "t.db"))
    win.show()
    win.open_book(pdf_path)
    win.reader.go_to(1)
    app.processEvents()
    find = win.find

    QTest.keyClick(win.reader, Qt.Key.Key_F, Qt.KeyboardModifier.ControlModifier)
    assert find.active and find.bar.edit.hasFocus()
    assert win.sidebar.isVisible() and win.side_stack.currentWidget() is find.list
    find.bar.edit.setText("zorblatt")
    find._typing.stop()
    find.start(find.bar.edit.text())
    while not find.search.done:
        find._step()
    assert [m.page for m in find.search.matches] == [0, 1, 2]
    assert find.current.page == 1  # the first match from the page being read
    assert find.bar.count.text() == "2 of 3"
    assert win.sidebar_title.text() == "3 matches"
    assert win.reader._find_current[0] == 1 and sorted(win.reader._find) == [0, 1, 2]

    QTest.keyClick(find.bar.edit, Qt.Key.Key_Return)
    assert find.current.page == 2
    QTest.keyClick(find.bar.edit, Qt.Key.Key_Return)
    assert find.current.page == 0  # wraps
    QTest.keyClick(find.bar.edit, Qt.Key.Key_Return, Qt.KeyboardModifier.ShiftModifier)
    assert find.current.page == 2
    assert find.list.currentIndex().row() == 2

    QTest.keyClick(find.bar.edit, Qt.Key.Key_Escape)
    assert not find.active and not win.reader._find
    assert win.sidebar_title.text() == "Contents" and win.side_stack.currentWidget() is win.toc

    # Reopened, the last search comes back as it was.
    find.open()
    assert find.bar.count.text() == "3 of 3" and win.reader._find
    win.close_book()
    assert not find.active and find.search is None


def test_folding_that_changes_length_maps_back_to_the_printed_word():
    words = [_w("ﬁnal", 0), _w("Straße", 40)]
    pt = page_text(words)
    assert pt.text == "final strasse"
    [m] = book_search.find_in_page(pt, 0, "nal stras")
    assert m.context == ("ﬁ", "nal Straß", "e")  # each printed character goes to one side
    [(x0, _, x1, _)] = m.rects
    assert x0 == words[0].x1 / 4 and x1 == words[1].x0 + (words[1].x1 - words[1].x0) * 5 / 6
