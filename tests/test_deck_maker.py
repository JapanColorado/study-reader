import json

import pytest

from study_reader import cardmodel, srs
from study_reader import deck_maker as dm

from test_srs import NOW

FLUX = {"type": "flashcard", "question": "What is flux?", "answer": "A flow through a surface.", "tags": ["em"]}
WHY = {"type": "truefalse", "question": "Flux is conserved where there are no sources.", "answer": True}
CLOZE = {"type": "cloze", "text": "{{c1::Gauss}} related {{c2::flux}} to charge"}


def reply(**data) -> str:
    return "Here you go:\n" + json.dumps(data)


def texts(blocks) -> str:
    return "\n".join(b.get("text", "") for b in blocks if b.get("type") == "text")


# -- cards as Claude writes them -------------------------------------------------------------
def test_card_specs():
    spec = dm.card_spec(FLUX)
    assert spec == {"kind": "flashcard", "tags": ["em"],
                    "fields": {"front": "What is flux?", "back": "A flow through a surface.", "extra": ""}}
    assert dm.for_claude(spec) == {"type": "flashcard", "question": "What is flux?",
                                   "answer": "A flow through a surface.", "tags": ["em"]}
    both = dm.card_spec({**FLUX, "type": "reversed"})
    assert both["kind"] == "reversed" and len(dm.previews(both)) == 2
    cloze = dm.card_spec({**CLOZE, "explanation": "Gauss's law"})
    assert cloze["fields"] == {"text": CLOZE["text"], "extra": "Gauss's law"} and len(dm.previews(cloze)) == 2
    written = dm.card_spec({"type": "free", "question": "Derive it.", "answer": "Like so.", "rubric": "steps"})
    assert written["kind"] == "written" and written["fields"]["type"] == "written"
    for bad in ({"type": "cloze", "text": "nothing hidden"}, {"type": "choice", "question": "q"},
                {"type": "occlusion", "question": "q"}, "not a card"):
        with pytest.raises((ValueError, TypeError, KeyError)):
            dm.card_spec(bad)


def test_parse_replies():
    r = dm.parse(reply(deck="Physics::Flux", note="Ask for more on fields.", cards=[
        {"action": "add", "card": FLUX},
        {"action": "add", "card": {"type": "choice", "question": "q"}},  # unusable
        {"id": 3, "action": "convert", "question": WHY},  # Convert's old wording
        {"id": 4, "action": "split", "cards": [FLUX, WHY]},
        {"id": 5, "action": "delete"},
        {"id": 6, "action": "keep", "reason": "fine"},
        {"type": "truefalse", "question": "Flat", "answer": False},  # a card with no wrapper
    ]))
    assert r["deck"] == "Physics::Flux" and r["note"] == "Ask for more on fields."
    actions = [(e["id"], e["action"]) for e in r["entries"]]
    assert actions == [(None, "add"), (None, "error"), (3, "rewrite"), (4, "rewrite"), (5, "remove"), (6, "keep"),
                       (None, "add")]
    assert len(r["entries"][3]["specs"]) == 2 and r["entries"][1]["adding"]
    too_many = dm.parse(reply(cards=[{"id": 1, "action": "split", "cards": [FLUX] * (dm.MAX_SPLIT + 1)}]))
    assert too_many["entries"][0]["action"] == "error"
    with pytest.raises(dm.DeckMakerError):
        dm.parse("no json here")


# -- a new deck ------------------------------------------------------------------------------
def test_new_deck_from_a_description(db, tmp_path):
    notes = tmp_path / "notes.md"
    notes.write_text("Gauss's law: flux through a closed surface is charge over epsilon zero.")
    draft_id, batch = dm.start_new(db, "Electrostatics for an exam", "my notes", [notes], parent="Physics")
    draft = db.draft()
    blocks = dm.first_blocks(db, draft, batch)
    assert "Electrostatics for an exam" in texts(blocks) and "<notes>\nmy notes" in texts(blocks)
    assert {"type": "source_files", "paths": [str(notes)]} in blocks
    expanded = dm.expand_files(blocks)
    assert '<file name="notes.md">' in texts(expanded) and not any(b["type"] == "source_files" for b in expanded)

    dm.record(db, batch, "s1", reply(deck="Gauss's law", note="Made 3.", cards=[
        {"action": "add", "card": FLUX}, {"action": "add", "card": CLOZE}, {"action": "add", "card": WHY}]))
    draft = db.draft()
    assert draft["deck"] == "Physics::Gauss's law" and db.draft_batch(batch)["session_id"] == "s1"
    items = db.draft_items(draft_id)
    assert [i["number"] for i in items] == [1, 2, 3] and all(i["status"] == "proposed" for i in items)
    c = dm.counts(db, draft_id)
    assert c["ticked"] == 3 and c["new_cards"] == 4  # the cloze makes two

    # A follow-up sees the draft (with what was unticked) and changes only what it names.
    db.update_draft_item(items[2]["id"], checked=0)
    follow = dm.follow_up_blocks(db, draft, batch, "drop the true/false, harder", adding=True)
    state = json.loads(texts(follow).split("<draft>\n")[1].split("\n</draft>")[0])
    assert state[2] == {"id": 3, "ticked": False, "card": dm.for_claude(dm.card_spec(WHY))}
    assert "<learner_feedback>\ndrop the true/false, harder" in texts(follow)
    harder = {**FLUX, "question": "Why is flux through a closed surface independent of its shape?"}
    dm.record(db, batch, "s1", reply(deck="Gauss", cards=[
        {"id": 3, "action": "remove"}, {"id": 1, "action": "rewrite", "card": harder},
        {"id": 2, "action": "split", "cards": [CLOZE, WHY]}, {"action": "add", "card": FLUX}]))
    items = db.draft_items(draft_id)
    assert [i["number"] for i in items] == [1, 2, 4, 5]  # #3 removed; the split's second card and the new one
    assert dm.specs(items[0]["proposal"])[0]["fields"]["front"] == harder["question"]
    assert db.draft()["deck"] == "Physics::Gauss"

    db.update_draft_item(items[3]["id"], checked=0)
    result = dm.apply(db, draft_id)
    assert db.draft() is None and result["rewritten"] == []
    deck = db.deck_by_name("Physics::Gauss")
    assert result["deck"] == deck["id"] and len(result["added"]) == 4  # flashcard, two cloze cards, true/false
    cards = [db.card(i) for i in result["added"]]
    assert all(c["deck_id"] == deck["id"] and srs.is_new(c) for c in cards)
    assert db.card_note(cards[0]["note_id"])["tags"].split() == ["em"]
    assert sorted(json.loads(c["data"])["type"] for c in cards) == ["cloze", "cloze", "flashcard", "truefalse"]


def test_bad_replies_and_files(db, tmp_path):
    draft_id, batch = dm.start_new(db, "anything")
    dm.record(db, batch, "s", "Sorry, I can't.")
    assert db.draft_batch(batch)["status"] == "error" and "no JSON" in db.draft_batch(batch)["error"]
    with pytest.raises(dm.DeckMakerError, match="gone"):
        dm.expand_files([{"type": "source_files", "paths": [str(tmp_path / "missing.pdf")]}])
    odd = tmp_path / "x.docx"
    odd.write_bytes(b"PK")
    with pytest.raises(dm.DeckMakerError, match="attach PDFs"):
        dm.expand_files([{"type": "source_files", "paths": [str(odd)]}])


def test_reading_a_pdf(pdf_path):
    from pathlib import Path
    text = dm.read_text(Path(pdf_path))
    assert text.startswith("[[page 1]]") and "zorblatt" in text


# -- improving cards ---------------------------------------------------------------------------
def test_improving_cards_keeps_their_schedule(db):
    deck = db.ensure_deck("Physics")
    cloze = db.add_card_note("cloze", {"text": "{{c1::Gauss}} found {{c2::flux}} laws", "extra": ""}, deck, tags=["em"])
    first, second = db.note_cards(cloze)
    srs.review(db, first["id"], 3, now=NOW)
    choice = db.add_card_note("choice", {"type": "choice", "question": "Which?", "choices": ["A", "B"], "answer": 0},
                              deck)
    (picked,) = db.note_cards(choice)
    draft_id, (batch,) = dm.start_improve(db, [first["id"], picked["id"]], "prefer numbers")
    draft = db.draft()
    assert draft["deck"] == "Physics" and draft["mode"] == "improve"
    blocks = dm.first_blocks(db, draft, batch)
    assert "prefer numbers" in texts(blocks) and dm.ADD_ALLOWED in texts(blocks)
    payload = json.loads(texts(blocks).split("<cards>\n")[1].split("\n</cards>")[0])
    assert payload[0] == {"id": 1, "deck": "Physics", "tags": ["em"], "type": "cloze",
                          "text": "{{c1::Gauss}} found {{c2::flux}} laws", "deletion": 1}

    question = {"type": "written", "question": "Who found the law of flux, and what does it relate?",
                "answer": "Gauss; flux to enclosed charge.", "rubric": "Gauss; enclosed charge"}
    dm.record(db, batch, "s", reply(cards=[
        {"id": 1, "action": "rewrite", "reason": "conceptual", "card": question},
        {"id": 2, "action": "rewrite", "card": {"type": "flashcard", "question": "Which?", "answer": "A"}}]))
    one, two = db.draft_items(draft_id)
    assert one["status"] == "proposed" and two["status"] == "kept" and "stay that way" in two["reason"]

    result = dm.apply(db, draft_id)
    assert result["rewritten"] == [first["id"]] and result["added"] == []
    card = db.card(first["id"])
    assert json.loads(card["data"])["type"] == "written" and card["reps"] == 1 and len(db.reviews(card["id"])) == 1
    assert db.card_note(card["note_id"])["tags"] == " em " and card["deck_id"] == deck
    assert db.card(second["id"])["note_id"] == cloze  # the other deletion stays where it was

    assert dm.undo_rewrite(db, first["id"])
    back = db.card(first["id"])
    assert back["note_id"] == cloze and back["ord"] == 0 and json.loads(back["data"])["type"] == "cloze"
    assert db.card_rewrite(first["id"]) is None


def test_splitting_and_adding_while_improving(db):
    deck = db.ensure_deck("D")
    note = db.add_card_note("written", {"type": "written", "question": "What is flux, and why is it conserved?",
                                        "answer": "A flow; no sources.", "rubric": ""}, deck, tags=["gauss"], page=3)
    (card,) = db.note_cards(note)
    srs.review(db, card["id"], 3, now=NOW)
    others = [c["id"] for n in range(9) for c in db.note_cards(db.add_card_note(
        "flashcard", {"front": f"q{n}", "back": "a"}, deck))]
    draft_id, batches = dm.start_improve(db, [card["id"], *others])
    assert len(batches) == 2  # eight cards a request
    draft = db.draft()
    assert dm.ADD_ELSEWHERE in texts(dm.first_blocks(db, draft, batches[1]))

    dm.record(db, batches[0], "s0", reply(cards=[
        {"id": 1, "action": "split", "reason": "two parts", "cards": [FLUX, CLOZE]},
        *({"id": n, "action": "keep"} for n in range(2, 8)),  # #8 is left out
        {"action": "add", "card": WHY}]))
    dm.record(db, batches[1], "s1", reply(cards=[{"id": 9, "action": "keep"}, {"id": 10, "action": "keep"},
                                               {"action": "add", "card": FLUX}]))
    items = {i["number"]: i for i in db.draft_items(draft_id)}
    assert items[8]["status"] == "error" and "left this card out" in items[8]["reason"]
    assert items[11]["card_id"] is None and items[11]["batch_id"] == batches[0]
    assert 12 not in items  # the second batch can't add cards
    assert items[1]["status"] == "proposed" and len(dm.specs(items[1]["proposal"])) == 2

    result = dm.apply(db, draft_id)
    kept = db.card(card["id"])
    assert json.loads(kept["data"])["question"] == "What is flux?" and kept["reps"] == 1
    assert db.card_note(kept["note_id"])["tags"].split() == ["gauss", "em"]
    split = [db.card(i) for i in db.card_rewrite(card["id"]) and json.loads(db.card_rewrite(card["id"])["original"])["added"]]
    assert [json.loads(c["data"])["type"] for c in split] == ["cloze", "cloze"]
    assert all((c["deck_id"], c["page"], srs.is_new(c)) == (deck, 3, True) for c in split)
    (new,) = [db.card(i) for i in result["added"] if db.card(i)["note_id"] not in {c["note_id"] for c in split}]
    assert json.loads(new["data"])["type"] == "truefalse" and new["deck_id"] == deck

    srs.review(db, split[0]["id"], 3, now=NOW)  # a reviewed card the split made outlives an undo
    assert dm.undo_rewrite(db, card["id"])
    assert json.loads(db.card(card["id"])["data"])["type"] == "written"
    assert db.card(split[0]["id"]) is not None and db.card(split[1]["id"]) is None


def test_follow_ups_on_existing_cards(db):
    deck = db.ensure_deck("D")
    (card,) = db.note_cards(db.add_card_note("flashcard", {"front": "Vague", "back": "Too much"}, deck))
    draft_id, (batch,) = dm.start_improve(db, [card["id"]])
    dm.record(db, batch, "s", reply(cards=[{"id": 1, "action": "rewrite", "card": FLUX}]))
    (item,) = db.draft_items(draft_id)
    db.update_draft_item(item["id"], checked=0)
    state = dm.draft_state(db, db.draft_items(draft_id))
    assert state[0]["ticked"] is False and state[0]["now"]["question"] == "Vague"
    assert state[0]["proposed"]["question"] == "What is flux?"
    dm.record(db, batch, "s", reply(cards=[{"id": 1, "action": "keep", "reason": "fine after all"}]))
    (item,) = db.draft_items(draft_id)
    assert (item["status"], item["proposal"], item["checked"]) == ("kept", "", 1)
    dm.record(db, batch, "s", reply(cards=[{"id": 1, "action": "rewrite", "card": WHY}]))
    assert db.draft_items(draft_id)[0]["status"] == "proposed"
    # Deleting the card drops it from the draft.
    db.delete_card(card["id"])
    assert db.draft_items(draft_id) == []


def test_a_new_draft_replaces_the_old(db):
    dm.start_new(db, "one")
    draft_id, batch = dm.start_new(db, "two")
    assert db.draft()["id"] == draft_id and db.conn.execute("SELECT COUNT(*) FROM deck_drafts").fetchone()[0] == 1
    dm.fail(db, batch, "rate limited")
    assert db.draft_batch(batch)["error"] == "rate limited"


# -- in the app ----------------------------------------------------------------------------
@pytest.fixture
def maker(db, monkeypatch):
    monkeypatch.setenv("QT_QPA_PLATFORM", "offscreen")
    from PySide6.QtCore import QObject, Signal
    from PySide6.QtWidgets import QApplication
    from study_reader.deck_maker_dialog import DeckMaker

    QApplication.instance() or QApplication([])

    class Worker(QObject):
        made, makeFailed = Signal(int, str, str), Signal(int, str)

        def __init__(self):
            super().__init__()
            self.sent, self.stopped = [], 0

        def submit_make(self, *args):
            self.sent.append(args)

        def stop_making(self):
            self.stopped += 1

    worker = Worker()
    return DeckMaker(db, worker), worker


def test_deck_maker(db, maker):
    maker, worker = maker
    changed = []
    maker.changed.connect(lambda: changed.append(1))
    maker.start_new("Spanish verbs", model="opus")
    batch, blocks, fresh, session, model = worker.sent[-1]
    assert maker.busy() and session is None and fresh is None and model == "opus"
    worker.made.emit(batch, "s1", reply(deck="Verbs", cards=[{"action": "add", "card": FLUX}]))
    assert not maker.busy() and changed and len(db.draft_items(db.draft()["id"])) == 1

    assert maker.follow_up("more") == 1
    batch, blocks, fresh, session, _ = worker.sent[-1]
    assert session == "s1" and "<learner_feedback>\nmore" in texts(blocks) and fresh[-1] == blocks[-1]
    worker.makeFailed.emit(batch, "overloaded")
    assert db.draft_batch(batch)["error"] == "overloaded" and len(db.draft_items(db.draft()["id"])) == 1
    assert not maker.can_retry()  # a follow-up that failed is just sent again

    made = []
    maker.cardsChanged.connect(lambda: made.append(1))
    result = maker.apply()
    assert len(result["added"]) == 1 and made and db.draft() is None
    assert db.card(result["added"][0])["deck_id"] == db.deck_by_name("Verbs")["id"]


def test_deck_maker_retries_and_stops(db, maker):
    maker, worker = maker
    deck = db.ensure_deck("D")
    ids = [c["id"] for n in range(2) for c in db.note_cards(db.add_card_note("flashcard", {"front": f"q{n}", "back": "a"}, deck))]
    assert maker.start_improve(ids) == 1
    batch = worker.sent[-1][0]
    maker.stop()
    assert worker.stopped and not maker.busy()
    assert {i["status"] for i in db.draft_items(db.draft()["id"])} == {"error"}
    worker.made.emit(batch, "s", reply(cards=[]))  # a reply after stopping is ignored
    assert maker.can_retry() and maker.retry() == 1
    assert {i["status"] for i in db.draft_items(db.draft()["id"])} == {"pending"}
    worker.made.emit(worker.sent[-1][0], "s", reply(cards=[{"id": 1, "action": "rewrite", "card": WHY},
                                                           {"id": 2, "action": "keep"}]))
    maker.tick_all(False)
    assert dm.counts(db, db.draft()["id"]).get("ticked", 0) == 0
    maker.tick_all(True)
    assert maker.apply()["rewritten"] == [ids[0]]
    assert maker.undo_rewrite(ids[0]) and json.loads(db.card(ids[0])["data"])["question"] == "q0"


def test_make_window(db, maker):
    from study_reader.deck_maker_dialog import MakeWindow

    maker, worker = maker
    window = MakeWindow(maker)
    window.new_form("Physics")
    assert window.pages.currentIndex() == 0 and window.parent_deck.currentText() == "Physics"
    window.description.setPlainText("Optics")
    window._generate()
    assert window.pages.currentIndex() == 2 and worker.sent
    request = json.loads(db.draft()["request"])
    assert request["description"] == "Optics" and request["parent"] == "Physics"
    worker.made.emit(worker.sent[-1][0], "s", reply(deck="Optics", cards=[{"action": "add", "card": CLOZE}]))
    window.refresh()
    assert window.apply_button.text() == "Add 2 cards" and window.apply_button.isEnabled()
    window._apply()
    assert window.pages.currentIndex() == 3 and "Added 2 cards to Physics::Optics" in window.done_label.text()
    window.improve_form([1, 2])
    assert window.improve_button.text() == "Improve 2 cards"
    window.close()


def test_main_window_wiring(tmp_path, monkeypatch):
    monkeypatch.setenv("STUDY_READER_DATA", str(tmp_path / "data"))
    monkeypatch.setenv("QT_QPA_PLATFORM", "offscreen")
    from PySide6.QtWidgets import QApplication
    from study_reader.db import Database
    from study_reader.main_window import MainWindow

    QApplication.instance() or QApplication([])
    db = Database(tmp_path / "t.db")
    deck = db.ensure_deck("Physics")
    (card,) = db.note_cards(db.add_card_note("flashcard", {"front": "q", "back": "a"}, deck))
    win = MainWindow(db)
    assert win.review.make_link
    browser = win.show_browser()
    assert browser.maker is win.deck_maker and browser.improve_action.isVisible()
    sent = []
    monkeypatch.setattr(win.worker, "submit_make", lambda *args: sent.append(args))
    browser.improve([card["id"]])
    window = win.deck_maker.window()
    assert window.pages.currentIndex() == 1
    window._improve()
    win.worker.made.emit(sent[-1][0], "s", reply(cards=[{"id": 1, "action": "rewrite", "card": WHY}]))
    assert win.deck_maker.apply()["rewritten"] == [card["id"]]
    browser.show_card(card["id"])
    assert browser.undo_rewrite_action.isEnabled()
    browser.undo_rewrite()
    assert json.loads(db.card(card["id"])["data"])["question"] == "q"
    window.close()
    win.close()


def test_follow_ups_go_to_the_requests_holding_the_cards_they_name(db, maker, monkeypatch):
    from PySide6.QtWidgets import QMessageBox
    from study_reader import deck_maker_dialog
    from study_reader.deck_maker_dialog import MakeWindow

    assert dm.mentioned("make #4 harder, and #12-14, #20 to #19; #7–8") == {4, 7, 8, 12, 13, 14, 19, 20}
    assert dm.mentioned("more on optics") == set()
    maker, worker = maker
    deck = db.ensure_deck("D")
    ids = [c["id"] for i in range(20)
           for c in db.note_cards(db.add_card_note("flashcard", {"front": f"q{i}", "back": "a"}, deck))]
    assert maker.start_improve(ids) == 3
    for batch, *_ in list(worker.sent):
        items = db.draft_items(db.draft()["id"], batch)
        worker.made.emit(batch, f"s{batch}", reply(cards=[{"id": i["number"], "action": "keep"} for i in items]))
    batches = [b["id"] for b in db.draft_batches(db.draft()["id"])]
    holding = {i["number"]: i["batch_id"] for i in db.draft_items(db.draft()["id"])}

    worker.sent.clear()
    assert maker.follow_up("make #12 harder") == 1 and worker.sent[0][0] == holding[12]
    worker.made.emit(holding[12], "s", reply(cards=[]))
    worker.sent.clear()
    assert maker.follow_up(f"#1 and #{len(holding)}") == 2
    assert {s[0] for s in worker.sent} == {holding[1], holding[len(holding)]}
    for s in worker.sent:
        worker.made.emit(s[0], "s", reply(cards=[]))
    assert maker.follow_up_targets("#999 harder") == (batches, False)  # no such card: every request
    assert maker.follow_up_targets("harder") == (batches, False)

    # The window asks before a follow-up naming no cards goes to many requests.
    worker.sent.clear()
    monkeypatch.setattr(deck_maker_dialog, "ASK_OVER", 2)
    monkeypatch.setattr(QMessageBox, "exec", lambda self: 0)  # closed without choosing
    window = MakeWindow(maker)
    window.present()
    window.feedback.setPlainText("harder")
    window._follow_up()
    assert worker.sent == [] and window.feedback.toPlainText() == "harder"
    window.feedback.setPlainText("#3 harder")
    window._follow_up()
    assert len(worker.sent) == 1 and window.feedback.toPlainText() == ""
    window.close()
