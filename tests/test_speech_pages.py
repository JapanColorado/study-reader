import pymupdf

from study_reader.document import Document
from study_reader.speech_pages import BookText


def _save(doc, tmp_path, name="book.pdf", **meta):
    if meta:
        doc.set_metadata(meta)
    path = tmp_path / name
    doc.save(path)
    return Document(path)


def _texts(book, start=(0, 0), end=None):
    return [u.text for u in book.utterances(start, end)]


def test_running_headers_footers_and_page_numbers_are_skipped(tmp_path):
    doc = pymupdf.open()
    for i in range(5):
        page = doc.new_page(width=400, height=500)
        page.insert_text((40, 30), "The Zorblatt Manual" if i % 2 else f"Chapter {i // 2 + 1}: Upkeep", fontsize=9)
        page.insert_text((40, 200), f"Zorblatts need care number {i}. They wobble.", fontsize=12)
        page.insert_text((195, 480), str(i + 11), fontsize=9)
    book = BookText(_save(doc, tmp_path))
    texts = _texts(book)
    assert texts[:2] == ["Zorblatts need care number 0.", "They wobble."]
    joined = " ".join(texts)
    assert "Manual" not in joined and "Upkeep" not in joined and "11" not in joined and "15" not in joined
    assert len(texts) == 10


def test_footnotes_and_their_markers_are_skipped(tmp_path):
    doc = pymupdf.open()
    page = doc.new_page(width=400, height=300)
    body = "Zorblatts are devices."
    page.insert_text((40, 60), body, fontsize=12)
    page.insert_text((40 + pymupdf.get_text_length(body, fontsize=12), 55), "1", fontsize=7)
    page.insert_text((40, 90), "They are rinsed often.", fontsize=12)
    page.insert_text((40, 255), "1", fontsize=6)
    page.insert_text((44, 258), "A zorblatt has seven legs.", fontsize=9)
    texts = _texts(BookText(_save(doc, tmp_path)))
    assert texts == ["Zorblatts are devices.", "They are rinsed often."]


def test_glued_footnote_numbers_are_dropped():
    from study_reader.speech_pages import GLUED_NOTE

    assert GLUED_NOTE.sub("", "complements.18") == "complements."
    assert GLUED_NOTE.sub("", "0.35") == "0.35" and GLUED_NOTE.sub("", "2.2,") == "2.2,"


def test_captions_are_skipped_but_mentions_are_read(tmp_path):
    doc = pymupdf.open()
    page = doc.new_page(width=400, height=300)
    page.insert_text((40, 60), "As Figure 2 shows, zorblatts wobble.", fontsize=11)
    page.insert_text((40, 200), "Figure 2.", fontsize=11, fontname="hebo")
    page.insert_text((100, 200), "A wobbling zorblatt.", fontsize=11)
    page.insert_text((40, 240), "Table 3: Wobble rates", fontsize=11)
    assert _texts(BookText(_save(doc, tmp_path))) == ["As Figure 2 shows, zorblatts wobble."]


def test_hyphenated_words_are_joined_and_cover_both_halves(tmp_path):
    doc = pymupdf.open()
    page = doc.new_page(width=400, height=300)
    page.insert_text((40, 60), "Every zorblatt needs cali-", fontsize=12)
    page.insert_text((40, 76), "bration twice.", fontsize=12)
    (u,) = BookText(_save(doc, tmp_path)).utterances((0, 0))
    assert u.text == "Every zorblatt needs calibration twice."
    start = u.text.index("calibration")
    word = u.word_at(start)
    assert len(word[0]) == 2 and word[0][0][1] < word[0][1][1]  # both lines
    assert len(u.rects[0]) == 2


def test_heading_is_a_sentence_of_its_own(tmp_path):
    doc = pymupdf.open()
    page = doc.new_page(width=400, height=300)
    page.insert_text((40, 50), "Care and Feeding", fontsize=20)
    page.insert_text((40, 90), "Feed the zorblatt daily. It hums.", fontsize=11)
    page.insert_text((40, 104), "Then it sleeps.", fontsize=11)
    us = list(BookText(_save(doc, tmp_path)).utterances((0, 0)))
    assert [u.text for u in us] == ["Care and Feeding.", "Feed the zorblatt daily.", "It hums.", "Then it sleeps."]
    assert us[0].pause > us[1].pause


def test_a_sentence_running_over_a_page_break_has_rects_on_both_pages(tmp_path):
    doc = pymupdf.open()
    doc.new_page(width=400, height=300).insert_text((40, 150), "First one. This sentence runs", fontsize=12)
    doc.new_page(width=400, height=300).insert_text((40, 150), "onto the next page. Done.", fontsize=12)
    us = list(BookText(_save(doc, tmp_path)).utterances((0, 0)))
    assert [u.text for u in us] == ["First one.", "This sentence runs onto the next page.", "Done."]
    assert set(us[1].rects) == {0, 1}
    assert us[1].first[0] == 0 and us[1].last[0] == 1


def test_web_snapshot_tex_is_spoken_as_math(tmp_path):
    doc = pymupdf.open()
    page = doc.new_page(width=400, height=300)
    page.insert_text((40, 60), "The area is", fontsize=12)
    x = 40 + pymupdf.get_text_length("The area is ", fontsize=12)
    page.insert_text((x, 52), "$x^2$", fontsize=0.5)
    page.insert_text((x + 14, 60), "units.", fontsize=12)
    book = BookText(_save(doc, tmp_path, creator="Study Reader"))
    (u,) = book.utterances((0, 0))
    assert u.text == "The area is x squared units."
    rect = u.word_at(u.text.index("x squared"))[0][0]
    assert rect[3] - rect[1] > 5  # the formula's height, not the tiny source's


def test_symbol_only_sentences_are_dropped(tmp_path):
    doc = pymupdf.open()
    page = doc.new_page(width=400, height=300)
    page.insert_text((40, 60), "As desired.", fontsize=12)
    page.insert_text((40, 120), "* * *", fontsize=12)
    page.insert_text((40, 180), "Next part.", fontsize=12)
    assert _texts(BookText(_save(doc, tmp_path))) == ["As desired.", "Next part."]


def test_start_end_and_finding_sentences(tmp_path):
    doc = pymupdf.open()
    page = doc.new_page(width=400, height=300)
    page.insert_text((40, 60), "One fish. Two fish.", fontsize=12)
    page.insert_text((40, 120), "Red fish. Blue fish.", fontsize=12)
    book = BookText(_save(doc, tmp_path))
    words = book.doc.words(0)
    start = book.start_at(0, 100)
    assert words[start[1]].text == "Red"
    assert _texts(book, start) == ["Red fish.", "Blue fish."]
    two = next(i for i, w in enumerate(words) if w.text == "Two")
    assert _texts(book, (0, 0), (0, two + 1)) == ["One fish.", "Two fish."]
    assert _texts(book, (0, 0), (0, two)) == ["One fish.", "Two"]  # a selection ends where it ends
    u = book.utterance_at(0, two + 1)
    assert u.text == "Two fish." and u.covers(0, two)
    assert book.utterance_before(u).text == "One fish."
    assert book.utterance_before(book.utterance_at(0, 0)) is None


def test_skipped_is_cached(tmp_path):
    doc = pymupdf.open()
    doc.new_page(width=400, height=300).insert_text((40, 60), "Hello there.", fontsize=12)
    book = BookText(_save(doc, tmp_path))
    assert book.skipped(0) is book.skipped(0)
