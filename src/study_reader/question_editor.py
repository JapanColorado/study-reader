"""Editing questions: the question editor (fields by type, tags) the question browser shows under
its table, and the Add dialog for making new questions."""

from __future__ import annotations

import hashlib
import json
import shutil
from pathlib import Path

from PySide6.QtCore import QMimeData, QStringListModel, Qt, Signal
from PySide6.QtGui import QKeySequence, QShortcut, QTextCursor
from PySide6.QtWidgets import (
    QCheckBox, QComboBox, QCompleter, QDialog, QDoubleSpinBox, QFileDialog, QFormLayout, QHBoxLayout, QLabel, QLineEdit,
    QPlainTextEdit, QPushButton, QSpinBox, QStackedWidget, QToolButton, QVBoxLayout, QWidget,
)

from . import question_model, quiz, variants
from .db import Database, media_dir

# Types a question can be switched between in the editor (their text carries over); a
# fill-in-the-blank or an image labels question stays what it is.
SWITCHABLE = ("self_check", "both_ways", *question_model.QUESTION_TYPES)
# The editor's tolerance box at this value: the calculation gives none (1% of the answer counts).
NO_TOLERANCE = -1.0
# Types the Add dialog makes (image labels come only from Anki).
ADD_TYPES = tuple(t for t in question_model.TYPES if t != "image_labels")


class FieldEdit(QPlainTextEdit):
    """A Markdown field. Pasted or dropped images are saved to the media folder and linked."""

    def __init__(self, placeholder: str = "", parent=None):
        super().__init__(parent)
        self.setObjectName("fieldEdit")
        self.setPlaceholderText(placeholder)
        self.setTabChangesFocus(True)
        self.setMinimumHeight(54)

    def canInsertFromMimeData(self, source: QMimeData) -> bool:
        return source.hasImage() or super().canInsertFromMimeData(source)

    def insertFromMimeData(self, source: QMimeData) -> None:
        if source.hasUrls() and all(u.isLocalFile() and Path(u.toLocalFile()).suffix.lower() in IMAGE_SUFFIXES
                                    for u in source.urls()):
            for url in source.urls():
                self.insertPlainText(f"![]({save_media_file(Path(url.toLocalFile()))})")
            return
        if source.hasImage():
            self.insertPlainText(f"![]({save_media_image(source.imageData())})")
            return
        super().insertFromMimeData(source)

    def wrap(self, before: str, after: str) -> None:
        """Put markup around the selection (or the cursor)."""
        cursor = self.textCursor()
        text = cursor.selectedText().replace(" ", "\n")
        cursor.insertText(before + text + after)
        if not text:
            cursor.movePosition(QTextCursor.MoveOperation.Left, n=len(after))
            self.setTextCursor(cursor)


IMAGE_SUFFIXES = {".png", ".jpg", ".jpeg", ".gif", ".webp", ".svg"}


def save_media_file(path: Path) -> str:
    """Copy an image into the media folder (named by content, so copies aren't duplicated)."""
    data = path.read_bytes()
    name = f"{path.stem[:40]}-{hashlib.sha1(data).hexdigest()[:10]}{path.suffix.lower()}"
    target = media_dir() / name
    if not target.exists():
        shutil.copyfile(path, target)
    return name


def save_media_image(image) -> str:
    from PySide6.QtCore import QBuffer, QByteArray, QIODevice
    data = QByteArray()
    buf = QBuffer(data)
    buf.open(QIODevice.OpenModeFlag.WriteOnly)
    image.save(buf, "PNG")
    raw = bytes(data)
    name = f"paste-{hashlib.sha1(raw).hexdigest()[:16]}.png"
    target = media_dir() / name
    if not target.exists():
        target.write_bytes(raw)
    return name


class QuestionEditor(QWidget):
    """A question's fields, by its type, plus its tags (what it shares with its sibling questions, see
    question_model). edited: the user changed something (the browser saves after a pause). In the
    browser it edits an existing question's fields (group_id); the Add dialog uses it for new ones
    (switch_type, then values())."""

    edited = Signal()

    def __init__(self, db: Database, parent=None):
        super().__init__(parent)
        self.db = db
        self.group_id: int | None = None
        self.kind = "self_check"  # the question type shown
        self._extra: dict = {}  # fields the editor doesn't show (image labels' image and shapes)
        self._varies: bool | None = None  # a question's "varies" (variants.py); None: not marked
        self._skipped = False  # Claude found nothing to vary in the loaded question (variants.SKIP)
        self._loading = False
        self._known_tags: list[str] | None = None  # for completion, read when first needed after a load

        self.type_label = QLabel()  # a type that can't be switched (fill-in-the-blank, image labels)
        self.type_label.setProperty("role", "muted")
        self.type_box = QComboBox()
        for t in SWITCHABLE:
            self.type_box.addItem(question_model.TYPE_LABELS[t], t)
        self.type_box.setToolTip("The question's type: what is typed so far carries over")
        self.type_box.activated.connect(lambda: self.switch_type(self.type_box.currentData()))
        toolbar = QHBoxLayout()
        toolbar.setContentsMargins(0, 0, 0, 0)
        toolbar.addWidget(self.type_label)
        toolbar.addWidget(self.type_box)
        toolbar.addStretch()
        self.tools = []
        def shortcut(key: str, slot) -> None:
            QShortcut(QKeySequence(key), self, slot, context=Qt.ShortcutContext.WidgetWithChildrenShortcut)

        for text, tip, key, slot in (
            ("B", "Bold (Ctrl+B)", "Ctrl+B", lambda: self._wrap("**", "**")),
            ("I", "Italic (Ctrl+I)", "Ctrl+I", lambda: self._wrap("*", "*")),
            ("$x$", "Math (Ctrl+M)", "Ctrl+M", lambda: self._wrap("$", "$")),
            ("[…]", "Blank (Ctrl+Shift+C; Ctrl+Alt+Shift+C keeps the same number)", "Ctrl+Shift+C",
             lambda: self.fill_in(False)),
            ("Image…", "Insert an image (or paste one)", None, self._insert_image),
        ):
            b = QToolButton(text=text, toolTip=tip)
            b.clicked.connect(slot)
            toolbar.addWidget(b)
            self.tools.append(b)
            if key:
                shortcut(key, slot)
        self.fill_in_button = self.tools[3]
        shortcut("Ctrl+Alt+Shift+C", lambda: self.fill_in(True))

        # Self-checks, fill-in-the-blank and image labels: one box per field.
        self.text_page = QWidget()
        self.text_form = QFormLayout(self.text_page)
        self.text_form.setContentsMargins(0, 0, 0, 0)
        self.fields: dict[str, FieldEdit] = {}
        self.field_labels: dict[str, QLabel] = {}
        for key in ("front", "back", "text", "header", "extra"):
            edit = FieldEdit(_PLACEHOLDERS.get(key, ""))
            edit.textChanged.connect(self._changed)
            label = QLabel(question_model.FIELD_LABELS[key])
            self.fields[key], self.field_labels[key] = edit, label
            self.text_form.addRow(label, edit)
        self.image_labels_info = QLabel()
        self.image_labels_info.setProperty("role", "muted")
        self.image_labels_info.setWordWrap(True)
        self.text_form.addRow(self.image_labels_info)

        # Questions that are answered (choice, true/false, calculation, short answer).
        self.quiz_page = QWidget()
        form = QFormLayout(self.quiz_page)
        form.setContentsMargins(0, 0, 0, 0)
        self.q_question = FieldEdit("The question (Markdown, $math$)")
        self.q_choices = FieldEdit("One choice per line")
        self.q_correct = QSpinBox(minimum=1, maximum=10)
        self.q_tf = QComboBox()
        self.q_tf.addItems(["True", "False"])
        self.q_answer = FieldEdit("The answer (Markdown)")
        self.q_number = QLineEdit(placeholderText="e.g. 0.25, 3/8 or sqrt(2)/2")
        self.q_unit = QLineEdit(placeholderText="optional, e.g. m/s")
        # -1 (its lowest) is no tolerance given, 1% of the answer; 0 is an exact answer.
        self.q_tolerance = QDoubleSpinBox(decimals=6, minimum=NO_TOLERANCE, maximum=1e12, specialValueText="1% of the answer",
                                          toolTip="How far off an answer may be and still count; 0 for exact")
        self.q_rubric = FieldEdit("The key points a full answer covers, shown when you rate it (optional)")
        self.q_explanation = FieldEdit("Why the answer is right (shown after answering)")
        self.q_varies = QCheckBox("New numbers after each attempt")  # text and tooltip: _question_rows
        self.q_varies.clicked.connect(self._varies_clicked)
        self.q_rows: dict[str, tuple] = {}
        for key, label, w in (("question", "Question", self.q_question),
                              ("choices", "Choices", self.q_choices), ("correct", "Correct choice", self.q_correct),
                              ("tf", "Answer", self.q_tf), ("answer", "Answer", self.q_answer),
                              ("number", "Answer", self.q_number), ("unit", "Unit", self.q_unit),
                              ("tolerance", "Tolerance", self.q_tolerance), ("rubric", "Rubric", self.q_rubric),
                              ("explanation", "Explanation", self.q_explanation), ("varies", "", self.q_varies)):
            lab = QLabel(label)
            form.addRow(lab, w)
            self.q_rows[key] = (lab, w)
        for w in (self.q_question, self.q_choices, self.q_answer, self.q_rubric, self.q_explanation):
            w.textChanged.connect(self._changed)
        for w in (self.q_number, self.q_unit):
            w.textChanged.connect(self._changed)
        self.q_correct.valueChanged.connect(self._changed)
        self.q_tf.currentIndexChanged.connect(self._changed)
        self.q_tolerance.valueChanged.connect(self._tolerance_changed)

        self.pages = QStackedWidget()
        self.pages.addWidget(self.text_page)
        self.pages.addWidget(self.quiz_page)

        self.tags = QLineEdit(objectName="tagsInput", placeholderText="Tags, separated by spaces")
        self.tags.textEdited.connect(self._changed)
        self._tag_completer = QCompleter(QStringListModel(self), self)
        self._tag_completer.setCaseSensitivity(Qt.CaseSensitivity.CaseInsensitive)
        self._tag_completer.activated.connect(self._complete_tag)
        self._tag_completer.setWidget(self.tags)
        self.tags.textEdited.connect(self._suggest_tags)
        tags_row = QHBoxLayout()
        tags_row.addWidget(QLabel("Tags"))
        tags_row.addWidget(self.tags, 1)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(6)
        layout.addLayout(toolbar)
        layout.addWidget(self.pages, 1)
        layout.addLayout(tags_row)
        self.set_kind("self_check")

    # -- loading -------------------------------------------------------------------------
    def load(self, group_id: int | None) -> None:
        """Show a question's fields, by their question_groups id (None: nothing)."""
        group = self.db.question_group(group_id) if group_id is not None else None
        self.group_id = group["id"] if group else None
        self._known_tags = None
        self._skipped = group is not None and any(
            c["variant"] and json.loads(c["variant"]) == variants.SKIP for c in self.db.group_questions(group["id"]))
        self.setEnabled(group is not None)
        if group is None:
            self._loading = True
            self.set_kind("self_check")
            self.clear()
            self._loading = False
            return
        self.set_fields(group["kind"], json.loads(group["fields"]), group["tags"].split())

    def set_fields(self, kind: str, fields: dict, tags=()) -> None:
        self._loading = True
        try:
            self.set_kind(kind)
            self.clear()  # the other types' boxes too, which switch_type fills from
            self._extra = {k: v for k, v in fields.items() if k in ("image", "shapes")}
            if kind in question_model.QUESTION_TYPES:
                self._load_quiz(fields)
            else:
                for key, edit in self.fields.items():
                    edit.setPlainText(str(fields.get(key, "")))
                if kind == "image_labels":
                    shapes = fields.get("shapes", [])
                    self.image_labels_info.setText(
                        f"Image {fields.get('image', '')} with {len(shapes)} shape(s) in "
                        f"{len(question_model.question_ords(kind, fields))} question(s). Masks can't be edited here.")
            self.tags.setText(" ".join(tags))
        finally:
            self._loading = False

    def set_kind(self, kind: str) -> None:
        """Show the fields of a question type (as they are: switch_type carries text over)."""
        self.kind = kind
        is_question = kind in question_model.QUESTION_TYPES
        self.pages.setCurrentWidget(self.quiz_page if is_question else self.text_page)
        shown = question_model.TEXT_FIELDS.get(kind, ())
        for key in self.fields:
            self.fields[key].setVisible(key in shown)
            self.field_labels[key].setVisible(key in shown)
        self.image_labels_info.setVisible(kind == "image_labels")
        self.type_label.setText(question_model.TYPE_LABELS.get(kind, kind))
        self.type_label.setVisible(kind not in SWITCHABLE)
        self.type_box.setVisible(kind in SWITCHABLE)
        self.type_box.setCurrentIndex(max(self.type_box.findData(kind), 0))
        self.fill_in_button.setVisible(kind == "fill_in")
        if is_question:
            self._question_rows()

    def switch_type(self, kind: str) -> None:
        """Make the question another type, carrying over what is typed: a self-check's front, back and
        extra are a question's question, answer and explanation, and the answer goes from one
        question type to another as far as it can (so the question can be saved as it is; it can't
        when the new answer is missing, and the browser would show the old type again)."""
        old = self.kind
        if kind == old:
            return
        if not self._loading:
            self._carry(old, kind)
        self.set_kind(kind)
        self._changed()

    def clear(self) -> None:
        self._known_tags = None
        for edit in self.fields.values():
            edit.clear()
        for w in (self.q_question, self.q_choices, self.q_answer, self.q_rubric, self.q_explanation, self.q_number,
                  self.q_unit):
            w.clear()
        self.q_tolerance.setValue(NO_TOLERANCE)
        self.q_correct.setValue(1)
        self._extra = {}
        self._varies = None
        self.q_varies.setChecked(True)

    def _load_quiz(self, q: dict) -> None:
        self._varies = q.get("varies")
        self.q_question.setPlainText(str(q.get("question", "")))
        self.q_explanation.setPlainText(str(q.get("explanation", "")))
        self.q_choices.setPlainText("\n".join(str(c) for c in q.get("choices", [])))
        answer = q.get("answer")
        self.q_correct.setValue(answer + 1 if q.get("type") == "choice" and isinstance(answer, int) else 1)
        self.q_tf.setCurrentIndex(0 if answer is True or q.get("type") != "truefalse" else 1)
        self.q_answer.setPlainText(str(answer) if q.get("type") == "short_answer" and answer is not None else "")
        self.q_number.setText(str(answer) if q.get("type") == "calculation" else "")
        self.q_unit.setText(str(q.get("unit", "")))
        tolerance = q.get("tolerance")
        self.q_tolerance.setValue(float(tolerance) if isinstance(tolerance, (int, float)) and tolerance >= 0 else NO_TOLERANCE)
        self.q_rubric.setPlainText(str(q.get("rubric", "")))
        self.q_varies.setChecked(self._varies is not False and not self._skipped)

    def _question_rows(self) -> None:
        t = self.kind
        rows = {"question", "explanation"} | {
            "choice": {"choices", "correct", "varies"}, "truefalse": {"tf"}, "calculation": {"number", "unit", "tolerance", "varies"},
            "short_answer": {"answer", "rubric", "varies"},
        }[t]
        for key, (label, w) in self.q_rows.items():
            label.setVisible(key in rows)
            w.setVisible(key in rows)
        self.q_varies.setText("New choices after each attempt" if t == "choice" else "New numbers after each attempt")
        self.q_varies.setToolTip(
            ("After each attempt Claude rewords the right answer and writes new wrong ones for the next time "
             "this comes up; the question stays as it is." if t == "choice" else
             "After each attempt Claude writes a fresh version of this problem (new givens, perhaps a new "
             "setting) for the next time it comes up.") + " Settings → Practice settings turns this off for every question.")
        self.q_rows["answer"][0].setText("Model answer" if t == "short_answer" else "Answer")

    def _carry(self, old: str, new: str) -> None:
        """Fill the new type's empty fields from the old type's (switch_type)."""
        self_checks = ("self_check", "both_ways")
        if old in self_checks and new in self_checks or old not in SWITCHABLE or new not in SWITCHABLE:
            return
        moves = ((self.fields["front"], self.q_question), (self.fields["extra"], self.q_explanation))
        if new in self_checks:
            for target, source in moves:
                if not target.toPlainText().strip():
                    target.setPlainText(source.toPlainText())
            if not self.fields["back"].toPlainText().strip():
                self.fields["back"].setPlainText(self._answer_text(old))
            return
        if old in self_checks:
            for source, target in moves:
                if not target.toPlainText().strip():
                    target.setPlainText(source.toPlainText())
        self._fill_answer(new, old, self._answer_text(old))

    def _answer_text(self, old: str) -> str:
        """The answer as typed for type `old`, as text."""
        choices = [c.strip() for c in self.q_choices.toPlainText().splitlines() if c.strip()]
        if old in ("self_check", "both_ways"):
            return self.fields["back"].toPlainText().strip()
        if old == "choice":
            i = self.q_correct.value() - 1
            answer = choices[i] if 0 <= i < len(choices) else ""
        elif old == "truefalse":
            answer = self.q_tf.currentText()
        elif old == "calculation":
            answer = " ".join(p for p in (self.q_number.text().strip(), self.q_unit.text().strip()) if p)
        else:
            answer = self.q_answer.toPlainText().strip()
        return answer

    def _fill_answer(self, new: str, old: str, answer: str) -> None:
        """Put an answer (text) in question type `new`'s answer field if that is empty."""
        choices = [c.strip() for c in self.q_choices.toPlainText().splitlines() if c.strip()]
        if new == "short_answer" and not self.q_answer.toPlainText().strip():
            self.q_answer.setPlainText(answer)
        elif new == "calculation" and not self.q_number.text().strip():
            try:
                quiz.expected_value(answer)
            except ValueError:
                answer = ""
            self.q_number.setText(answer)
        elif new == "truefalse" and answer.lower() in ("true", "false"):
            self.q_tf.setCurrentIndex(0 if answer.lower() == "true" else 1)
        elif new == "choice" and not choices:
            if old == "truefalse":
                self.q_choices.setPlainText("True\nFalse")
                self.q_correct.setValue(self.q_tf.currentIndex() + 1)
            elif answer:
                self.q_choices.setPlainText(answer + "\n")  # the right one; the others are to add
                self.q_correct.setValue(1)

    def _tolerance_changed(self, value: float) -> None:
        if NO_TOLERANCE < value < 0:  # typed between "none given" and exact: none given
            self.q_tolerance.setValue(NO_TOLERANCE)
            return
        self._changed()

    def _varies_clicked(self, on: bool) -> None:
        self._varies = on
        self._changed()

    # -- draft -----------------------------------------------------------------------------
    def draft(self) -> dict:
        """Everything as entered, saved or not, for restore()."""
        return {
            "group_id": self.group_id, "kind": self.kind, "extra": dict(self._extra), "varies": self._varies,
            "fields": {k: e.toPlainText() for k, e in self.fields.items()}, "tags": self.tags.text(),
            "question": self.q_question.toPlainText(),
            "choices": self.q_choices.toPlainText(), "correct": self.q_correct.value(), "tf": self.q_tf.currentIndex(),
            "answer": self.q_answer.toPlainText(), "number": self.q_number.text(), "unit": self.q_unit.text(),
            "tolerance": self.q_tolerance.value(), "rubric": self.q_rubric.toPlainText(),
            "explanation": self.q_explanation.toPlainText(), "varies_box": self.q_varies.isChecked(),
        }

    def restore(self, d: dict) -> None:
        """Show a draft() of the loaded question again."""
        self._loading = True
        try:
            self.set_kind(d["kind"])
            self._extra, self._varies = dict(d["extra"]), d["varies"]
            for key, edit in self.fields.items():
                edit.setPlainText(d["fields"].get(key, ""))
            self.tags.setText(d["tags"])
            for w, key in ((self.q_question, "question"), (self.q_choices, "choices"), (self.q_answer, "answer"),
                           (self.q_rubric, "rubric"), (self.q_explanation, "explanation")):
                w.setPlainText(d[key])
            self.q_number.setText(d["number"])
            self.q_unit.setText(d["unit"])
            self.q_correct.setValue(d["correct"])
            self.q_tf.setCurrentIndex(d["tf"])
            self.q_tolerance.setValue(d["tolerance"])
            self.q_varies.setChecked(d["varies_box"])
        finally:
            self._loading = False

    # -- values --------------------------------------------------------------------------
    def values(self) -> tuple[str, dict, list[str]]:
        """(type, fields, tags) as entered. Raises question_model.QuestionError for a question that can't be saved."""
        tags = question_model.split_tags(self.tags.text())
        if self.kind in question_model.QUESTION_TYPES:
            return self.kind, self._quiz_fields(), tags
        fields = {k: self.fields[k].toPlainText().strip() for k in question_model.TEXT_FIELDS[self.kind]}
        fields.update(self._extra if self.kind == "image_labels" else {})
        question_model.check(self.kind, fields)
        return self.kind, fields, tags

    def _quiz_fields(self) -> dict:
        t = self.kind
        raw = {"type": t, "question": self.q_question.toPlainText().strip(),
               "explanation": self.q_explanation.toPlainText().strip()}
        if t == "choice":
            raw["choices"] = [c.strip() for c in self.q_choices.toPlainText().splitlines() if c.strip()]
            raw["answer"] = self.q_correct.value() - 1
        elif t == "truefalse":
            raw["answer"] = self.q_tf.currentIndex() == 0
        elif t == "calculation":
            text = self.q_number.text().strip()
            try:
                raw["answer"] = int(text) if text.lstrip("-").isdigit() else float(text)
            except ValueError:
                raw["answer"] = text
            raw["unit"] = self.q_unit.text().strip()
            if self.q_tolerance.value() >= 0:
                raw["tolerance"] = self.q_tolerance.value()
        else:
            raw["answer"] = self.q_answer.toPlainText().strip()
            raw["rubric"] = self.q_rubric.toPlainText().strip()
        if t in ("calculation", "short_answer", "choice") and self._varies is not None:
            raw["varies"] = self._varies
        try:
            q = quiz.validate_question(raw)
        except (ValueError, TypeError, KeyError) as e:
            raise question_model.QuestionError(_sentence(str(e))) from None
        # Typed here, like a self-check's fields, so it may hold HTML and pasted images (q.html in quiz.js).
        q["html"] = True
        return q

    def save(self) -> bool:
        """Store the edits to the loaded question (and its siblings). Raises question_model.QuestionError if they
        can't be saved."""
        if self.group_id is None:
            return False
        if self.db.question_group(self.group_id) is None:
            raise question_model.QuestionError("this question was deleted meanwhile")
        kind, fields, tags = self.values()
        self.db.update_question_group(self.group_id, fields, tags, kind=kind)
        return True

    # -- editing helpers -----------------------------------------------------------------
    def _changed(self, *_) -> None:
        if not self._loading:
            self.edited.emit()

    def _first_field(self) -> FieldEdit | None:
        return next((f for f in (*self.fields.values(), self.q_question) if f.isVisible()), None)

    def _focused_field(self) -> FieldEdit | None:
        w = self.focusWidget()
        if isinstance(w, FieldEdit) and w.isVisible():
            return w
        return self._first_field()

    def focus_first(self) -> None:
        if (f := self._first_field()) is not None:
            f.setFocus()

    def _wrap(self, before: str, after: str) -> None:
        if (f := self._focused_field()) is not None:
            f.wrap(before, after)
            f.setFocus()

    def fill_in(self, same_number: bool) -> None:
        """Wrap the selection in the next blank (or the last one again)."""
        if self.kind != "fill_in":
            return
        text = self.fields["text"].toPlainText()
        n = question_model.renumber_next(text)
        if same_number and n > 1:
            n -= 1
        f = self.fields["text"]
        f.wrap(f"{{{{c{n}::", "}}")
        f.setFocus()

    def _insert_image(self) -> None:
        path, _ = QFileDialog.getOpenFileName(self, "Insert image", "", "Images (*.png *.jpg *.jpeg *.gif *.webp *.svg)")
        if path and (f := self._focused_field()) is not None:
            f.insertPlainText(f"![]({save_media_file(Path(path))})")

    def _suggest_tags(self, text: str) -> None:
        word = text.rsplit(" ", 1)[-1]
        if not word:
            self._tag_completer.popup().hide()
            return
        if self._known_tags is None:
            self._known_tags = self.db.all_tags()
            self._tag_completer.model().setStringList(self._known_tags)
        self._tag_completer.setCompletionPrefix(word)
        if self._tag_completer.completionCount():
            self._tag_completer.complete()

    def _complete_tag(self, tag: str) -> None:
        head = self.tags.text().rsplit(" ", 1)
        self.tags.setText((head[0] + " " if len(head) > 1 else "") + tag + " ")
        self._changed()


_PLACEHOLDERS = {
    "front": "Front (Markdown, $math$; paste images)",
    "back": "Back",
    "text": "Text with blanks: {{c1::answer}} or {{c1::answer::hint}} — select text and press Ctrl+Shift+C",
    "header": "Shown above the image",
    "extra": "Extra: shown with the answer (optional)",
}


def _sentence(text: str) -> str:
    text = text.strip()
    return (text[:1].upper() + text[1:]).rstrip(".") + "." if text else text


class DeckBox(QComboBox):
    """An editable deck chooser: existing decks, or type a new 'Parent::Child' name."""

    def __init__(self, db: Database, parent=None):
        super().__init__(parent)
        self.db = db
        self.setEditable(True)
        self.setInsertPolicy(QComboBox.InsertPolicy.NoInsert)
        self.setMinimumWidth(260)
        self.completer().setCompletionMode(QCompleter.CompletionMode.PopupCompletion)
        self.completer().setFilterMode(Qt.MatchFlag.MatchContains)
        self.reload()

    def reload(self, current: str | None = None) -> None:
        current = current if current is not None else self.currentText()
        blocked = self.blockSignals(True)  # the text is the same afterwards; filling the list isn't a choice of its first deck
        self.clear()
        self.addItems([d["name"] for d in self.db.decks()])
        self.setCurrentText(current)
        self.blockSignals(blocked)

    def deck_name(self) -> str:
        from .db import deck_name
        return deck_name(self.currentText())


class AddDialog(QDialog):
    """Make new questions: pick a type and a deck, fill in the fields, Add (Ctrl+Enter)."""

    added = Signal(int)  # question_groups id

    def __init__(self, db: Database, parent=None):
        super().__init__(parent)
        self.db = db
        self.setWindowTitle("Add questions")
        self.setWindowFlag(Qt.WindowType.Window)
        self.resize(640, 620)
        self.type = QComboBox()
        for t in ADD_TYPES:
            self.type.addItem(question_model.TYPE_LABELS[t], t)
        self.deck = DeckBox(db)
        self.editor = QuestionEditor(db)
        self.type.currentIndexChanged.connect(lambda: self._set_type(self.type.currentData()))
        self.status = QLabel()
        self.status.setProperty("role", "muted")
        add = QPushButton("Add", default=True)
        add.setProperty("primary", True)
        add.setToolTip("Add (Ctrl+Enter)")
        add.clicked.connect(self.add)
        close = QPushButton("Close")
        close.clicked.connect(self.close)
        QShortcut(QKeySequence("Ctrl+Return"), self, self.add)
        top = QHBoxLayout()
        top.addWidget(QLabel("Type"))
        top.addWidget(self.type)
        top.addSpacing(12)
        top.addWidget(QLabel("Deck"))
        top.addWidget(self.deck, 1)
        buttons = QHBoxLayout()
        buttons.addWidget(self.status, 1)
        buttons.addWidget(close)
        buttons.addWidget(add)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(16, 14, 16, 14)
        layout.addLayout(top)
        layout.addWidget(self.editor, 1)
        layout.addLayout(buttons)
        self.type.setCurrentIndex(max(self.type.findData(db.get_setting("add_type")), 0))
        self._set_type(self.type.currentData())
        self.deck.setCurrentText(db.get_setting("add_deck", ""))

    def _set_type(self, kind: str) -> None:
        self.editor.switch_type(kind)
        self.editor.type_label.hide()  # the dialog's own Type box says it
        self.editor.type_box.hide()
        self.editor.setEnabled(True)

    def set_deck(self, name: str) -> None:
        if name:
            self.deck.setCurrentText(name)

    def add(self) -> None:
        try:
            kind, fields, tags = self.editor.values()
        except question_model.QuestionError as e:
            self.status.setText(str(e))
            return
        name = self.deck.deck_name()
        if not name:
            self.status.setText("Choose a deck (type a new name to make one).")
            return
        group_id = self.db.add_question_group(kind, fields, self.db.ensure_deck(name), tags)
        self.db.set_setting("add_type", kind)
        self.db.set_setting("add_deck", name)
        n = len(self.db.group_questions(group_id))
        self.status.setText(f"Added {n} question{'s' * (n != 1)} to {name}.")
        tags_text = self.editor.tags.text()
        self.editor.clear()
        self.editor.tags.setText(tags_text)
        self.deck.reload(name)
        self.editor.focus_first()
        self.added.emit(group_id)
