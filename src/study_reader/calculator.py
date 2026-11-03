"""Right-hand calculator panel: Desmos-style math input with a per-book history."""

from __future__ import annotations

import ast
import json
import math
import operator
import re
from decimal import Decimal
from fractions import Fraction
from pathlib import Path

from PySide6.QtCore import QObject, QPoint, QSize, Qt, QUrl, Signal, Slot
from PySide6.QtGui import QColor, QCursor, QGuiApplication
from PySide6.QtWebChannel import QWebChannel
from PySide6.QtWebEngineWidgets import QWebEngineView
from PySide6.QtWidgets import QHBoxLayout, QLabel, QMenu, QToolButton, QToolTip, QVBoxLayout, QWidget

from .calc_latex import LatexError, expr_to_latex, fraction_latex, fraction_of, latex_to_expr, number_latex
from .db import Database
from .theme import icon, theme

ASSETS = Path(__file__).parent / "assets"

# Integers stay below Python's 4300-digit int<->str limit so results can be shown and saved.
MAX_INT_BITS = 14_000
MAX_FACTORIAL = 1_000


class CalcError(Exception):
    pass


def _exact(value):
    """Fractions that are whole numbers become ints, so int-only functions (gcd, comb) accept them."""
    if isinstance(value, Fraction) and value.denominator == 1:
        return int(value)
    return value


def _bits(value) -> int:
    if isinstance(value, Fraction):
        return max(value.numerator.bit_length(), value.denominator.bit_length())
    return value.bit_length() if isinstance(value, int) else 0


def _div(a, b):
    """Exact for ints and fractions: 1/3 stays one third."""
    if isinstance(a, (int, Fraction)) and isinstance(b, (int, Fraction)):
        return Fraction(a) / Fraction(b)
    return a / b


def _pow(a, b):
    if isinstance(a, (int, Fraction)) and isinstance(b, int):
        if abs(b) > 1 and abs(a) not in (0, 1) and _bits(a) * abs(b) > MAX_INT_BITS:
            raise CalcError("Result is too large")
        return Fraction(a) ** b  # exact, negative powers included
    value = a ** b
    if isinstance(value, complex):
        raise CalcError("Result is not a real number")
    return value


def _factorial(n):
    if isinstance(n, (float, Fraction)) and n == int(n):
        n = int(n)
    if not isinstance(n, int) or n < 0:
        raise CalcError("factorial needs a non-negative whole number")
    if n > MAX_FACTORIAL:
        raise CalcError("Result is too large")
    return math.factorial(n)


BINARY = {
    ast.Add: operator.add, ast.Sub: operator.sub, ast.Mult: operator.mul, ast.Div: _div,
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


Number = int | Fraction | float


class Calculator:
    """Evaluates arithmetic safely (no Python eval) and remembers `ans` and assigned variables.

    Arithmetic on whole numbers and decimals is exact (`Fraction`s), as in 0.1 + 0.2 = 3/10;
    functions like sqrt and sin, and the constants, give floats."""

    def __init__(self, degrees: bool = False):
        self.degrees = degrees
        self.variables: dict[str, Number] = {}
        self.ans: Number | None = None

    def evaluate(self, text: str) -> tuple[str | None, Number]:
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

    def compute(self, expr: str) -> Number:
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
        if _bits(value) > MAX_INT_BITS:
            raise CalcError("Result is too large")
        return value

    def _eval(self, node: ast.AST):
        return _exact(self._eval_node(node))

    def _eval_node(self, node: ast.AST):
        match node:
            case ast.Constant(value=float(v)) if math.isfinite(v):
                return Fraction(repr(v))  # the decimal as written: 0.1 is exactly 1/10
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
        if len(name) > 1 and all(c in self.variables or c in CONSTANTS for c in name):
            return math.prod(self._lookup(c) for c in name)  # xy is x times y, as written in math
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


def format_value(value: Number) -> str:
    if isinstance(value, Fraction):
        if value.denominator == 1:
            value = value.numerator
        else:
            try:
                value = float(value)
            except OverflowError:
                return format(Decimal(value.numerator) / Decimal(value.denominator), ".10e")
    if isinstance(value, int):
        return str(value) if abs(value) < 10**15 else format(Decimal(value), ".10e")
    if math.isinf(value):
        return "∞" if value > 0 else "-∞"
    if value.is_integer() and abs(value) < 1e15:
        return str(int(value))
    return f"{value:.12g}"


def store_value(value: Number) -> str:
    if isinstance(value, Fraction):
        return f"{value.numerator}/{value.denominator}"
    return repr(value)


def parse_value(text: str) -> Number:
    """Inverse of `store_value`."""
    if "/" in text:
        return Fraction(text)
    try:
        return int(text)
    except ValueError:
        return float(text)


HELP = (
    "Type / for a fraction, ^ for a power, sqrt, cbrt or nthroot for a root,\n"
    "  abs, floor and ceil for brackets; → steps out of them. 2x means 2 × x\n"
    "Operators: + − * / % (mod) and ! (factorial)\n"
    "Constants: pi, e, tau, inf · ans = the last result\n"
    "Variables: x = 2.5, then use x\n"
    "Functions: sqrt cbrt exp ln log(x[, base]) log10 log2 abs floor ceil round trunc\n"
    "  sin cos tan asin acos atan atan2 sinh cosh tanh asinh acosh atanh\n"
    "  factorial gcd lcm comb perm hypot min max deg rad\n"
    "Whole numbers and decimals stay exact: click a result's ½ button to see it as a fraction"
)


class _Bridge(QObject):
    """What calculator.html calls through the web channel."""

    def __init__(self, panel: CalculatorPanel):
        super().__init__(panel)
        self.panel = panel

    @Slot(str)
    def changed(self, latex: str) -> None:
        self.panel.preview(latex)

    @Slot(str)
    def submit(self, latex: str) -> None:
        self.panel.submit(latex)

    @Slot(int, bool)
    def menu(self, key: int, fraction: bool) -> None:
        self.panel.history_menu(key, fraction)


class CalculatorPanel(QWidget):
    """Header and hint in Qt; history and the math-field input in calculator.html. There are two:
    the open book's, and the Review page's (shared=True), which shows the history of the book of
    the card under review and, for a card with no book, a shared one (book_id None). closable: a
    button in the header asks to hide it (closeRequested)."""

    historyChanged = Signal(object)  # the book id (None: the shared history) whose saved history changed
    closeRequested = Signal()

    def __init__(self, db: Database, shared: bool = False, closable: bool = False, parent=None):
        super().__init__(parent)
        self.db = db
        self.shared = shared
        self.book_id: int | None = None
        self.calc = Calculator(degrees=db.get_setting("calc_angle") == "deg")
        self.entries: list[dict] = []  # {key, id?, expr, latex, result, value}, oldest first
        self._next_key = 0
        self.preview_text = ""
        self.preview_error = False

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
        if closable:
            close_btn = QToolButton()
            close_btn.setIcon(icon("x"))
            close_btn.setIconSize(QSize(17, 17))
            close_btn.setToolTip("Hide the calculator (F11)")
            close_btn.clicked.connect(self.closeRequested)
            header.addWidget(close_btn)

        self.view = QWebEngineView()
        self.view.page().setBackgroundColor(QColor(theme().colors["surface"]))
        self.channel = QWebChannel(self.view.page())
        self.channel.registerObject("calc", _Bridge(self))
        self.view.page().setWebChannel(self.channel)
        self._ready = False
        self._pending: list[str] = []
        self.view.loadFinished.connect(self._on_loaded)
        self.view.load(QUrl.fromLocalFile(str(ASSETS / "calculator.html")))
        hint = QLabel("Enter to calculate · / for a fraction · ↑ for earlier · x = … to store")
        hint.setProperty("role", "hint")
        hint.setWordWrap(True)  # unwrapped, it holds the panel wider than its slot and the right edge is cut off
        hint.setContentsMargins(6, 0, 6, 0)

        self.setObjectName("chatPanel")  # same surface as the chat panel it swaps with
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(8, 8, 8, 8)
        layout.setSpacing(6)
        layout.addLayout(header)
        layout.addWidget(self.view, 1)
        layout.addWidget(hint)
        theme().changed.connect(self._apply_theme)
        self._apply_theme()
        self._sync_angle()
        self.set_book(None)

    # -- page ----------------------------------------------------------------------------
    def _js(self, function: str, *args) -> None:
        js = f"{function}({', '.join(json.dumps(a) for a in args)})"
        if self._ready:
            self.view.page().runJavaScript(js)
        else:
            self._pending.append(js)

    def _on_loaded(self, ok: bool) -> None:
        self._ready = True
        for js in self._pending:
            self.view.page().runJavaScript(js)
        self._pending.clear()

    def _apply_theme(self) -> None:
        self.view.page().setBackgroundColor(QColor(theme().colors["surface"]))
        self._js("setTheme", theme().colors)

    def _page_entry(self, entry: dict) -> dict:
        value = parse_value(entry["value"])
        fraction = fraction_of(value)
        return {
            "key": entry["key"], "latex": entry["latex"] or expr_to_latex(entry["expr"]),
            "result": number_latex(entry["result"]), "fraction": fraction_latex(fraction) if fraction else None,
            # Labels for the decimal/fraction switch: a preview of the form it switches to
            "decimal_label": f"{float(fraction):.1f}".replace("-0.0", "0.0") if fraction else None,
            "fraction_label": fraction_latex(fraction)
            if fraction and abs(fraction.numerator) < 10 and fraction.denominator < 10 else "\\frac{a}{b}",
        }

    # -- book / history --------------------------------------------------------------
    @property
    def saves(self) -> bool:
        """Whether calculations are kept: with a book, or in the shared history."""
        return self.book_id is not None or self.shared

    def set_book(self, book_id: int | None) -> None:
        """Show a book's history (None: none, or for the Review page's, the shared one); the input clears."""
        self.book_id = book_id
        self.clear_btn.setToolTip("Clear this book's calculator history" if book_id is not None
                                  else "Clear the calculator history of cards with no book")
        self._load()
        self._set_input("")

    def reload(self) -> None:
        """The history was changed by the other calculator: show it again, keeping the input."""
        self._load()
        self._js("refreshPreview")

    def _load(self) -> None:
        self.calc.variables.clear()
        self.calc.ans = None
        self.entries = []
        rows = self.db.calculations(self.book_id) if self.saves else []
        for row in rows:
            self._restore(row)
            self.entries.append(self._keyed(dict(row)))
        self._js("setEntries", [self._page_entry(e) for e in self.entries])
        self._sync_empty()

    def _keyed(self, entry: dict) -> dict:
        entry["key"] = self._next_key
        self._next_key += 1
        return entry

    def _restore(self, row) -> None:
        """Rebuild `ans` and variables from saved values, not by re-evaluating (the angle unit may differ now)."""
        value = parse_value(row["value"])
        if m := ASSIGNMENT.match(row["expr"]):
            self.calc.variables[m.group(1)] = value
        self.calc.ans = value

    def clear_history(self) -> None:
        if self.saves:
            self.db.clear_calculations(self.book_id)
        self.set_book(self.book_id)
        if self.saves:
            self.historyChanged.emit(self.book_id)

    def submit(self, latex: str) -> bool:
        """Evaluates the math field's LaTeX; on success it joins the history and the field clears."""
        if not latex.strip():
            return False
        if self.book_id is not None and self.db.book(self.book_id) is None:
            self.book_id = None  # removed from the library meanwhile: its history went with it
            self._load()
        try:
            expr = latex_to_expr(latex)
            _, value = self.calc.evaluate(expr)
        except (LatexError, CalcError) as e:
            self._show_preview(error=str(e))
            return False
        entry = {"expr": expr, "latex": latex, "result": format_value(value), "value": store_value(value)}
        if self.saves:
            entry["id"] = self.db.add_calculation(
                self.book_id, expr, entry["result"], entry["value"], latex,
            )
        self.entries.append(self._keyed(entry))
        self._js("addEntry", self._page_entry(entry))
        self._set_input("")
        self._sync_empty()
        if self.saves:
            self.historyChanged.emit(self.book_id)
        return True

    def _sync_empty(self) -> None:
        self.clear_btn.setEnabled(bool(self.entries))

    def _entry(self, key: int) -> dict | None:
        return next((e for e in self.entries if e["key"] == key), None)

    def history_menu(self, key: int, fraction_shown: bool) -> None:
        entry = self._entry(key)
        if not entry:
            return
        page = self._page_entry(entry)
        fraction = fraction_of(parse_value(entry["value"]))
        clipboard = QGuiApplication.clipboard()
        menu = QMenu(self)
        menu.addAction("Copy result").triggered.connect(lambda: clipboard.setText(entry["result"]))
        if fraction:
            menu.addAction("Copy as fraction").triggered.connect(lambda: clipboard.setText(str(fraction)))
        menu.addAction("Copy expression").triggered.connect(lambda: clipboard.setText(entry["expr"]))
        menu.addAction("Copy LaTeX").triggered.connect(lambda: clipboard.setText(page["latex"]))
        menu.addAction("Edit expression").triggered.connect(lambda: self._set_input(page["latex"], focus=True))
        menu.addSeparator()
        menu.addAction("Delete").triggered.connect(lambda: self._delete(entry))
        menu.exec(QCursor.pos())

    def _delete(self, entry: dict) -> None:
        if "id" in entry:
            self.db.delete_calculation(entry["id"])
        self.entries.remove(entry)
        self._js("removeEntry", entry["key"])
        self._sync_empty()
        if "id" in entry:
            self.historyChanged.emit(self.book_id)

    # -- input -------------------------------------------------------------------------
    def _set_input(self, latex: str, focus: bool = False) -> None:
        self._js("setInput" if focus else "setValue", latex)
        self._show_preview()

    def focus_input(self) -> None:
        self.view.setFocus()
        self._js("focusInput", True)

    def preview(self, latex: str) -> None:
        """Live result while typing; unfinished input shows nothing (errors show on Enter)."""
        try:
            expr = latex_to_expr(latex)
            if m := ASSIGNMENT.match(expr):
                expr = m.group(2)
            value = self.calc.compute(expr) if expr.strip() else None
        except (LatexError, CalcError):
            value = None
        if value is None:
            self._show_preview()
            return
        fraction = fraction_of(value)
        self._show_preview(number_latex(format_value(value)), fraction_latex(fraction) if fraction else None)

    def _show_preview(self, result: str = "", fraction: str | None = None, error: str = "") -> None:
        self.preview_text = error or result
        self.preview_error = bool(error)
        self._js("setPreview", {"result": result, "fraction": fraction, "error": error})

    def _set_degrees(self, on: bool) -> None:
        self.calc.degrees = on
        self.db.set_setting("calc_angle", "deg" if on else "rad")
        self._sync_angle()
        self._js("refreshPreview")

    def _sync_angle(self) -> None:
        self.angle.setText("DEG" if self.calc.degrees else "RAD")
