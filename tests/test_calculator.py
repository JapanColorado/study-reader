import math
import sqlite3
from fractions import Fraction

import pytest

from study_reader.calc_latex import LatexError, expr_to_latex, fraction_of, latex_to_expr, number_latex
from study_reader.calculator import Calculator, CalcError, format_value, parse_value, store_value
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


def test_exact_fractions():
    c = Calculator()
    assert c.compute("0.1 + 0.2") == Fraction(3, 10)
    assert c.compute("1/3 + 1/6") == Fraction(1, 2)
    assert c.compute("2^-2") == Fraction(1, 4)
    assert c.compute("6/3") == 2 and isinstance(c.compute("6/3"), int)
    assert c.compute("gcd(8/2, 6)") == 2
    assert isinstance(c.compute("sqrt(2) / 2"), float)
    assert format_value(Fraction(1, 3)) == "0.333333333333"
    assert parse_value(store_value(Fraction(-7, 2))) == Fraction(-7, 2)
    assert parse_value(store_value(0.5)) == 0.5 and parse_value("3") == 3
    c.variables.update(x=2, y=3)
    assert c.compute("xy") == 6  # implied multiplication of known names


def test_fraction_of():
    assert fraction_of(Fraction(3, 4)) == Fraction(3, 4)
    assert fraction_of(math.sin(math.pi / 6)) == Fraction(1, 2)  # float noise near a simple fraction
    assert fraction_of(math.pi) is None and fraction_of(math.sqrt(2)) is None
    assert fraction_of(4) is None and fraction_of(Fraction(1, 10**20)) is None
    assert number_latex("1.5e+20") == "1.5\\times10^{20}" and number_latex("-∞") == "-\\infty"


@pytest.mark.parametrize("latex, expr", [
    (r"\frac{1}{2+3}", "(1)/(2+3)"),
    (r"\frac34x", "(3)/(4)*x"),
    (r"2\pi r", "2*pi*r"),
    (r"3\left(x+1\right)", "3*(x+1)"),
    (r"\sqrt{2}\cdot\sqrt[3]{8}", "sqrt(2)*cbrt(8)"),
    (r"\sqrt[4]{16}", "(16)^(1/(4))"),
    (r"x^{10}+e^2", "x^(10)+e^(2)"),
    (r"\sin2x", "sin(2*x)"),
    (r"\sin^{-1}\left(1\right)", "asin(1)"),
    (r"\sin^2\left(x\right)", "(sin(x))^(2)"),
    (r"\log_{2}\left(8\right)", "log(8, 2)"),
    (r"\left|x\right|+|y|", "abs(x)+abs(y)"),
    (r"\left\lfloor2.5\right\rfloor", "floor(2.5)"),
    (r"5!", "factorial(5)"),
    (r"7\%2", "7%2"),
    (r"\operatorname{comb}\left(5,2\right)", "comb(5,2)"),
    (r"x_{1}=2.5e3", "x_1=2.5e3"),
    (r"1.5\times10^{20}", "1.5*10^(20)"),
    (r"\infty", "inf"),
])
def test_latex_to_expr(latex, expr):
    assert latex_to_expr(latex) == expr


@pytest.mark.parametrize("latex", [r"\frac{\placeholder{}}{2}", r"\int x", r"!"])
def test_latex_errors(latex):
    with pytest.raises(LatexError):
        latex_to_expr(latex)


def test_plain_expressions_load_into_the_math_field():
    c = Calculator()
    for expr in ("sqrt(2) * sin(pi/4) + 3^10 % 7", "log10(100) + 2**10 // 3"):
        assert c.compute(latex_to_expr(expr_to_latex(expr))) == c.compute(expr)


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
    assert panel.submit(r"r=\frac{3}{2}") and panel.submit(r"2r")
    assert not panel.submit(r"\frac{1}{\placeholder{}}") and panel.preview_error
    assert [e["expr"] for e in panel.entries] == ["r=(3)/(2)", "2*r"]
    panel.preview(r"\frac{1}{3}")
    assert panel.preview_text == "0.333333333333" and not panel.preview_error

    again = CalculatorPanel(db)
    again.set_book(book)
    assert [e["latex"] for e in again.entries] == [r"r=\frac{3}{2}", "2r"]
    assert again.calc.variables == {"r": Fraction(3, 2)} and again.calc.ans == 3
    again.set_book(None)
    assert again.entries == [] and again.calc.ans is None


def test_calculations_gain_latex_column(tmp_path):
    path = tmp_path / "old.db"
    conn = sqlite3.connect(path)
    conn.executescript(
        "CREATE TABLE books (id INTEGER PRIMARY KEY, hash TEXT UNIQUE, path TEXT, title TEXT, page_count INTEGER,"
        " opened_at REAL);"
        "CREATE TABLE calculations (id INTEGER PRIMARY KEY, book_id INTEGER, expr TEXT NOT NULL,"
        " result TEXT NOT NULL, value TEXT NOT NULL, created_at REAL NOT NULL);"
        "INSERT INTO books (id, hash, path, title, page_count, opened_at) VALUES (1, 'h', '/a.pdf', 'A', 1, 0);"
        "INSERT INTO calculations (book_id, expr, result, value, created_at) VALUES (1, '1 + 1', '2', '2', 0);"
    )
    conn.commit()
    conn.close()
    db = Database(path)
    assert db.calculations(1)[0]["latex"] == ""


def test_shared_history_has_no_book(tmp_path):
    """The Review page keeps one history for cards with no book (book_id NULL)."""
    db = Database(tmp_path / "t.db")
    book = db.upsert_book("h", "/a.pdf", "A", 1)["id"]
    db.add_calculation(None, "1 + 1", "2", "2")
    db.add_calculation(book, "2 + 2", "4", "4")
    assert [r["expr"] for r in db.calculations(None)] == ["1 + 1"]
    assert [r["expr"] for r in db.calculations(book)] == ["2 + 2"]
    db.clear_calculations(None)
    assert db.calculations(None) == [] and len(db.calculations(book)) == 1


def test_calculations_book_becomes_optional(tmp_path):
    """Databases from when every calculation had a book get the table rebuilt, rows kept."""
    path = tmp_path / "old.db"
    conn = sqlite3.connect(path)
    conn.executescript(
        "CREATE TABLE books (id INTEGER PRIMARY KEY, hash TEXT UNIQUE, path TEXT, title TEXT, page_count INTEGER,"
        " opened_at REAL);"
        "CREATE TABLE calculations (id INTEGER PRIMARY KEY,"
        " book_id INTEGER NOT NULL REFERENCES books(id) ON DELETE CASCADE, expr TEXT NOT NULL,"
        " latex TEXT NOT NULL DEFAULT '', result TEXT NOT NULL, value TEXT NOT NULL, created_at REAL NOT NULL);"
        "INSERT INTO books (id, hash, path, title, page_count, opened_at) VALUES (1, 'h', '/a.pdf', 'A', 1, 0);"
        "INSERT INTO calculations (id, book_id, expr, latex, result, value, created_at)"
        " VALUES (7, 1, '1 + 1', '1+1', '2', '2', 0);"
    )
    conn.commit()
    conn.close()
    db = Database(path)
    (row,) = db.calculations(1)
    assert (row["id"], row["expr"], row["latex"]) == (7, "1 + 1", "1+1")
    assert db.add_calculation(None, "3", "3", "3")
    db.delete_book(1)  # still goes with its book
    assert db.calculations(1) == [] and len(db.calculations(None)) == 1
    Database(path)  # and opens again without rebuilding


def test_review_calculator_shares_a_history_without_a_book(tmp_path, monkeypatch):
    monkeypatch.setenv("QT_QPA_PLATFORM", "offscreen")
    from PySide6.QtWidgets import QApplication
    from study_reader.calculator import CalculatorPanel

    app = QApplication.instance() or QApplication([])
    db = Database(tmp_path / "t.db")
    book = db.upsert_book("h", "/a.pdf", "A", 1)["id"]
    plain, shared = CalculatorPanel(db), CalculatorPanel(db, shared=True)
    assert not plain.saves and shared.saves
    plain.submit("1+1")  # no book: not kept
    shared.submit("2+3")  # no book: the shared history
    assert db.calculations(None)[0]["expr"] == "2+3" and len(db.calculations(None)) == 1

    changed = []
    shared.historyChanged.connect(changed.append)
    shared.set_book(book)
    shared.submit("x=4")
    assert changed == [book] and db.calculations(book)[0]["expr"] == "x=4"
    plain.set_book(book)
    plain.submit("x+1")
    assert shared.entries[-1]["expr"] == "x=4"
    shared.reload()  # the other one changed it
    assert [e["expr"] for e in shared.entries] == ["x=4", "x+1"] and shared.calc.ans == 5
    shared.clear_history()
    assert changed[-1] == book and db.calculations(book) == [] and len(db.calculations(None)) == 1

    db.delete_book(book)  # removed while its history is up: what comes next is kept in the shared one
    assert shared.submit("7")
    assert shared.book_id is None and [r["expr"] for r in db.calculations(None)] == ["2+3", "7"]
