"""Right-hand calculator panel: typed expressions with a per-book history."""

from __future__ import annotations

import ast
import math
import operator
import re
from decimal import Decimal

from PySide6.QtCore import QEvent, QPoint, QSize, Qt
from PySide6.QtGui import QGuiApplication
from PySide6.QtWidgets import (
    QFrame, QHBoxLayout, QLabel, QLineEdit, QListWidget, QListWidgetItem, QMenu, QToolButton, QToolTip,
    QVBoxLayout, QWidget,
)

from .db import Database
from .theme import icon

# Integers stay below Python's 4300-digit int<->str limit so results can be shown and saved.
MAX_INT_BITS = 14_000
MAX_FACTORIAL = 1_000


class CalcError(Exception):
    pass


def _pow(a, b):
    if isinstance(a, int) and isinstance(b, int) and b > 0 and abs(a) > 1:
        if a.bit_length() * b > MAX_INT_BITS:
            raise CalcError("Result is too large")
    value = a ** b
    if isinstance(value, complex):
        raise CalcError("Result is not a real number")
    return value


def _factorial(n):
    if isinstance(n, float) and n.is_integer():
        n = int(n)
    if not isinstance(n, int) or n < 0:
        raise CalcError("factorial needs a non-negative whole number")
    if n > MAX_FACTORIAL:
        raise CalcError("Result is too large")
    return math.factorial(n)


BINARY = {
    ast.Add: operator.add, ast.Sub: operator.sub, ast.Mult: operator.mul, ast.Div: operator.truediv,
    ast.FloorDiv: operator.floordiv, ast.Mod: operator.mod, ast.Pow: _pow,
}
UNARY = {ast.UAdd: operator.pos, ast.USub: operator.neg}
CONSTANTS = {"pi": math.pi, "e": math.e, "tau": math.tau, "inf": math.inf}
FUNCTIONS = {
    "sqrt": math.sqrt, "cbrt": math.cbrt, "exp": math.exp, "ln": math.log, "log": math.log,
    "log10": math.log10, "log2": math.log2, "abs": abs, "floor": math.floor, "ceil": math.ceil,
    "round": round, "trunc": math.trunc, "factorial": _factorial, "gcd": math.gcd, "lcm": math.lcm,
    "comb": math.comb, "perm": math.perm, "hypot": math.hypot, "min": min, "max": max,
    "sinh": math.sinh, "cosh": math.cosh, "tanh": math.tanh,
    "asinh": math.asinh, "acosh": math.acosh, "atanh": math.atanh,
    "deg": math.degrees, "rad": math.radians,
}
TRIG = {"sin": math.sin, "cos": math.cos, "tan": math.tan}
INVERSE_TRIG = {"asin": math.asin, "acos": math.acos, "atan": math.atan, "atan2": math.atan2}
RESERVED = {*CONSTANTS, *FUNCTIONS, *TRIG, *INVERSE_TRIG, "ans"}
ASSIGNMENT = re.compile(r"^\s*([A-Za-z_]\w*)\s*=(?!=)(.*)$")


class Calculator:
    """Evaluates arithmetic safely (no Python eval) and remembers `ans` and assigned variables."""

    def __init__(self, degrees: bool = False):
        self.degrees = degrees
        self.variables: dict[str, int | float] = {}
        self.ans: int | float | None = None

    def evaluate(self, text: str) -> tuple[str | None, int | float]:
        """Returns (assigned variable name or None, value). Raises CalcError."""
        name, expr = None, text
        if m := ASSIGNMENT.match(text):
            name, expr = m.group(1), m.group(2)
            if name in RESERVED:
                raise CalcError(f"'{name}' is a built-in name")
        value = self.compute(expr)
        if name:
            self.variables[name] = value
        self.ans = value
        return name, value

    def compute(self, expr: str) -> int | float:
        """Value of an expression, without updating `ans` or variables."""
        expr = expr.strip().replace("^", "**").replace("×", "*").replace("÷", "/").replace("−", "-")
        if not expr.strip():
            raise CalcError("Nothing to calculate")
        try:
            tree = ast.parse(expr, mode="eval")
        except SyntaxError:
            raise CalcError("Couldn't read that expression") from None
        try:
            value = self._eval(tree.body)
        except CalcError:
            raise
        except ZeroDivisionError:
            raise CalcError("Division by zero") from None
        except OverflowError:
            raise CalcError("Result is too large") from None
        except (ValueError, TypeError) as e:
            raise CalcError(_friendly(e)) from None
        if isinstance(value, float) and math.isnan(value):
            raise CalcError("Result is not a number")
        if isinstance(value, int) and value.bit_length() > MAX_INT_BITS:
            raise CalcError("Result is too large")
        return value

    def _eval(self, node: ast.AST):
        match node:
            case ast.Constant(value=v) if isinstance(v, (int, float)) and not isinstance(v, bool):
                return v
            case ast.BinOp(op=op) if type(op) in BINARY:
                return BINARY[type(op)](self._eval(node.left), self._eval(node.right))
            case ast.UnaryOp(op=op) if type(op) in UNARY:
                return UNARY[type(op)](self._eval(node.operand))
            case ast.Name(id=name):
                return self._lookup(name)
            case ast.Call(func=ast.Name(id=name), args=args, keywords=[]):
                return self._call(name, [self._eval(a) for a in args])
        raise CalcError("Couldn't read that expression")

    def _lookup(self, name: str):
        if name == "ans":
            if self.ans is None:
                raise CalcError("No previous answer yet")
            return self.ans
        if name in self.variables:
            return self.variables[name]
        if name in CONSTANTS:
            return CONSTANTS[name]
        if name in FUNCTIONS or name in TRIG or name in INVERSE_TRIG:
            raise CalcError(f"'{name}' is a function — try {name}(…)")
        raise CalcError(f"Unknown name '{name}'")

    def _call(self, name: str, args: list):
        if name in TRIG:
            if len(args) != 1:
                raise CalcError(f"{name} takes one value")
            x = math.radians(args[0]) if self.degrees else args[0]
            value = TRIG[name](x)
            return 0.0 if abs(value) < 1e-15 else value  # sin(pi) is 1.2e-16, not a meaningful answer
        if name in INVERSE_TRIG:
            value = INVERSE_TRIG[name](*args)
            return math.degrees(value) if self.degrees else value
        if name in FUNCTIONS:
            return FUNCTIONS[name](*args)
        raise CalcError(f"Unknown function '{name}'")


def _friendly(e: Exception) -> str:
    text = str(e)
    if "math domain error" in text:
        return "Outside the function's domain"
    if "argument" in text or "expected" in text:
        return "Wrong number of values for that function"
    return text[:1].upper() + text[1:]


def format_value(value: int | float) -> str:
    if isinstance(value, int):
        return str(value) if abs(value) < 10**15 else format(Decimal(value), ".10e")
    if math.isinf(value):
        return "∞" if value > 0 else "-∞"
    if value.is_integer() and abs(value) < 1e15:
        return str(int(value))
    return f"{value:.12g}"


def parse_value(text: str) -> int | float:
    """Inverse of `repr` for stored values."""
    try:
        return int(text)
    except ValueError:
        return float(text)


HELP = (
    "Operators: + − * / // % ^ (or **) and parentheses\n"
    "Constants: pi, e, tau, inf · ans = the last result\n"
    "Variables: x = 2.5, then use x\n"
    "Functions: sqrt cbrt exp ln log(x[, base]) log10 log2 abs floor ceil round trunc\n"
    "  sin cos tan asin acos atan atan2 sinh cosh tanh asinh acosh atanh\n"
    "  factorial gcd lcm comb perm hypot min max deg rad"
)


class _Input(QLineEdit):
    """Line edit that walks back through earlier expressions with Up/Down."""

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self.previous: list[str] = []
        self._index = 0
        self._draft = ""

    def reset_history(self, expressions: list[str]) -> None:
        self.previous = expressions
        self._index = len(expressions)

    def keyPressEvent(self, event) -> None:
        key = event.key()
        if key in (Qt.Key.Key_Up, Qt.Key.Key_Down) and self.previous:
            if self._index == len(self.previous):
                self._draft = self.text()
            step = -1 if key == Qt.Key.Key_Up else 1
            self._index = max(0, min(len(self.previous), self._index + step))
            self.setText(self.previous[self._index] if self._index < len(self.previous) else self._draft)
            return
        if key == Qt.Key.Key_Escape and self.text():
            self.clear()
            return
        super().keyPressEvent(event)


class CalculatorPanel(QWidget):
    def __init__(self, db: Database, parent=None):
        super().__init__(parent)
        self.db = db
        self.book_id: int | None = None
        self.calc = Calculator(degrees=db.get_setting("calc_angle") == "deg")

        title = QLabel("Calculator")
        title.setProperty("role", "section")
        self.angle = QToolButton(objectName="angle")
        self.angle.setCheckable(True)
        self.angle.setChecked(self.calc.degrees)
        self.angle.setToolTip("Angle unit for trigonometry: radians or degrees")
        self.angle.toggled.connect(self._set_degrees)
        help_btn = QToolButton()
        help_btn.setIcon(icon("help"))
        help_btn.setIconSize(QSize(17, 17))
        help_btn.setToolTip(HELP)
        help_btn.clicked.connect(lambda: QToolTip.showText(help_btn.mapToGlobal(QPoint(0, help_btn.height())), HELP, help_btn))
        self.clear_btn = QToolButton()
        self.clear_btn.setIcon(icon("trash"))
        self.clear_btn.setIconSize(QSize(17, 17))
        self.clear_btn.setToolTip("Clear this book's calculator history")
        self.clear_btn.clicked.connect(self.clear_history)
        header = QHBoxLayout()
        header.setContentsMargins(4, 0, 0, 0)
        header.addWidget(title)
        header.addStretch()
        header.addWidget(self.angle)
        header.addWidget(help_btn)
        header.addWidget(self.clear_btn)

        self.history = QListWidget(objectName="calcHistory")
        self.history.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.history.setVerticalScrollMode(QListWidget.ScrollMode.ScrollPerPixel)
        self.history.setSelectionMode(QListWidget.SelectionMode.NoSelection)
        self.history.setToolTip("Click to insert the result · double-click to edit the expression")
        self.history.itemClicked.connect(self._insert_result)
        self.history.itemDoubleClicked.connect(self._edit_item)
        self.history.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.history.customContextMenuRequested.connect(self._history_menu)
        self.empty = QLabel("Type an expression below, e.g.\nsqrt(2) * sin(pi/4) + 3^2", alignment=Qt.AlignmentFlag.AlignCenter)
        self.empty.setProperty("role", "hint")

        self.input = _Input(objectName="calcInput")
        self.input.setPlaceholderText("Calculate…")
        self.input.returnPressed.connect(self.submit)
        self.input.textChanged.connect(self._preview)
        self.input.installEventFilter(self)
        self.preview = QLabel()
        self.preview.setProperty("role", "muted")
        self.preview.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        self.box = QFrame(objectName="composer")
        box = QVBoxLayout(self.box)
        box.setContentsMargins(10, 8, 10, 8)
        box.setSpacing(2)
        box.addWidget(self.input)
        box.addWidget(self.preview)
        hint = QLabel("Enter to calculate · ↑ for earlier · x = … to store")
        hint.setProperty("role", "hint")
        hint.setContentsMargins(6, 0, 6, 0)

        self.setObjectName("chatPanel")  # same surface as the chat panel it swaps with
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(8, 8, 8, 8)
        layout.setSpacing(6)
        layout.addLayout(header)
        layout.addWidget(self.history, 1)
        layout.addWidget(self.empty, 1)
        layout.addWidget(self.box)
        layout.addWidget(hint)
        self._sync_angle()
        self.set_book(None)

    # -- book / history --------------------------------------------------------------
    def set_book(self, book_id: int | None) -> None:
        self.book_id = book_id
        self.calc.variables.clear()
        self.calc.ans = None
        self.history.clear()
        self.input.clear()
        rows = self.db.calculations(book_id) if book_id is not None else []
        for row in rows:
            self._restore(row)
            self._add_item(dict(row))
        self.input.reset_history([r["expr"] for r in rows])
        self._sync_empty()
        self.history.scrollToBottom()

    def _restore(self, row) -> None:
        """Rebuild `ans` and variables from saved values, not by re-evaluating (the angle unit may differ now)."""
        value = parse_value(row["value"])
        if m := ASSIGNMENT.match(row["expr"]):
            self.calc.variables[m.group(1)] = value
        self.calc.ans = value

    def clear_history(self) -> None:
        if self.book_id is not None:
            self.db.clear_calculations(self.book_id)
        self.set_book(self.book_id)

    def submit(self) -> None:
        text = self.input.text().strip()
        if not text:
            return
        try:
            _, value = self.calc.evaluate(text)
        except CalcError as e:
            self._show_preview(str(e), error=True)
            return
        entry = {"expr": text, "result": format_value(value), "value": repr(value)}
        if self.book_id is not None:
            entry["id"] = self.db.add_calculation(self.book_id, text, entry["result"], entry["value"])
        self._add_item(entry)
        self.input.reset_history(self.input.previous + [text])
        self.input.clear()
        self._sync_empty()
        self.history.scrollToBottom()

    def _add_item(self, entry: dict) -> None:
        row = QWidget()
        row.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
        lay = QVBoxLayout(row)
        lay.setContentsMargins(8, 6, 8, 6)
        lay.setSpacing(0)
        expr = QLabel(entry["expr"])
        expr.setToolTip(entry["expr"])
        expr.setProperty("role", "muted")
        result = QLabel(f"= {entry['result']}", objectName="calcResult")
        result.setAlignment(Qt.AlignmentFlag.AlignRight)
        lay.addWidget(expr)
        lay.addWidget(result)
        item = QListWidgetItem()
        item.setData(Qt.ItemDataRole.UserRole, entry)
        item.setSizeHint(row.sizeHint())
        self.history.addItem(item)
        self.history.setItemWidget(item, row)

    def _sync_empty(self) -> None:
        has = self.history.count() > 0
        self.history.setVisible(has)
        self.empty.setVisible(not has)
        self.clear_btn.setEnabled(has)

    def _history_menu(self, pos) -> None:
        item = self.history.itemAt(pos)
        if not item:
            return
        entry = item.data(Qt.ItemDataRole.UserRole)
        clipboard = QGuiApplication.clipboard()
        menu = QMenu(self)
        menu.addAction("Copy result").triggered.connect(lambda: clipboard.setText(entry["result"]))
        menu.addAction("Copy expression").triggered.connect(lambda: clipboard.setText(entry["expr"]))
        menu.addAction("Edit expression").triggered.connect(lambda: self._edit_item(item))
        menu.addSeparator()
        menu.addAction("Delete").triggered.connect(lambda: self._delete_item(item))
        menu.exec(self.history.viewport().mapToGlobal(pos))

    def _delete_item(self, item: QListWidgetItem) -> None:
        entry = item.data(Qt.ItemDataRole.UserRole)
        if "id" in entry:
            self.db.delete_calculation(entry["id"])
        self.history.takeItem(self.history.row(item))
        self._sync_empty()

    def _edit_item(self, item: QListWidgetItem) -> None:
        self.input.setText(item.data(Qt.ItemDataRole.UserRole)["expr"])
        self.input.setFocus()

    def _insert_result(self, item: QListWidgetItem) -> None:
        result = item.data(Qt.ItemDataRole.UserRole)["result"]
        self.input.insert(result.replace("∞", "inf"))
        self.input.setFocus()

    # -- input -------------------------------------------------------------------------
    def eventFilter(self, obj, event) -> bool:
        if obj is self.input and event.type() in (QEvent.Type.FocusIn, QEvent.Type.FocusOut):
            self.box.setProperty("focused", event.type() == QEvent.Type.FocusIn)
            self.box.style().polish(self.box)
        return super().eventFilter(obj, event)

    def focus_input(self) -> None:
        self.input.setFocus()
        self.input.selectAll()

    def _preview(self) -> None:
        text = self.input.text()
        m = ASSIGNMENT.match(text)
        expr = m.group(2) if m else text
        if not expr.strip():
            self._show_preview("")
            return
        try:
            self._show_preview(f"= {format_value(self.calc.compute(expr))}")
        except CalcError:
            self._show_preview("")  # still typing; errors show on Enter

    def _show_preview(self, text: str, error: bool = False) -> None:
        self.preview.setText(text)
        self.preview.setProperty("role", "error" if error else "muted")
        self.preview.style().polish(self.preview)
        self.preview.setVisible(bool(text))

    def _set_degrees(self, on: bool) -> None:
        self.calc.degrees = on
        self.db.set_setting("calc_angle", "deg" if on else "rad")
        self._sync_angle()
        self._preview()

    def _sync_angle(self) -> None:
        self.angle.setText("DEG" if self.calc.degrees else "RAD")
