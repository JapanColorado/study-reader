import math
import sqlite3
import time
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
    """The Practice page keeps one history for questions with no book (book_id NULL)."""
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


def test_practice_calculator_shares_a_history_without_a_book(tmp_path, monkeypatch):
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


@pytest.mark.parametrize("expr", [
    "comb(3000000, 1500000)", "perm(300000)", "perm(3000000, 1500000)", "round(1/3, 3000000)", "1+" * 3000 + "1",
])
def test_huge_work_is_refused_before_it_is_done(expr):
    started = time.monotonic()
    with pytest.raises(CalcError):
        Calculator().compute(expr)
    assert time.monotonic() - started < 1


def test_guarded_functions_still_compute():
    c = Calculator()
    assert c.compute("comb(52, 5)") == 2598960 and c.compute("perm(5, 2)") == 20 and c.compute("perm(5)") == 120
    assert c.compute("round(2.567, 2)") == Fraction(257, 100) and c.compute("round(7/2)") == 4


def test_round_and_log_follow_school_conventions():
    """Halves round away from zero and log is base 10, as on a handheld calculator."""
    c = Calculator()
    assert c.compute("round(2.5)") == 3 and c.compute("round(-2.5)") == -3 and c.compute("round(0.5)") == 1
    assert c.compute("round(0.125, 2)") == Fraction(13, 100) and c.compute("round(1250, -2)") == 1300
    assert c.compute("round(sqrt(6.25))") == 3 and c.compute("round(sqrt(0.015625), 2)") == 0.13
    assert c.compute("round(sqrt(2), 3)") == 1.414 and c.compute("round(-1/3, 2)") == Fraction(-33, 100)
    assert c.compute("log(1000)") == 3 and c.compute("log(8, 2)") == 3 and c.compute("ln(e)") == 1
    assert c.compute(latex_to_expr(r"\log\left(100\right)")) == 2


def test_a_function_name_is_never_a_product_of_variables():
    c = Calculator()
    for v in ("s = 2", "i = 3", "n = 5", "x = 7"):
        c.evaluate(v)
    with pytest.raises(CalcError, match="function"):
        c.compute("sin")  # not s·i·n
    with pytest.raises(CalcError, match="needs a value"):
        c.compute(latex_to_expr("\\sin"))  # sin()
    with pytest.raises(CalcError, match="function sin"):
        c.compute("xsin")
    assert c.compute(latex_to_expr("sin x")) == c.compute("sinx") == c.compute("sin(x)")  # not s·i·n·x
    assert c.compute(latex_to_expr("2sin x")) == 2 * c.compute("sin(x)")
    assert c.compute("sinhx") == c.compute("sinh(x)")  # the longest name
    assert c.compute("nx") == 35 and c.compute("nis") == 30  # products as before
    c.evaluate("sinx = 1")
    assert c.compute("sinx") == 1  # a variable of that name comes first


# -- functions, derivatives, integrals, sums, solve ----------------------------------------------
from study_reader.calculator import Formula, Function, Roots  # noqa: E402


def test_defined_functions_and_their_derivatives():
    c = Calculator()
    name, fn = c.evaluate("f(x) = x^3 + 2*x")
    assert name == "f" and isinstance(fn, Function) and fn.params == ("x",)
    assert c.compute("f(2)") == 12 and c.compute("f'(2)") == 14 and c.compute("f''(2)") == 12
    assert c.compute("f'''(5)") == 6 and c.compute("2*f(1) + f'(0)") == 8
    assert c.compute("f'(1/2)") == Fraction(11, 4)  # exact, like the rest
    formula = c.compute("f'(x)", rich=True)
    assert isinstance(formula, Formula) and formula.latex == "3 x^{2} + 2"
    with pytest.raises(CalcError):
        c.compute("f'(x)")  # a formula only where one is asked for (not in quiz grading)
    assert c.derivative_latex(fn) == r"f^{\prime}\left(x\right)=3 x^{2} + 2"


def test_functions_see_variables_when_used():
    c = Calculator()
    c.evaluate("h(x) = a*x^2")
    with pytest.raises(CalcError, match="Unknown name 'a'"):
        c.compute("h(2)")
    c.evaluate("a = 3")
    assert c.compute("h(2)") == 12 and c.compute("h'(2)") == 12
    assert c.compute("h'(x)", rich=True).latex == "6 x"  # a's value goes in
    c.evaluate("x = 5")
    assert c.compute("h'(x)", rich=True) == 30  # x has a value now
    assert c.compute("h(2)") == 12  # the variable x doesn't reach inside h


def test_several_variables_and_partial_derivatives():
    c = Calculator()
    c.evaluate("g(x, y) = x^2*y + y")
    assert c.compute("g(1, 2)") == 4
    assert c.compute("diff(g, x)(1, 2)") == 4 and c.compute("diff(g, y)(3, 0)") == 10
    assert c.compute("diff(g, x, y)", rich=True).latex == "2 x"
    with pytest.raises(CalcError, match="variables"):
        c.compute("g'(1)")
    with pytest.raises(CalcError, match="takes 2 values"):
        c.compute("g(1)")


def test_derivatives_of_expressions():
    c = Calculator()
    assert c.compute("diff(x^2*sin(x), x)", rich=True).latex == r"x^{2} \cos\left(x \right) + 2 x \sin\left(x \right)"
    c.evaluate("x = 2")
    assert c.compute("diff(x^3, x)") == 12 and c.compute("diff(x^3, x, 2)") == 12
    assert math.isclose(c.compute("diff(sin(x), x)"), math.cos(2))
    assert c.compute("diff(t^2, t)", rich=True).latex == "2 t"


def test_derivatives_follow_the_angle_unit():
    c = Calculator(degrees=True)
    c.evaluate("s(x) = sin(x)")
    assert math.isclose(c.compute("s'(0)"), math.pi / 180)
    assert math.isclose(c.compute("s(30)"), 0.5)


@pytest.mark.parametrize("expr, message", [
    ("k'(2)", "Can't differentiate floor"), ("r(2)", "can't call itself"), ("q", "is a function"),
])
def test_function_errors(expr, message):
    c = Calculator()
    c.evaluate("k(x) = floor(x)")
    c.evaluate("r(x) = r(x - 1)")
    c.evaluate("q(x) = x")
    with pytest.raises(CalcError, match=message):
        c.compute(expr)


def test_definitions_replace_variables_and_back():
    c = Calculator()
    c.evaluate("f = 2")
    c.evaluate("f(x) = x + 1")
    assert "f" not in c.variables and c.compute("f(1)") == 2
    c.evaluate("f = 3")
    assert "f" not in c.functions and c.compute("f") == 3
    with pytest.raises(CalcError):
        c.evaluate("sin(x) = x")
    with pytest.raises(CalcError):
        c.evaluate("f(pi) = 1")


def test_integrals_sums_and_products():
    c = Calculator()
    assert c.compute("integral(x^2, x, 0, 1)") == Fraction(1, 3)  # a polynomial: exact
    assert math.isclose(c.compute("integral(sin(x), x, 0, pi)"), 2)
    assert math.isclose(c.compute("integral(exp(-x^2), x, -inf, inf)"), math.sqrt(math.pi))
    assert math.isclose(c.compute("integral(1/x^2, x, 1, inf)"), 1)
    with pytest.raises(CalcError, match="settle"):
        c.compute("integral(1/x, x, 1, inf)")
    assert c.compute("sum(n^2, n, 1, 10)") == 385 and c.compute("prod(n, n, 1, 6)") == 720
    assert c.compute("sum(1/2^n, n, 1, 3)") == Fraction(7, 8)
    assert math.isclose(c.compute("sum(1/n^2, n, 1, 5000)"), math.pi**2 / 6, rel_tol=1e-3)
    c.evaluate("f(x) = 3*x^2")
    assert c.compute("integral(f(x), x, 0, 2)") == 8 and c.compute("integral(f'(t), t, 0, 1)") == 3


def test_solve():
    c = Calculator()
    roots = c.compute("solve(x^2 = 2, x)", rich=True)
    assert isinstance(roots, Roots) and [round(r, 12) for r in roots.values] == [round(-2**0.5, 12), round(2**0.5, 12)]
    assert c.compute("solve(2*x + 1 = 0, x)") == Fraction(-1, 2)  # exact when it is a simple fraction
    assert math.isclose(c.compute("solve(x^2 = 2, x, 1)"), 2**0.5)
    assert math.isclose(c.compute("solve(cos(x) = x, x)"), 0.7390851332151607)
    assert c.compute("solve((x-3)^2, x)") == 3  # touches zero without crossing
    assert [r for r in c.compute("solve(sin(x), x, 1, 7)", rich=True).values] == pytest.approx([math.pi, 2 * math.pi])
    with pytest.raises(CalcError, match="No solution"):
        c.compute("solve(1/x, x)")  # a pole is not a root
    with pytest.raises(CalcError, match="several"):
        c.compute("solve(x^2 = 2, x) + 1", rich=True)
    c.evaluate("f(x) = x^3 - x")
    assert len(c.compute("solve(f(x) = 0, x)", rich=True).values) == 3


@pytest.mark.parametrize("latex, expr", [
    (r"f(x)=x^3+2x", "f(x)=x^(3)+2*x"),
    (r"f\left(x\right)=x^2", "f(x)=x^(2)"),
    (r"g(x,y)=x^2y", "g(x,y)=x^(2)*y"),
    (r"f^{\prime}(2)", "f'(2)"),
    (r"f^{\prime}^{\prime}\left(x\right)", "f''(x)"),
    (r"f^{\prime\prime}(x)", "f''(x)"),
    (r"af(3)", "a*f(3)"),
    (r"\frac{d}{dx}\left(x^2\sin x\right)", "diff(x^(2)*sin(x), x)"),
    (r"\frac{d}{dx}x^2\sin x+1", "diff(x^(2)*sin(x), x)+1"),
    (r"\frac{d^2}{dx^2}\left(x^3\right)", "diff(x^(3), x, x)"),
    (r"\frac{df}{dx}(2)", "diff(f, x)(2)"),
    (r"\frac{\partial^2g}{\partial x\partial y}\left(1,2\right)", "diff(g, x, y)(1,2)"),
    (r"\int_0^1x^2dx", "integral(x^(2), x, 0, 1)"),
    (r"\int_{0}^{\pi}\sin\left(x\right)\,\mathrm{d}x+1", "integral(sin(x), x, 0, pi)+1"),
    (r"\sum_{n=1}^{10}\frac{1}{n}+1", "sum((1)/(n), n, 1, 10)+1"),
    (r"\prod\limits_{k=1}^{5}k", "prod(k, k, 1, 5)"),
    (r"\operatorname{solve}\left(x^2=2,x\right)", "solve(x^(2)=2,x)"),
])
def test_calculus_latex(latex, expr):
    assert latex_to_expr(latex, {"f", "g"}) == expr


def test_quiz_answers_read_latex_as_before():
    """Without the calculator's functions, LaTeX reads as the phone's port reads it."""
    assert latex_to_expr(r"f(x)") == "f*(x)"
    with pytest.raises(LatexError, match="doesn't know"):
        latex_to_expr(r"\int_0^1 x dx")


def test_formulas_read_back_into_the_math_field():
    c = Calculator()
    for expr in ["diff(x^2*sin(x)*cos(x), x)", "diff(tan(x)/x, x)", "diff(asinh(x) + abs(x) + ln(x), x)"]:
        formula = c.compute(expr, rich=True)
        again = Calculator()
        again.variables["x"] = Fraction(7, 10)
        expected = Calculator()
        expected.variables["x"] = Fraction(7, 10)
        assert math.isclose(again.compute(latex_to_expr(formula.latex, set())), expected.compute(expr))


def test_panel_keeps_functions_and_plots_them(tmp_path, monkeypatch):
    monkeypatch.setenv("QT_QPA_PLATFORM", "offscreen")
    from PySide6.QtWidgets import QApplication
    from study_reader.calculator import CalculatorPanel

    app = QApplication.instance() or QApplication([])
    db = Database(tmp_path / "t.db")
    book = db.upsert_book("h", "/a.pdf", "A", 1)["id"]
    panel = CalculatorPanel(db)
    panel.set_book(book)
    assert panel.submit(r"f(x)=x^3+2x") and panel.submit(r"f^{\prime}(2)") and panel.submit(r"f^{\prime}(t)")
    assert panel.submit(r"\operatorname{solve}\left(x^2=4,x\right)")
    page = [panel._page_entry(e) for e in panel.entries]
    assert [p["kind"] for p in page] == ["function", "number", "formula", "roots"]
    assert page[0]["derivative"] == r"f^{\prime}\left(x\right)=3 x^{2} + 2" and page[0]["plot"]
    assert page[2]["result"] == "3 t^{2} + 2" and page[3]["roots"] == ["-2", "2"] and page[3]["exact"]
    assert panel.calc.ans == 14  # formulas and lists of solutions leave ans alone

    sent = []
    monkeypatch.setattr(panel, "_js", lambda *a: sent.append(a))
    panel.plot(panel.entries[0]["key"], -1.0, 1.0, 5)
    name, key, data = sent[-1]
    assert name == "setPlot" and data["f"] == [-3.0, -1.125, 0.0, 1.125, 3.0] and data["df"] == [5.0, 2.75, 2.0, 2.75, 5.0]

    panel.preview(r"g(x)=x^2")
    assert panel.preview_text == r"g^{\prime}\left(x\right)=2 x"
    assert "g" not in panel.calc.functions  # only a preview

    again = CalculatorPanel(db)
    again.set_book(book)
    assert again.calc.compute("f(3)") == 33 and again.calc.ans == 14
    again._delete(again.entries[0])  # the definition goes, and f with it
    assert "f" not in again.calc.functions


def test_panel_imports_math_keeping_the_draft(tmp_path, monkeypatch):
    monkeypatch.setenv("QT_QPA_PLATFORM", "offscreen")
    from PySide6.QtWidgets import QApplication
    from study_reader.calculator import CalculatorPanel

    app = QApplication.instance() or QApplication([])
    db = Database(tmp_path / "t.db")
    panel = CalculatorPanel(db, shared=True)
    panel.preview("1+")
    inputs = []
    monkeypatch.setattr(panel, "_set_input", lambda latex, focus=False: inputs.append(latex))
    assert panel.import_math(r"P(t)=200e^{0.03t}") and panel.import_math(r"v_{0}=12")
    assert inputs[-1] == "1+" and panel.calc.variables["v_0"] == 12
    assert math.isclose(panel.calc.compute("P(10)"), 200 * math.exp(0.3))
    assert not panel.import_math(r"y=\frac{1}{\placeholder{}}") and panel.preview_error and inputs[-1] == r"y=\frac{1}{\placeholder{}}"
