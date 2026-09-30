import math

import pytest

from study_reader.calculator import Calculator, CalcError, format_value
from study_reader.db import Database


def test_arithmetic_and_precedence():
    c = Calculator()
    assert c.compute("2 + 3 ^ 2") == 11
    assert c.compute("-2**2") == -4
    assert c.compute("7 // 2 + 7 % 2") == 4
    assert c.compute("sqrt(16) + log(8, 2) + ln(e)") == 8
    assert c.compute("factorial(5) / comb(5, 2)") == 12
    assert math.isclose(c.compute("sin(pi / 6)"), 0.5)
    assert c.compute("sin(pi)") == 0.0


def test_ans_and_variables():
    c = Calculator()
    assert c.evaluate("x = 4") == ("x", 4)
    assert c.evaluate("x * 2")[1] == 8
    assert c.evaluate("ans + x")[1] == 12
    with pytest.raises(CalcError):
        c.evaluate("pi = 3")


def test_degrees():
    c = Calculator(degrees=True)
    assert math.isclose(c.compute("cos(60)"), 0.5)
    assert math.isclose(c.compute("atan(1)"), 45)


@pytest.mark.parametrize("expr", [
    "1/0", "sqrt(-1)", "(-8) ^ (1/3)", "9 ^ 9 ^ 9", "factorial(100000)", "__import__('os')",
    "x", "sin", "2 +", "(1).real", "[1, 2]", "True + 1", "abs(1, 2)",
])
def test_errors_are_friendly(expr):
    with pytest.raises(CalcError):
        Calculator().compute(expr)


def test_format_value():
    assert format_value(3.0) == "3"
    assert format_value(0.1 + 0.2) == "0.3"
    assert format_value(1 / 3) == "0.333333333333"
    assert format_value(2**100) == "1.2676506002e+30"
    assert format_value(math.inf) == "∞"


def test_calculations_are_per_book(tmp_path):
    db = Database(tmp_path / "t.db")
    a = db.upsert_book("h1", "/a.pdf", "A", 1)["id"]
    b = db.upsert_book("h2", "/b.pdf", "B", 1)["id"]
    db.add_calculation(a, "x = 2", "2", "2")
    db.add_calculation(b, "1 + 1", "2", "2")
    assert [r["expr"] for r in db.calculations(a)] == ["x = 2"]
    db.clear_calculations(a)
    assert db.calculations(a) == [] and len(db.calculations(b)) == 1
    db.delete_book(b)
    assert db.calculations(b) == []


def test_panel_saves_and_restores_history(tmp_path, monkeypatch):
    monkeypatch.setenv("QT_QPA_PLATFORM", "offscreen")
    from PySide6.QtWidgets import QApplication
    from study_reader.calculator import CalculatorPanel

    app = QApplication.instance() or QApplication([])
    db = Database(tmp_path / "t.db")
    book = db.upsert_book("h", "/a.pdf", "A", 1)["id"]
    panel = CalculatorPanel(db)
    panel.set_book(book)
    for text in ("r = 1.5", "2 * r", "oops +"):
        panel.input.setText(text)
        panel.submit()
    assert panel.history.count() == 2
    assert panel.input.text() == "oops +" and panel.preview.property("role") == "error"

    again = CalculatorPanel(db)
    again.set_book(book)
    assert again.history.count() == 2
    assert again.calc.variables == {"r": 1.5} and again.calc.ans == 3.0
    again.set_book(None)
    assert again.history.count() == 0 and again.calc.ans is None
