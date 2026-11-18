import json

import pytest

from study_reader import practice
from study_reader import deck_maker as dm

from test_practice import NOW

FLUX = {"type": "self_check", "question": "What is flux?", "answer": "A flow through a surface.", "tags": ["em"]}
WHY = {"type": "truefalse", "question": "Flux is conserved where there are no sources.", "answer": True}
FILL_IN = {"type": "fill_in", "text": "{{c1::Gauss}} related {{c2::flux}} to charge"}


def reply(**data) -> str:
    return "Here you go:\n" + json.dumps(data)


def texts(blocks) -> str:
    return "\n".join(b.get("text", "") for b in blocks if b.get("type") == "text")


# -- questions as Claude writes them ---------------------------------------------------------
def test_question_specs():
    spec = dm.question_spec(FLUX)
    assert spec == {"kind": "self_check", "tags": ["em"],
                    "fields": {"front": "What is flux?", "back": "A flow through a surface.", "extra": ""}}
    assert dm.for_claude(spec) == {"type": "self_check", "question": "What is flux?",
                                   "answer": "A flow through a surface.", "tags": ["em"]}
    both = dm.question_spec({**FLUX, "type": "both_ways"})
    assert both["kind"] == "both_ways" and len(dm.previews(both)) == 2
    fill_in = dm.question_spec({**FILL_IN, "explanation": "Gauss's law"})
    assert fill_in["fields"] == {"text": FILL_IN["text"], "extra": "Gauss's law"} and len(dm.previews(fill_in)) == 2
    short = dm.question_spec({"type": "free", "question": "Derive it.", "answer": "Like so.", "rubric": "steps"})
    assert short["kind"] == "short_answer" and short["fields"]["type"] == "short_answer"
    # The old type names are still read.
    assert dm.question_spec({**FLUX, "type": "flashcard"}) == spec
    assert dm.question_spec({**FLUX, "type": "reversed"})["kind"] == "both_ways"
    assert dm.question_spec({**FILL_IN, "type": "cloze"})["kind"] == "fill_in"
    assert dm.question_spec({"type": "numeric", "question": "1+1?", "answer": 2})["kind"] == "calculation"
    for bad in ({"type": "fill_in", "text": "nothing hidden"}, {"type": "choice", "question": "q"},
                {"type": "image_labels", "question": "q"}, {"type": "occlusion", "question": "q"}, "not a question"):
        with pytest.raises((ValueError, TypeError, KeyError)):
            dm.question_spec(bad)


def test_parse_replies():
    r = dm.parse(reply(deck="Physics::Flux", note="Ask for more on fields.", questions=[
        {"action": "add", "question": FLUX},
        {"action": "add", "question": {"type": "choice", "question": "q"}},  # unusable
        {"id": 3, "action": "convert", "question": WHY},  # Convert's old wording
        {"id": 4, "action": "split", "cards": [FLUX, WHY]},  # the old wording
        {"id": 5, "action": "delete"},
        {"id": 6, "action": "keep", "reason": "fine"},
        {"type": "truefalse", "question": "Flat", "answer": False},  # a question with no wrapper
    ]))
    assert r["deck"] == "Physics::Flux" and r["note"] == "Ask for more on fields."
    actions = [(e["id"], e["action"]) for e in r["entries"]]
    assert actions == [(None, "add"), (None, "error"), (3, "rewrite"), (4, "rewrite"), (5, "remove"), (6, "keep"),
                       (None, "add")]
    assert len(r["entries"][3]["specs"]) == 2 and r["entries"][1]["adding"]
    too_many = dm.parse(reply(questions=[{"id": 1, "action": "split", "cards": [FLUX] * (dm.MAX_SPLIT + 1)}]))
    assert too_many["entries"][0]["action"] == "error"
    old = dm.parse(reply(cards=[{"action": "add", "card": FLUX}]))  # replies from before the rename
    assert [(e["action"], e["specs"][0]["kind"]) for e in old["entries"]] == [("add", "self_check")]
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

    dm.record(db, batch, "s1", reply(deck="Gauss's law", note="Made 3.", questions=[
        {"action": "add", "question": FLUX}, {"action": "add", "question": FILL_IN}, {"action": "add", "question": WHY}]))
    draft = db.draft()
    assert draft["deck"] == "Physics::Gauss's law" and db.draft_batch(batch)["session_id"] == "s1"
    items = db.draft_items(draft_id)
    assert [i["number"] for i in items] == [1, 2, 3] and all(i["status"] == "proposed" for i in items)
    c = dm.counts(db, draft_id)
    assert c["ticked"] == 3 and c["new_questions"] == 4  # the fill-in-the-blank makes two

    # A follow-up sees the draft (with what was unticked) and changes only what it names.
    db.update_draft_item(items[2]["id"], checked=0)
    follow = dm.follow_up_blocks(db, draft, batch, "drop the true/false, harder", adding=True)
    state = json.loads(texts(follow).split("<draft>\n")[1].split("\n</draft>")[0])
    assert state[2] == {"id": 3, "ticked": False, "question": dm.for_claude(dm.question_spec(WHY))}
    assert "<learner_feedback>\ndrop the true/false, harder" in texts(follow)
    harder = {**FLUX, "question": "Why is flux through a closed surface independent of its shape?"}
    dm.record(db, batch, "s1", reply(deck="Gauss", questions=[
        {"id": 3, "action": "remove"}, {"id": 1, "action": "rewrite", "question": harder},
        {"id": 2, "action": "split", "cards": [FILL_IN, WHY]}, {"action": "add", "question": FLUX}]))
    items = db.draft_items(draft_id)
    assert [i["number"] for i in items] == [1, 2, 4, 5]  # #3 removed; the split's second question and the new one
    assert dm.specs(items[0]["proposal"])[0]["fields"]["front"] == harder["question"]
    assert db.draft()["deck"] == "Physics::Gauss"

    db.update_draft_item(items[3]["id"], checked=0)
    result = dm.apply(db, draft_id)
    assert db.draft() is None and result["rewritten"] == []
    deck = db.deck_by_name("Physics::Gauss")
    assert result["deck"] == deck["id"] and len(result["added"]) == 4  # self-check, two blanks, true/false
    questions = [db.question(i) for i in result["added"]]
    assert all(c["deck_id"] == deck["id"] and practice.is_new(c) for c in questions)
    assert db.question_group(questions[0]["group_id"])["tags"].split() == ["em"]
    assert sorted(json.loads(c["data"])["type"] for c in questions) == ["fill_in", "fill_in", "self_check", "truefalse"]


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


# -- improving questions -----------------------------------------------------------------------
def test_improving_questions_keeps_their_schedule(db):
    deck = db.ensure_deck("Physics")
    fill_in = db.add_question_group("fill_in", {"text": "{{c1::Gauss}} found {{c2::flux}} laws", "extra": ""}, deck, tags=["em"])
    first, second = db.group_questions(fill_in)
    practice.attempt(db, first["id"], 3, now=NOW)
    choice = db.add_question_group("choice", {"type": "choice", "question": "Which?", "choices": ["A", "B"], "answer": 0},
                              deck)
    (picked,) = db.group_questions(choice)
    draft_id, (batch,) = dm.start_improve(db, [first["id"], picked["id"]], "prefer numbers")
    draft = db.draft()
    assert draft["deck"] == "Physics" and draft["mode"] == "improve"
    blocks = dm.first_blocks(db, draft, batch)
    assert "prefer numbers" in texts(blocks) and dm.ADD_ALLOWED in texts(blocks)
    payload = json.loads(texts(blocks).split("<questions>\n")[1].split("\n</questions>")[0])
    assert payload[0] == {"id": 1, "deck": "Physics", "tags": ["em"], "type": "fill_in",
                          "text": "{{c1::Gauss}} found {{c2::flux}} laws", "blank": 1}

    q = {"type": "short_answer", "question": "Who found the law of flux, and what does it relate?",
         "answer": "Gauss; flux to enclosed charge.", "rubric": "Gauss; enclosed charge"}
    dm.record(db, batch, "s", reply(questions=[
        {"id": 1, "action": "rewrite", "reason": "conceptual", "question": q},
        {"id": 2, "action": "rewrite", "question": {"type": "self_check", "question": "Which?", "answer": "A"}}]))
    one, two = db.draft_items(draft_id)
    assert one["status"] == "proposed" and two["status"] == "kept" and "stay that way" in two["reason"]

    result = dm.apply(db, draft_id)
    assert result["rewritten"] == [first["id"]] and result["added"] == []
    question = db.question(first["id"])
    assert json.loads(question["data"])["type"] == "short_answer" and question["reps"] == 1 and len(db.attempts(question["id"])) == 1
    assert db.question_group(question["group_id"])["tags"] == " em " and question["deck_id"] == deck
    assert db.question(second["id"])["group_id"] == fill_in  # the other blank stays where it was

    assert dm.undo_rewrite(db, first["id"])
    back = db.question(first["id"])
    assert back["group_id"] == fill_in and back["ord"] == 0 and json.loads(back["data"])["type"] == "fill_in"
    assert db.question_rewrite(first["id"]) is None


def test_undoing_a_rewrite_stored_before_the_rename(db):
    deck = db.ensure_deck("D")
    (question,) = db.group_questions(db.add_question_group("self_check", {"front": "q", "back": "a"}, deck))
    dm.rewrite(db, question["id"], [dm.question_spec(WHY)])
    r = db.question_rewrite(question["id"])
    original = json.loads(r["original"])
    original["note_id"], original["kind"] = original.pop("group_id"), "flashcard"
    db.conn.execute("UPDATE question_rewrites SET original=? WHERE id=?", (json.dumps(original), r["id"]))
    assert dm.undo_rewrite(db, question["id"])
    q = json.loads(db.question(question["id"])["data"])
    assert q["type"] == "self_check" and q["question"] == "q"


def test_splitting_and_adding_while_improving(db):
    deck = db.ensure_deck("D")
    group = db.add_question_group("short_answer", {"type": "short_answer", "question": "What is flux, and why is it conserved?",
                                        "answer": "A flow; no sources.", "rubric": ""}, deck, tags=["gauss"], page=3)
    (question,) = db.group_questions(group)
    practice.attempt(db, question["id"], 3, now=NOW)
    others = [c["id"] for n in range(9) for c in db.group_questions(db.add_question_group(
        "self_check", {"front": f"q{n}", "back": "a"}, deck))]
    draft_id, batches = dm.start_improve(db, [question["id"], *others])
    assert len(batches) == 2  # eight questions a request
    draft = db.draft()
    assert dm.ADD_ELSEWHERE in texts(dm.first_blocks(db, draft, batches[1]))

    dm.record(db, batches[0], "s0", reply(questions=[
        {"id": 1, "action": "split", "reason": "two parts", "cards": [FLUX, FILL_IN]},
        *({"id": n, "action": "keep"} for n in range(2, 8)),  # #8 is left out
        {"action": "add", "question": WHY}]))
    dm.record(db, batches[1], "s1", reply(questions=[{"id": 9, "action": "keep"}, {"id": 10, "action": "keep"},
                                               {"action": "add", "question": FLUX}]))
    items = {i["number"]: i for i in db.draft_items(draft_id)}
    assert items[8]["status"] == "error" and "left this question out" in items[8]["reason"]
    assert items[11]["question_id"] is None and items[11]["batch_id"] == batches[0]
    assert 12 not in items  # the second batch can't add questions
    assert items[1]["status"] == "proposed" and len(dm.specs(items[1]["proposal"])) == 2

    result = dm.apply(db, draft_id)
    kept = db.question(question["id"])
    assert json.loads(kept["data"])["question"] == "What is flux?" and kept["reps"] == 1
    assert db.question_group(kept["group_id"])["tags"].split() == ["gauss", "em"]
    split = [db.question(i) for i in json.loads(db.question_rewrite(question["id"])["original"])["added"]]
    assert [json.loads(c["data"])["type"] for c in split] == ["fill_in", "fill_in"]
    assert all((c["deck_id"], c["page"], practice.is_new(c)) == (deck, 3, True) for c in split)
    (new,) = [db.question(i) for i in result["added"] if db.question(i)["group_id"] not in {c["group_id"] for c in split}]
    assert json.loads(new["data"])["type"] == "truefalse" and new["deck_id"] == deck

    practice.attempt(db, split[0]["id"], 3, now=NOW)  # an attempted question the split made outlives an undo
    assert dm.undo_rewrite(db, question["id"])
    assert json.loads(db.question(question["id"])["data"])["type"] == "short_answer"
    assert db.question(split[0]["id"]) is not None and db.question(split[1]["id"]) is None


def test_follow_ups_on_existing_questions(db):
    deck = db.ensure_deck("D")
    (question,) = db.group_questions(db.add_question_group("self_check", {"front": "Vague", "back": "Too much"}, deck))
    draft_id, (batch,) = dm.start_improve(db, [question["id"]])
    dm.record(db, batch, "s", reply(questions=[{"id": 1, "action": "rewrite", "question": FLUX}]))
    (item,) = db.draft_items(draft_id)
    db.update_draft_item(item["id"], checked=0)
    state = dm.draft_state(db, db.draft_items(draft_id))
    assert state[0]["ticked"] is False and state[0]["now"]["question"] == "Vague"
    assert state[0]["proposed"]["question"] == "What is flux?"
    dm.record(db, batch, "s", reply(questions=[{"id": 1, "action": "keep", "reason": "fine after all"}]))
    (item,) = db.draft_items(draft_id)
    assert (item["status"], item["proposal"], item["checked"]) == ("kept", "", 1)
    dm.record(db, batch, "s", reply(questions=[{"id": 1, "action": "rewrite", "question": WHY}]))
    assert db.draft_items(draft_id)[0]["status"] == "proposed"
    # Deleting the question drops it from the draft.
    db.delete_question(question["id"])
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
    worker.made.emit(batch, "s1", reply(deck="Verbs", questions=[{"action": "add", "question": FLUX}]))
    assert not maker.busy() and changed and len(db.draft_items(db.draft()["id"])) == 1

    assert maker.follow_up("more") == 1
    batch, blocks, fresh, session, _ = worker.sent[-1]
    assert session == "s1" and "<learner_feedback>\nmore" in texts(blocks) and fresh[-1] == blocks[-1]
    worker.makeFailed.emit(batch, "overloaded")
    assert db.draft_batch(batch)["error"] == "overloaded" and len(db.draft_items(db.draft()["id"])) == 1
    assert not maker.can_retry()  # a follow-up that failed is just sent again

    made = []
    maker.questionsChanged.connect(lambda: made.append(1))
    result = maker.apply()
    assert len(result["added"]) == 1 and made and db.draft() is None
    assert db.question(result["added"][0])["deck_id"] == db.deck_by_name("Verbs")["id"]


def test_deck_maker_retries_and_stops(db, maker):
    maker, worker = maker
    deck = db.ensure_deck("D")
    ids = [c["id"] for n in range(2) for c in db.group_questions(db.add_question_group("self_check", {"front": f"q{n}", "back": "a"}, deck))]
    assert maker.start_improve(ids) == 1
    batch = worker.sent[-1][0]
    maker.stop()
    assert worker.stopped and not maker.busy()
    assert {i["status"] for i in db.draft_items(db.draft()["id"])} == {"error"}
    worker.made.emit(batch, "s", reply(questions=[]))  # a reply after stopping is ignored
    assert maker.can_retry() and maker.retry() == 1
    assert {i["status"] for i in db.draft_items(db.draft()["id"])} == {"pending"}
    worker.made.emit(worker.sent[-1][0], "s", reply(questions=[{"id": 1, "action": "rewrite", "question": WHY},
                                                           {"id": 2, "action": "keep"}]))
    maker.tick_all(False)
    assert dm.counts(db, db.draft()["id"]).get("ticked", 0) == 0
    maker.tick_all(True)
    assert maker.apply()["rewritten"] == [ids[0]]
    assert maker.undo_rewrite(ids[0]) and json.loads(db.question(ids[0])["data"])["question"] == "q0"


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
    worker.made.emit(worker.sent[-1][0], "s", reply(deck="Optics", questions=[{"action": "add", "question": FILL_IN}]))
    window.refresh()
    assert window.apply_button.text() == "Add 2 questions" and window.apply_button.isEnabled()
    window._apply()
    assert window.pages.currentIndex() == 3 and "Added 2 questions to Physics::Optics" in window.done_label.text()
    window.improve_form([1, 2])
    assert window.improve_button.text() == "Improve 2 questions"
    window.close()


def test_ticking_a_draft_question_does_not_redraw_the_draft(db, maker, monkeypatch):
    from study_reader.deck_maker_dialog import MakeWindow

    maker, worker = maker
    window = MakeWindow(maker)
    calls = []
    monkeypatch.setattr(window.view, "js", lambda function, *args: calls.append((function, args)))
    window.new_form("")
    window.description.setPlainText("Optics")
    window._generate()
    worker.made.emit(worker.sent[-1][0], "s", reply(questions=[{"action": "add", "question": FLUX},
                                                               {"action": "add", "question": WHY}]))
    window.refresh()
    assert calls[-1][0] == "showDraft"
    first, second = db.draft_items(db.draft()["id"])
    calls.clear()
    maker.tick(first["id"], False)  # what the page's checkbox calls
    window._throttle.stop()
    window.refresh()
    assert [f for f, _ in calls] == ["setTicks"] * len(calls) and calls
    assert calls[-1][1][0] == {str(first["id"]): False, str(second["id"]): True}
    assert window.apply_button.text() == "Add 1 question"
    db.update_draft_item(second["id"], reason="Clearer")  # anything else draws it again
    window.refresh()
    assert calls[-1][0] == "showDraft"
    window.close()


def test_draft_ticks_change_in_place(chat_panel):
    from study_reader.browser import QuestionView
    from test_practice import run_js, wait_ready

    view = QuestionView()
    page = view.page()
    wait_ready(view, page)
    item = {"id": 7, "number": 1, "status": "proposed", "checked": True, "reason": "", "now": None, "kind": "",
            "groups": [{"kind": "self_check", "questions": [{"type": "self_check", "question": "q", "answer": "a"}]}]}
    run_js(page, f"showDraft({json.dumps({'busy': False, 'note': '', 'items': [item]})}); "
                 "document.querySelector('.pair').drawn = 1")
    state = ("JSON.stringify([document.querySelector('.pair').drawn, document.querySelector('[data-tick]').checked, "
             "document.querySelector('.pair').className])")
    assert json.loads(run_js(page, f'setTicks({{"7": false}}); {state}')) == [1, False, "pair off"]  # the same element
    assert json.loads(run_js(page, f'setTicks({{"7": true}}); {state}')) == [1, True, "pair"]
    view.close()


def test_main_window_wiring(tmp_path, monkeypatch):
    monkeypatch.setenv("STUDY_READER_DATA", str(tmp_path / "data"))
    monkeypatch.setenv("QT_QPA_PLATFORM", "offscreen")
    from PySide6.QtWidgets import QApplication
    from study_reader.db import Database
    from study_reader.main_window import MainWindow

    QApplication.instance() or QApplication([])
    db = Database(tmp_path / "t.db")
    deck = db.ensure_deck("Physics")
    (question,) = db.group_questions(db.add_question_group("self_check", {"front": "q", "back": "a"}, deck))
    win = MainWindow(db)
    assert win.practice.make_link
    browser = win.show_browser()
    assert browser.maker is win.deck_maker and browser.improve_action.isVisible()
    sent = []
    monkeypatch.setattr(win.worker, "submit_make", lambda *args: sent.append(args))
    browser.improve([question["id"]])
    window = win.deck_maker.window()
    assert window.pages.currentIndex() == 1
    window._improve()
    win.worker.made.emit(sent[-1][0], "s", reply(questions=[{"id": 1, "action": "rewrite", "question": WHY}]))
    assert win.deck_maker.apply()["rewritten"] == [question["id"]]
    browser.show_question(question["id"])
    assert browser.undo_rewrite_action.isEnabled()
    browser.undo_rewrite()
    assert json.loads(db.question(question["id"])["data"])["question"] == "q"
    window.close()
    win.close()


def test_follow_ups_go_to_the_requests_holding_the_questions_they_name(db, maker, monkeypatch):
    from PySide6.QtWidgets import QMessageBox
    from study_reader import deck_maker_dialog
    from study_reader.deck_maker_dialog import MakeWindow

    assert dm.mentioned("make #4 harder, and #12-14, #20 to #19; #7–8") == {4, 7, 8, 12, 13, 14, 19, 20}
    assert dm.mentioned("more on optics") == set()
    maker, worker = maker
    deck = db.ensure_deck("D")
    ids = [c["id"] for i in range(20)
           for c in db.group_questions(db.add_question_group("self_check", {"front": f"q{i}", "back": "a"}, deck))]
    assert maker.start_improve(ids) == 3
    for batch, *_ in list(worker.sent):
        items = db.draft_items(db.draft()["id"], batch)
        worker.made.emit(batch, f"s{batch}", reply(questions=[{"id": i["number"], "action": "keep"} for i in items]))
    batches = [b["id"] for b in db.draft_batches(db.draft()["id"])]
    holding = {i["number"]: i["batch_id"] for i in db.draft_items(db.draft()["id"])}

    worker.sent.clear()
    assert maker.follow_up("make #12 harder") == 1 and worker.sent[0][0] == holding[12]
    worker.made.emit(holding[12], "s", reply(questions=[]))
    worker.sent.clear()
    assert maker.follow_up(f"#1 and #{len(holding)}") == 2
    assert {s[0] for s in worker.sent} == {holding[1], holding[len(holding)]}
    for s in worker.sent:
        worker.made.emit(s[0], "s", reply(questions=[]))
    assert maker.follow_up_targets("#999 harder") == (batches, False)  # no such question: every request
    assert maker.follow_up_targets("harder") == (batches, False)

    # The window asks before a follow-up naming no questions goes to many requests.
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
