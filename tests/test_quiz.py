import asyncio
import json

import pytest

from study_reader import chat_service as cs
from study_reader import quiz
from study_reader.db import Database

from test_core import FakeClaude, book

QUIZ = r'''Here's a quiz.

```quiz Zorblatts
{"questions": [
  {"type": "choice", "question": "How many legs?", "choices": ["Six", "Seven", "$\frac{15}{2}$"], "answer": 1,
   "page": 1, "explanation": "Page 1 says seven."},
  {"type": "truefalse", "question": "Store it near a window?", "answer": false},
  {"type": "numeric", "question": "Rinse time in $\theta$ units?", "answer": "41", "unit": "s"},
  {"type": "free", "question": "Why rinse?", "answer": "To calibrate.", "rubric": "mentions tea"}
]}
```'''


def reply(text=QUIZ):
    return {"id": 2, "role": "assistant", "text": text}


# -- parsing ----------------------------------------------------------------------------
def test_split_replaces_fences_and_forgives_latex_backslashes():
    text, quizzes = quiz.split(QUIZ)
    assert "```quiz\n#0\n```" in text and "questions" not in text and text.startswith("Here's a quiz.")
    (q,) = quizzes
    assert q["title"] == "Zorblatts" and [x["type"] for x in q["questions"]] == ["choice", "truefalse", "numeric", "free"]
    # \frac and \theta would otherwise be a form feed and a tab.
    assert q["questions"][0]["choices"][2] == r"$\frac{15}{2}$" and r"$\theta$" in q["questions"][2]["question"]
    assert q["questions"][0]["page"] == 1 and q["questions"][1]["page"] is None
    assert q["questions"][2]["answer_latex"] == "41"
    # Correctly escaped JSON is left alone.
    assert quiz.loads(r'{"a": "\\frac{1}{2}\n\"x\""}') == {"a": "\\frac{1}{2}\n\"x\""}


def test_unreadable_quiz_keeps_its_source():
    text, (q,) = quiz.split("```quiz Broken\n{\"questions\": [{\"type\": \"essay\", \"question\": \"x\"}]}\n```")
    assert text == "```quiz\n#0\n```" and q["title"] == "Broken" and "essay" in q["error"] and "essay" in q["raw"]
    _, (q,) = quiz.split('```quiz\n[{"type": "free", "question": "Why?", "rubric": ["tea", "static"]}]\n```')
    assert q["questions"][0]["rubric"] == "- tea\n- static"
    _, (q,) = quiz.split("```quiz\nnot json\n```")
    assert "error" in q and q["raw"] == "not json"
    assert quiz.split("```python\nprint(1)\n```") == ("```python\nprint(1)\n```", [])


# -- grading ----------------------------------------------------------------------------
def test_local_grading():
    choice, tf, numeric, free = quiz.quizzes(QUIZ)[0]["questions"]
    assert quiz.grade(choice, {"choice": 1}) == "correct" and quiz.grade(choice, {"choice": 0}) == "incorrect"
    assert quiz.grade(tf, {"value": False}) == "correct" and quiz.grade(tf, {"value": True}) == "incorrect"
    assert quiz.grade(numeric, {"latex": "41"}) == "correct"
    assert quiz.grade(numeric, {"latex": r"\frac{82}{2}"}) == "correct"
    assert quiz.grade(numeric, {"latex": "40.7"}) == "correct"  # within 1%
    assert quiz.grade(numeric, {"latex": "40"}) == "incorrect"
    assert quiz.grade({**numeric, "tolerance": 1.5}, {"latex": "40"}) == "correct"
    root = {"type": "numeric", "answer": "sqrt(2)/2"}
    assert quiz.grade(root, {"latex": r"\frac{\sqrt{2}}{2}"}) == "correct"
    assert quiz.grade(root, {"latex": "0.7071"}) == "correct"
    with pytest.raises(quiz.AnswerError):
        quiz.grade(numeric, {"latex": r"\frac{1}{"})
    with pytest.raises(quiz.AnswerError):
        quiz.grade(numeric, {"latex": ""})


def test_grade_reply_parsing():
    assert quiz.parse_grade('{"verdict": "partial", "feedback": "Close: $\\alpha$."}') == ("partial", r"Close: $\alpha$.")
    assert quiz.parse_grade('Sure!\n```json\n{"verdict": "Correct", "feedback": "Yes."}\n```') == ("correct", "Yes.")
    assert quiz.parse_grade("Looks right to me.") == (None, "Looks right to me.")
    prompt = quiz.grading_prompt(quiz.quizzes(QUIZ)[0], 3, "Because tea.")
    assert "question 4" in prompt and "To calibrate." in prompt and "mentions tea" in prompt and "Because tea." in prompt


def test_section_around():
    toc = [(1, "One", 0), (2, "1.1", 0), (3, "Sub\0\0", 1), (2, "1.2", 4), (2, "1.3", 8), (1, "Two", 10)]
    assert quiz.scopes(toc, 2, 20) == [(1, 0, 9, "One"), (2, 0, 3, "1.1"), (3, 1, 3, "Sub")]
    assert quiz.section_around(toc, 2, 20) == (0, 3, "1.1")
    assert quiz.section_around(toc, 2, 20, level=3) == (1, 3, "Sub")
    assert quiz.section_around(toc, 2, 20, level=1) == (0, 9, "One")
    assert quiz.section_around(toc, 2, 20, level=quiz.PAGE_LEVEL) == (2, 2, None)
    assert quiz.section_around(toc, 2, 20, level=quiz.DOCUMENT_LEVEL) == (0, 19, None)
    assert quiz.section_around(toc, 2, 200, level=quiz.DOCUMENT_LEVEL) == (0, 9, "One")  # too long
    assert quiz.request(0, 19, None, whole=True) == ("Quiz me on the whole document (pages 1–20)", "Quiz: whole document")
    assert quiz.section_around(toc, 9, 20) == (8, 9, "1.3")
    assert quiz.section_around(toc, 15, 20) == (10, 19, "Two")  # no level 2 here: the nearest above
    assert quiz.section_around([], 0, 20) == (0, 2, None)
    assert quiz.section_around([], 19, 20) == (17, 19, None)
    assert quiz.request(0, 3, "1.1") == ("Quiz me on the section “1.1” (pages 1–4)", "Quiz: 1.1")
    assert quiz.request(4, 4, None) == ("Quiz me on page 5", "Quiz: page 5")


def test_answers_context_describes_answers():
    rows = [
        {"quiz": 0, "question": 0, "response": '{"choice": 2}', "status": "done", "verdict": "incorrect"},
        {"quiz": 0, "question": 3, "response": '{"text": "tea"}', "status": "pending", "verdict": None},
    ]
    ctx = quiz.answers_context([{"id": 1, "role": "user", "text": "Quiz me"}, reply()], {2: rows})
    assert "<quiz_answers>" in ctx and 'Quiz "Zorblatts"' in ctx
    assert r"Q1: I chose C ($\frac{15}{2}$) (incorrect)" in ctx and "Q2: not answered yet" in ctx
    assert "tea" in ctx and "(being graded)" in ctx
    assert quiz.answers_context([reply()], {}) == ""


# -- storage ----------------------------------------------------------------------------
def test_quiz_answers_are_replaced_and_go_with_their_message(tmp_path):
    db = Database(tmp_path / "t.db")
    chat = db.create_chat(db.upsert_book("h", "/a.pdf", "A", 3)["id"], "Quiz")
    db.add_message(chat, "user", "Quiz me")
    msg = db.add_message(chat, "assistant", QUIZ)
    first = db.save_quiz_answer(msg, 0, 0, '{"choice": 0}', "done", "incorrect")
    second = db.save_quiz_answer(msg, 0, 0, '{"choice": 1}', "done", "correct")
    db.save_quiz_answer(msg, 0, 1, '{"value": false}', "done", "correct")
    assert second != first and db.quiz_answer(first) is None
    assert [(r["question"], r["verdict"]) for r in db.quiz_answers(chat)[msg]] == [(0, "correct"), (1, "correct")]
    db.clear_quiz_answers(msg, 0)
    assert db.quiz_answers(chat) == {}
    db.save_quiz_answer(msg, 0, 0, '{"choice": 1}', "done", "correct")
    db.delete_messages_from(chat, msg)
    assert db.quiz_answers(chat) == {}


# -- grading through Claude ---------------------------------------------------------------
def test_grading_forks_from_the_quiz_reply(monkeypatch, tmp_path, pdf_path):
    fake = FakeClaude(monkeypatch)
    fake.sessions |= {"base", "chat"}
    fake.uuids.add("quiz-reply")
    events = []
    svc = cs.ChatService(lambda *a: events.append(a), tmp_path)
    job = cs.Job(chat_id=1, book=book(pdf_path, "base", "full"), chat_session_id="chat", question="Grade it",
                 resume_at="quiz-reply", history=[("user", "Quiz me"), ("assistant", "quiz")], detached=True)
    asyncio.run(svc.grade(job, 9))
    assert fake.calls == [{"text": "Grade it", "resume": "chat", "fork": True, "resume_at": "quiz-reply"}]
    assert events == [("graded", 9, "answer")]

    # Without a branch point it still forks rather than continuing the chat's session.
    asyncio.run(svc.grade(cs.Job(chat_id=1, book=book(pdf_path, "base", "full"), chat_session_id="chat",
                                 question="Grade it", detached=True), 10))
    assert fake.calls[-1]["resume"] == "chat" and fake.calls[-1]["fork"] is True

    # A pruned chat session: fork from the book and replay the chat up to the quiz.
    fake.sessions.discard("chat")
    asyncio.run(svc.grade(job, 11))
    assert fake.calls[-1]["resume"] == "base" and fake.calls[-1]["fork"] is True
    assert "Earlier in this conversation" in fake.calls[-1]["text"] and events[-1] == ("graded", 11, "answer")


# -- the chat panel -----------------------------------------------------------------------
def test_answering_a_quiz_in_the_chat_panel(chat_panel):
    panel, worker, db, book_id = chat_panel
    panel.start_quiz("Quiz me on page 2.", "Quiz: page 2", 1)
    panel.send()
    chat = panel.chat_id
    (job,) = worker.jobs
    assert job.question == "Quiz me on page 2." and job.page == 1 and job.context == ""
    assert db.chat(chat)["title"] == "Quiz: page 2"
    worker.done.emit(chat, "s1", QUIZ, "u1")
    msg = db.messages(chat)[1]["id"]

    panel._on_quiz_answer(msg, 0, 0, json.dumps({"choice": 2}))
    panel._on_quiz_answer(msg, 0, 2, json.dumps({"latex": r"\frac{1}{"}))  # unreadable: not saved
    panel._on_quiz_answer(msg, 0, 3, json.dumps({"text": "Because tea."}))
    rows = {r["question"]: r for r in db.quiz_answers(chat)[msg]}
    assert rows[0]["verdict"] == "incorrect" and 2 not in rows and rows[3]["status"] == "pending"

    grading, answer_id = worker.gradings[-1]
    assert grading.detached and (grading.chat_session_id, grading.resume_at) == ("s1", "u1")
    assert "Because tea." in grading.question and [r for r, _ in grading.history] == ["user", "assistant"]
    worker.graded.emit(answer_id, '{"verdict": "partial", "feedback": "Say why tea."}')
    row = db.quiz_answer(answer_id)
    assert (row["status"], row["verdict"], row["feedback"]) == ("done", "partial", "Say why tea.")

    # A failed grading can be retried.
    panel._on_quiz_answer(msg, 0, 3, json.dumps({"text": "Tea!"}))
    _, answer_id = worker.gradings[-1]
    worker.gradeFailed.emit(answer_id, "offline")
    assert db.quiz_answer(answer_id)["status"] == "error"
    panel._on_quiz_regrade(msg, 0, 3)
    assert worker.gradings[-1][1] == answer_id and db.quiz_answer(answer_id)["status"] == "pending"

    # Follow-ups tell Claude what was answered.
    panel.input.setPlainText("Why was I wrong about Q1?")
    panel.send()
    assert "Q1: I chose C" in worker.jobs[-1].context
    worker.done.emit(chat, "s2", "Because.", "u2")

    # Retaking clears the answers, and a grading still running for them is dropped.
    panel._on_quiz_retake(msg, 0)
    assert db.quiz_answers(chat) == {}
    worker.graded.emit(answer_id, '{"verdict": "correct", "feedback": "late"}')
    assert db.quiz_answers(chat) == {}


def test_quiz_on_a_selected_passage(chat_panel):
    panel, worker, db, book_id = chat_panel
    snippet = {"kind": "text", "page": 2, "text": "Never store a zorblatt\nnear a window.", "rects": [], "png": None}
    question, title = quiz.snippet_request(snippet)
    panel.start_quiz(question, title, snippet["page"], snippet)
    panel.send()
    (job,) = worker.jobs
    assert job.question == "Quiz me on this passage" and [s.text for s in job.snippets] == [snippet["text"]]
    assert db.chat(panel.chat_id)["title"] == "Quiz: Never store a zorblatt near a window."
    assert db.messages(panel.chat_id)[0]["snippets"][0]["page"] == 2


def test_drafted_request_keeps_the_scope():
    req = "Quiz me on page 5"
    assert quiz.drafted_request(req, "  ") == req
    assert quiz.drafted_request(req, "Quiz me on page 5, harder") == "Quiz me on page 5, harder"
    assert quiz.drafted_request(req, "Quiz me on  page\n5 harder") == "Quiz me on  page\n5 harder"
    assert quiz.drafted_request(req, "only derivations") == "Quiz me on page 5\n\nonly derivations"


def test_quiz_me_drafts_in_the_composer_until_sent(chat_panel):
    from PySide6.QtGui import QTextCursor

    panel, worker, db, book_id = chat_panel
    panel.input.setPlainText("half a question")
    panel.start_quiz("Quiz me on the section “6.4 Energy” (pages 12–15)", "Quiz: 6.4 Energy", 11, last=14)
    # Nothing is sent or saved yet: the request waits in the composer, with a chip, cursor at its end.
    assert worker.jobs == [] and db.chats(book_id) == [] and panel.chat_id is None
    assert panel.input.toPlainText() == "Quiz me on the section “6.4 Energy” (pages 12–15)"
    assert panel.input.textCursor().atEnd() and panel.chips.count() == 1
    assert panel._quiz["label"] == "6.4 Energy · pages 12–15"

    panel.input.moveCursor(QTextCursor.MoveOperation.End)
    panel.input.insertPlainText(", only derivations")
    panel.send()
    (job,) = worker.jobs
    assert job.question == "Quiz me on the section “6.4 Energy” (pages 12–15), only derivations" and job.page == 11
    chat = db.chat(panel.chat_id)
    assert chat["title"] == "Quiz: 6.4 Energy" and panel.chat_id not in panel._untitled
    (m,) = db.messages(panel.chat_id)
    assert (m["page_from"], m["page_to"]) == (11, 14)
    # The composer gets back what it held, and the next send is an ordinary question.
    assert panel._quiz is None and panel.input.toPlainText() == "half a question" and panel.chips.count() == 0


def test_quiz_me_with_the_request_deleted_or_empty(chat_panel):
    panel, worker, db, book_id = chat_panel
    panel.start_quiz("Quiz me on page 2", "Quiz: page 2", 1)
    panel.input.setPlainText("harder please")
    panel.send()
    assert worker.jobs[-1].question == "Quiz me on page 2\n\nharder please"
    panel.start_quiz("Quiz me on page 3", "Quiz: page 3", 2)
    panel.input.clear()
    panel.send()
    assert worker.jobs[-1].question == "Quiz me on page 3" and db.chat(panel.chat_id)["title"] == "Quiz: page 3"


def test_removing_the_quiz_chip_cancels_it(chat_panel):
    from PySide6.QtWidgets import QToolButton

    panel, worker, db, book_id = chat_panel
    snippet = {"kind": "text", "page": 2, "text": "Never store a zorblatt.", "rects": [], "png": None}
    marks = []
    panel.highlightsChanged.connect(lambda: marks.append(len(panel.pending_snippets())))
    panel.start_quiz(*quiz.snippet_request(snippet)[:2], snippet["page"], snippet)
    assert marks == [1] and panel._quiz["label"] == "p. 3 · Never store a zorblatt."
    chip = panel.chips.itemAt(0).widget()
    chip.findChildren(QToolButton)[0].click()
    assert panel._quiz is None and marks == [1, 0] and panel.input.toPlainText() == "" and panel.chips.count() == 0
    panel.input.setPlainText("What is a zorblatt?")
    panel.send()
    assert worker.jobs[-1].question == "What is a zorblatt?" and worker.jobs[-1].snippets == []
    assert db.chat(panel.chat_id)["title"] == "What is a zorblatt?"  # an ordinary chat, named later

    # Leaving for another chat (or a new one) cancels a drafted quiz too, leaving no empty chat.
    chats = len(db.chats(book_id))
    panel.start_quiz("Quiz me on page 1", "Quiz: page 1", 0)
    panel.new_chat()
    assert panel._quiz is None and panel.input.toPlainText() == "" and len(db.chats(book_id)) == chats


def test_quiz_me_on_a_reply_drafts_in_the_same_chat(chat_panel):
    panel, worker, db, book_id = chat_panel
    panel.input.setPlainText("Explain friction.")
    panel.send()
    chat = panel.chat_id
    worker.done.emit(chat, "s1", "Friction resists sliding.", "u1")
    reply = db.messages(chat)[1]["id"]
    panel.quiz_on_reply(reply)
    assert panel.chat_id == chat and len(worker.jobs) == 1 and len(db.messages(chat)) == 2
    assert panel.input.toPlainText() == "Quiz me on your last reply" and panel._quiz["label"] == "Claude's last reply"
    panel.input.insertPlainText(" with three questions")
    panel.send()
    job = worker.jobs[-1]
    assert job.chat_id == chat and job.question == "Quiz me on your last reply with three questions"
    assert [m["role"] for m in db.messages(chat)] == ["user", "assistant", "user"]
    assert db.chat(chat)["title"] == "Explain friction."


def test_quiz_me_in_the_window_only_drafts(pdf_path, tmp_path, monkeypatch):
    monkeypatch.setenv("QT_QPA_PLATFORM", "offscreen")
    monkeypatch.setenv("STUDY_READER_DATA", str(tmp_path / "data"))
    from PySide6.QtWidgets import QApplication
    from study_reader.main_window import MainWindow

    QApplication.instance() or QApplication([])
    db = Database(tmp_path / "t.db")
    win = MainWindow(db)
    win.show()
    win.open_book(pdf_path)
    win.quiz_action.trigger()
    assert win.chat.isVisible() and win.chat._quiz is not None and db.chats(win.book_id) == []
    assert win.chat.input.toPlainText().startswith("Quiz me on ") and win.focusWidget() is win.chat.input
    win.close()
