"""Right-hand calculator panel: Desmos-style math input with a per-book history."""

from __future__ import annotations

import ast
import json
import math
import operator
import re
from dataclasses import dataclass
from decimal import Decimal
from fractions import Fraction
from pathlib import Path

from PySide6.QtCore import QObject, QPoint, QSize, Qt, QUrl, Signal, Slot
from PySide6.QtGui import QColor, QCursor, QGuiApplication
from PySide6.QtWebChannel import QWebChannel
from PySide6.QtWebEngineWidgets import QWebEngineView
from PySide6.QtWidgets import QHBoxLayout, QLabel, QMenu, QToolButton, QToolTip, QVBoxLayout, QWidget

from .calc_analysis import AnalysisError, find_roots, integrate, root_near
from .calc_latex import LatexError, expr_to_latex, fraction_latex, fraction_of, latex_to_expr, number_latex
from .calc_symbolic import (MAX_ORDER, PRIME, Converter, SymbolicError, differentiate, formula_latex, from_sympy,
                            number, plain, symbol, sympy, to_number)
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


def _whole(n):
    return int(n) if isinstance(n, Fraction) and n.denominator == 1 else n


def _comb(n, k):
    n, k = _whole(n), _whole(k)
    if isinstance(n, int) and isinstance(k, int) and 0 <= k <= n and min(k, n - k) * n.bit_length() > 40 * MAX_INT_BITS:
        raise CalcError("Result is too large")  # before the work: these take minutes when big
    return math.comb(n, k)


def _perm(n, k=None):
    n, k = _whole(n), _whole(k)
    if k is None:
        return _factorial(n)
    if isinstance(n, int) and isinstance(k, int) and 0 <= k <= n and k * n.bit_length() > 40 * MAX_INT_BITS:
        raise CalcError("Result is too large")
    return math.perm(n, k)


def _half_away(x: Fraction) -> int:
    """x to the nearest whole number, halves away from zero (2.5 → 3, −2.5 → −3), as taught
    at school; Python's own round() sends halves to the even neighbour."""
    whole = (abs(x) + Fraction(1, 2)).__floor__()
    return -whole if x < 0 else whole


def _round(x, digits=None):
    if isinstance(digits, Fraction) and digits.denominator == 1:
        digits = int(digits)
    if isinstance(digits, int) and abs(digits) > 1000:
        raise CalcError("That is too many digits to round to")
    if isinstance(x, float) and not math.isfinite(x) or not isinstance(digits, int | None):
        return round(x) if digits is None else round(x, digits)  # Python's errors and oddities
    if digits is None:
        return _half_away(Fraction(x))
    if isinstance(x, float):  # by its exact value, so 2.675 (really 2.67499…) gives 2.67
        if digits > 323:
            return x
        if digits < -308:
            return 0.0 * x
        shift = Fraction(10) ** digits
        rounded = float(Fraction(_half_away(Fraction(x) * shift)) / shift)  # OverflowError past the float range
        return math.copysign(0.0, x) if rounded == 0 else rounded
    shift = Fraction(10) ** digits
    return Fraction(_half_away(x * shift)) / shift


def _log(x, base=None):
    """log is the base-10 logarithm, as on a calculator's key; ln is the natural one."""
    return math.log10(x) if base is None else math.log(x, base)


BINARY = {
    ast.Add: operator.add, ast.Sub: operator.sub, ast.Mult: operator.mul, ast.Div: _div,
    ast.FloorDiv: operator.floordiv, ast.Mod: operator.mod, ast.Pow: _pow,
}
UNARY = {ast.UAdd: operator.pos, ast.USub: operator.neg}
CONSTANTS = {"pi": math.pi, "e": math.e, "tau": math.tau, "inf": math.inf}


def _sign(x):
    return (x > 0) - (x < 0)


FUNCTIONS = {
    "sqrt": math.sqrt, "cbrt": math.cbrt, "exp": math.exp, "ln": math.log, "log": _log,
    "log10": math.log10, "log2": math.log2, "abs": abs, "floor": math.floor, "ceil": math.ceil,
    "round": _round, "trunc": math.trunc, "factorial": _factorial, "gcd": math.gcd, "lcm": math.lcm,
    "comb": _comb, "perm": _perm, "hypot": math.hypot, "min": min, "max": max, "sign": _sign,
    "sinh": math.sinh, "cosh": math.cosh, "tanh": math.tanh,
    "asinh": math.asinh, "acosh": math.acosh, "atanh": math.atanh,
    "deg": math.degrees, "rad": math.radians,
}
TRIG = {"sin": math.sin, "cos": math.cos, "tan": math.tan}
INVERSE_TRIG = {"asin": math.asin, "acos": math.acos, "atan": math.atan, "atan2": math.atan2}
# Forms whose arguments aren't all values: diff(expr, x), integral(expr, x, a, b), sum(expr, n, a, b),
# prod(expr, n, a, b), solve(lhs = rhs, x[, guess | a, b]).
SPECIAL = {"diff", "integral", "sum", "prod", "solve"}
RESERVED = {*CONSTANTS, *FUNCTIONS, *TRIG, *INVERSE_TRIG, *SPECIAL, "ans"}
CALLABLE = {*FUNCTIONS, *TRIG, *INVERSE_TRIG}
BY_LENGTH = sorted(CALLABLE, key=len, reverse=True)  # function names, longest first
ASSIGNMENT = re.compile(r"^\s*([A-Za-z_]\w*)\s*=(?!=)(.*)$")
DEFINITION = re.compile(r"^\s*([A-Za-z_]\w*)\s*\(\s*([A-Za-z_]\w*(?:\s*,\s*[A-Za-z_]\w*)*)\s*\)\s*=(?!=)(.*)$")
STEPS = 1_000_000  # nodes evaluated at most for one calculation (sums, integrals and solve add up)
PREVIEW_STEPS = 100_000  # while typing
MAX_TERMS = 1_000_000
MAX_DEPTH = 30  # functions calling functions
EXACT_SERIES_BITS = 256  # a sum or product's fractions turn into floats past this


Number = int | Fraction | float


@dataclass(eq=False)
class Function:
    """A function the user defined, f(x, y) = body, or a derivative of one (worked out by SymPy:
    `sympy` is the derivative over `sympy_params`, `body` its calculator form, in radians)."""
    name: str
    params: tuple[str, ...]
    body: ast.expr | None
    source: str = ""
    sympy: object = None
    sympy_params: tuple = ()
    radians: bool = False
    error: str = ""  # why `body` is None

    @property
    def label(self) -> str:
        return self.name.replace(PRIME, "'")

    @property
    def signature(self) -> str:
        return f"{self.label}({', '.join(self.params)})"


@dataclass(frozen=True)
class Formula:
    """A result with variables that hold no value: f'(x) with x unset is 3x² + 2."""
    latex: str
    text: str


@dataclass(frozen=True)
class Roots:
    """solve(…) with more than one solution."""
    var: str
    values: tuple
    more: bool = False


class UnknownName(CalcError):
    def __init__(self, name: str):
        super().__init__(f"Unknown name '{name}'")
        self.name = name


def _prepare(expr: str) -> str:
    expr = expr.strip().replace("^", "**").replace("×", "*").replace("÷", "/").replace("−", "-")
    expr = re.sub(r"(?<=\w)('+)(?=\s*\()", lambda m: PRIME * len(m.group(1)), expr)  # f'(x)
    if re.search(r"\bsolve\s*\(", expr):
        expr = re.sub(r"(?<![=<>!])=(?!=)", "==", expr)  # solve(x^2 = 2, x)
    return expr


class Calculator:
    """Evaluates arithmetic safely (no Python eval) and remembers `ans`, assigned variables and
    defined functions.

    Arithmetic on whole numbers and decimals is exact (`Fraction`s), as in 0.1 + 0.2 = 3/10;
    functions like sqrt and sin, and the constants, give floats. Functions are defined as
    f(x) = x^2 + a (a is looked up when f is used) and differentiated symbolically (calc_symbolic):
    f'(2), diff(x^2 sin(x), x), diff(g, x, y). integral, sum, prod and solve are numerical
    (calc_analysis), except that sums and products of exact terms stay exact."""

    def __init__(self, degrees: bool = False):
        self.degrees = degrees
        self.variables: dict[str, Number] = {}
        self.functions: dict[str, Function] = {}
        self.ans: Number | None = None
        self._derived: dict = {}
        self._inline: dict = {}

    # -- state -------------------------------------------------------------------------------
    def _changed(self) -> None:
        """Definitions changed: derivatives worked out before may be stale."""
        self._derived.clear()
        self._inline.clear()

    def reset(self) -> None:
        self.variables.clear()
        self.functions.clear()
        self.ans = None
        self._changed()

    def store(self, name: str, value: Number) -> None:
        if name not in self.variables or name in self.functions:
            self._changed()  # a new name changes how xy splits into x·y
        self.functions.pop(name, None)
        self.variables[name] = value

    def function_names(self) -> set[str]:
        return set(self.functions)

    def is_function(self, name: str) -> bool:
        return name.rstrip(PRIME) in self.functions

    def function(self, name: str) -> Function:
        """A defined function, or with primes (fʹʹ) its derivative of that order."""
        base = name.rstrip(PRIME)
        if base not in self.functions:
            raise CalcError(f"Unknown function '{name.replace(PRIME, chr(39))}'")
        fn = self.functions[base]
        order = len(name) - len(base)
        if not order:
            return fn
        if len(fn.params) != 1:
            raise CalcError(f"{base} has {len(fn.params)} variables: use ∂{base}/∂{fn.params[0]} for a derivative")
        return self.derived(fn, [fn.params[0]] * order)

    def define(self, name: str, params: list[str], body: str) -> Function:
        fn = self.make_function(name, params, body)
        self.variables.pop(name, None)
        self.functions[name] = fn
        self._changed()
        return fn

    def make_function(self, name: str, params: list[str], body: str) -> Function:
        """A checked definition, not yet stored (the preview shows its derivative)."""
        if name in RESERVED or PRIME in name:
            raise CalcError(f"'{name}' is a built-in name")
        for p in params:
            if p in RESERVED or PRIME in p:
                raise CalcError(f"'{p}' is a built-in name, not a variable")
            if p == name:
                raise CalcError(f"'{p}' can't be both the function and its variable")
        if len(set(params)) != len(params):
            raise CalcError("Each variable can appear only once")
        tree = self._parse(body)
        if any(isinstance(n, ast.Compare) for n in ast.walk(tree.body)):
            raise CalcError("A function's formula can't hold another =")
        return Function(name, tuple(params), tree.body, source=body.strip())

    # -- evaluation ----------------------------------------------------------------------------
    def evaluate(self, text: str):
        """Returns (assigned or defined name or None, value): a number, a `Function` (a definition),
        a `Formula` or `Roots`. Raises CalcError."""
        if m := DEFINITION.match(text):
            params = [p.strip() for p in m.group(2).split(",")]
            return m.group(1), self.define(m.group(1), params, m.group(3))
        name, expr = None, text
        if m := ASSIGNMENT.match(text):
            name, expr = m.group(1), m.group(2)
            if name in RESERVED or PRIME in name:
                raise CalcError(f"'{name}' is a built-in name")
        value = self.compute(expr, rich=True)
        if name:
            if not isinstance(value, Number):
                raise CalcError("Only a number can be stored in a variable")
            self.store(name, value)
        if isinstance(value, Number):
            self.ans = value
        return name, value

    def compute(self, expr: str, steps: int = STEPS, rich: bool = False):
        """Value of an expression, without updating `ans` or variables. Only a number unless `rich`,
        which also allows a `Formula` (variables without values, around a derivative or a defined
        function) or `Roots`."""
        tree = self._parse(expr)
        try:
            value = _Evaluation(self, steps, tree.body).eval(tree.body, {})
        except UnknownName:
            if rich and self._symbolic(tree.body):
                try:
                    return self.formula(tree.body)
                except SymbolicError as e:
                    if any(_is_derivative(n) for n in ast.walk(tree.body)):
                        raise CalcError(str(e)) from None
            raise
        except CalcError:
            raise
        except (SymbolicError, AnalysisError) as e:
            raise CalcError(str(e)) from None
        except ZeroDivisionError:
            raise CalcError("Division by zero") from None
        except OverflowError:
            raise CalcError("Result is too large") from None
        except RecursionError:
            raise CalcError("That expression is too long") from None
        except (ValueError, TypeError) as e:
            raise CalcError(_friendly(e)) from None
        if isinstance(value, Roots) and not rich:
            raise CalcError(f"There are several solutions: give a guess, as in solve(…, {value.var}, 1)")
        if isinstance(value, float) and math.isnan(value):
            raise CalcError("Result is not a number")
        if _bits(value) > MAX_INT_BITS:
            raise CalcError("Result is too large")
        return value

    def _parse(self, expr: str) -> ast.Expression:
        expr = _prepare(expr)
        if not expr.strip():
            raise CalcError("Nothing to calculate")
        try:
            return ast.parse(expr, mode="eval")
        except SyntaxError:
            raise CalcError("Couldn't read that expression") from None

    def value_of(self, node: ast.AST, scope: dict, steps: int = STEPS) -> Number:
        """A number for a parsed expression, with `scope`'s names bound (calc_symbolic uses it)."""
        value = _Evaluation(self, steps, node).eval(node, scope)
        if not isinstance(value, Number):
            raise CalcError("Not a single number")
        return value

    def call(self, fn: Function, args: list, steps: int = PREVIEW_STEPS) -> Number:
        """fn applied to numbers (for plots)."""
        evaluation = _Evaluation(self, steps, None)
        return evaluation.apply(fn, args)

    def _symbolic(self, node: ast.AST) -> bool:
        """Whether a derivative or a defined function is in it, so a formula is the answer when a
        variable has no value."""
        for n in ast.walk(node):
            if _is_derivative(n) or isinstance(n, ast.Call) and isinstance(n.func, ast.Name) and self.is_function(n.func.id):
                return True
        return False

    def formula(self, node: ast.AST) -> Formula:
        expr = Converter(self, values=True).convert(node)
        try:
            text = plain(expr)
        except SymbolicError:
            text = str(expr)
        return Formula(formula_latex(expr), text)

    # -- derivatives -----------------------------------------------------------------------------
    def diff_args(self, args: list) -> tuple:
        """diff's target (a function's name, or an expression's tree) and variables, in order:
        diff(f, x, 2) is (\"f\", [x, x])."""
        if len(args) < 2:
            raise CalcError("diff needs a formula and a variable, as in diff(x^2, x)")
        target = args[0]
        if isinstance(target, ast.Name) and self.is_function(target.id):
            target = target.id
        variables: list[str] = []
        for a in args[1:]:
            if isinstance(a, ast.Name) and a.id not in RESERVED and PRIME not in a.id:
                variables.append(a.id)
            elif isinstance(a, ast.Constant) and type(a.value) is int and variables and 1 <= a.value <= MAX_ORDER:
                variables += [variables[-1]] * (a.value - 1)
            else:
                raise CalcError("diff's variables must be names, as in diff(x^2, x)")
        if len(variables) > MAX_ORDER:
            raise CalcError(f"Derivatives go up to order {MAX_ORDER}")
        return target, variables

    def derivative_target(self, args: list) -> tuple[Function, list[str]]:
        target, variables = self.diff_args(args)
        if not isinstance(target, str):
            raise CalcError("Only a function's derivative takes values, as in diff(f, x)(2)")
        return self.function(target), variables

    def derived(self, fn: Function, variables: list[str]) -> Function:
        """The derivative of fn over its variables, in order, as a function of the same variables."""
        key = (fn, tuple(variables), self.degrees)
        if key in self._derived:
            return self._derived[key]
        if len(self._derived) > 512:  # previews make one for every keystroke
            self._derived.clear()
        for v in variables:
            if v not in fn.params:
                raise CalcError(f"{fn.signature} has no variable {v}")
        sp = sympy()
        dummies = tuple(sp.Dummy(p, real=True) for p in fn.params)
        try:
            base = Converter(self)._inline(fn, list(dummies))
            expr = differentiate(base, tuple(dummies[fn.params.index(v)] for v in variables))
        except SymbolicError as e:
            raise CalcError(str(e)) from None
        except CalcError:
            raise
        except Exception:  # SymPy refusing something odd
            raise CalcError(f"Couldn't work out the derivative of {fn.label}") from None
        if len(fn.params) == 1:
            name = fn.name + PRIME * len(variables)
        else:
            name = f"∂{fn.name}/∂{''.join(variables)}" if len(variables) == 1 else f"∂^{len(variables)}{fn.name}/∂{''.join(variables)}"
        try:
            body, error = from_sympy(expr.xreplace({d: symbol(p) for d, p in zip(dummies, fn.params)})), ""
        except SymbolicError as e:
            body, error = None, str(e)
        derived = Function(name, fn.params, body, sympy=expr, sympy_params=dummies, radians=True, error=error)
        self._derived[key] = derived
        return derived

    def derivative_latex(self, fn: Function) -> str | None:
        """f'(x) = … for a function of one variable, or None when it has no derivative to show."""
        if len(fn.params) != 1:
            return None
        try:
            d = self.derived(fn, list(fn.params))
            sp = sympy()
            expr = d.sympy.xreplace({dummy: symbol(p) for dummy, p in zip(d.sympy_params, d.params)})
            return f"{_latex_name(d.name)}\\left({fn.params[0]}\\right)={formula_latex(expr)}"
        except (CalcError, SymbolicError):
            return None

    def inline_derivative(self, node: ast.Call, scope_names) -> ast.expr:
        """diff(expr, x, …) as an expression tree to evaluate where it stands (in radians)."""
        key = (ast.dump(node), frozenset(scope_names), self.degrees)
        if key not in self._inline:
            if len(self._inline) > 256:
                self._inline.clear()
            try:
                expr = Converter(self).convert(node, {n: symbol(n) for n in scope_names})
                self._inline[key] = from_sympy(expr)
            except SymbolicError as e:
                raise CalcError(str(e)) from None
        return self._inline[key]

    def exact_integral(self, body: ast.AST, var: str, a, b, scope: dict):
        """∫ of a polynomial with exact limits, exactly; None otherwise."""
        if not all(isinstance(v, (int, Fraction)) for v in (a, b)):
            return None
        try:
            sp = sympy()
            x = sp.Dummy(var, real=True)
            bound = {n: number(v) for n, v in scope.items()} | {var: x}
            expr = Converter(self, values=True).convert(body, bound)
            if expr.free_symbols - {x} or not expr.is_polynomial(x):
                return None
            poly = sp.Poly(expr, x)
            if poly.degree() > 200 or not all(c.is_Rational for c in poly.coeffs()):
                return None
            return to_number(sp.integrate(poly.as_expr(), (x, number(a), number(b))))
        except (SymbolicError, CalcError, ArithmeticError, ValueError, TypeError):
            return None


def _is_derivative(node: ast.AST) -> bool:
    return (isinstance(node, ast.Call) and isinstance(node.func, (ast.Name, ast.Call))
            and (isinstance(node.func, ast.Call) or node.func.id == "diff" or node.func.id.endswith(PRIME)))


def _latex_name(name: str) -> str:
    base = name.rstrip(PRIME)
    return base + "^{" + "\\prime" * (len(name) - len(base)) + "}" if base != name else base


class _Evaluation:
    """One calculation: a budget of steps, the angle unit (functions worked out by SymPy run in
    radians) and the functions being called, so one that calls itself is caught."""

    def __init__(self, calc: Calculator, steps: int, top: ast.AST | None):
        self.calc = calc
        self.steps = steps
        self.top = top
        self.degrees = calc.degrees
        self.stack: list[str] = []

    def eval(self, node: ast.AST, scope: dict):
        self.steps -= 1
        if self.steps < 0:
            raise CalcError("That takes too long to work out")
        value = _exact(self.node(node, scope))
        if isinstance(value, Roots) and node is not self.top:
            raise CalcError(f"solve found several solutions: give a guess, as in solve(…, {value.var}, 1)")
        return value

    def node(self, node: ast.AST, scope: dict):
        match node:
            case ast.Constant(value=float(v)) if math.isfinite(v):
                return Fraction(repr(v))  # the decimal as written: 0.1 is exactly 1/10
            case ast.Constant(value=v) if isinstance(v, (int, float)) and not isinstance(v, bool):
                return v
            case ast.BinOp(op=op) if type(op) in BINARY:
                return BINARY[type(op)](self.eval(node.left, scope), self.eval(node.right, scope))
            case ast.UnaryOp(op=op) if type(op) in UNARY:
                return UNARY[type(op)](self.eval(node.operand, scope))
            case ast.Name(id=name):
                return self.lookup(name, scope)
            case ast.Call(func=ast.Name(id=name), args=args, keywords=[]) if name in SPECIAL and name not in scope:
                return self.special(name, node, args, scope)
            case ast.Call(func=ast.Call(func=ast.Name(id="diff"), args=inner, keywords=[]), args=args, keywords=[]):
                fn, variables = self.calc.derivative_target(inner)
                return self.apply(self.calc.derived(fn, variables), [self.eval(a, scope) for a in args])
            case ast.Call(func=ast.Name(id=name), args=args, keywords=[]):
                values = [self.eval(a, scope) for a in args]
                if self.calc.is_function(name) and name not in scope:
                    return self.apply(self.calc.function(name), values)
                return self.builtin(name, values)
        raise CalcError("Couldn't read that expression")

    def lookup(self, name: str, scope: dict):
        calc = self.calc
        if name in scope:
            return scope[name]
        if name == "ans":
            if calc.ans is None:
                raise CalcError("No previous answer yet")
            return calc.ans
        if name in calc.variables:
            return calc.variables[name]
        if name in CONSTANTS:
            return CONSTANTS[name]
        if calc.is_function(name):
            label = name.replace(PRIME, "'")
            raise CalcError(f"'{label}' is a function — try {label}(…)")
        if name in CALLABLE or name in SPECIAL:  # before reading it as a product: sin is not s·i·n
            raise CalcError(f"'{name}' is a function — try {name}(…)")
        if len(name) > 1 and PRIME not in name:
            # sinx (from `sin x` too, as spaces are dropped) is sin(x): a function name, then what it
            # is applied to, the longest name first (sinhx is sinh(x)).
            for func in BY_LENGTH:
                if name.startswith(func) and len(name) > len(func):
                    try:
                        arg = self.lookup(name[len(func):], scope)
                    except CalcError:
                        break
                    return self.builtin(func, [arg])
            known = lambda c: c in scope or c in calc.variables or c in CONSTANTS
            if all(known(c) for c in name):
                if func := next((f for f in BY_LENGTH if f in name), None):
                    raise CalcError(f"'{name}' holds the function {func} without a value — try {func}(…), "
                                    "or * between letters to multiply")
                return math.prod(self.lookup(c, scope) for c in name)  # xy is x times y, as written in math
        raise UnknownName(name)

    def builtin(self, name: str, args: list):
        if not args and name in CALLABLE:  # sin() — or `sin` in the math field, with nothing after it
            raise CalcError(f"'{name}' needs a value — try {name}(…)")
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
        raise CalcError(f"Unknown function '{name.replace(PRIME, chr(39))}'")

    def apply(self, fn: Function, args: list):
        if len(args) != len(fn.params):
            n = len(fn.params)
            raise CalcError(f"{fn.signature} takes {n} value{'s' if n != 1 else ''}")
        if fn.body is None:
            raise CalcError(fn.error or f"Can't work out {fn.label}")
        if fn.name in self.stack:
            raise CalcError(f"{fn.label} can't call itself")
        if len(self.stack) >= MAX_DEPTH:
            raise CalcError("Functions call each other too deeply")
        self.stack.append(fn.name)
        saved = self.degrees
        if fn.radians:
            self.degrees = False
        try:
            return self.eval(fn.body, dict(zip(fn.params, args)))
        finally:
            self.stack.pop()
            self.degrees = saved

    def _in_radians(self, node: ast.AST, scope: dict):
        saved = self.degrees
        self.degrees = False
        try:
            return self.eval(node, scope)
        finally:
            self.degrees = saved

    # -- diff, integral, sum, prod, solve ------------------------------------------------------
    def special(self, name: str, node: ast.Call, args: list, scope: dict):
        if name == "diff":
            target, variables = self.calc.diff_args(args)
            if isinstance(target, str):  # diff(f, x): at f's variables' values here
                derived = self.calc.derived(self.calc.function(target), variables)
                if derived.body is None:
                    raise CalcError(derived.error)
                return self._in_radians(derived.body, scope)
            return self._in_radians(self.calc.inline_derivative(node, scope), scope)
        if name == "solve":
            return self.solve(args, scope)
        if len(args) != 4 or not isinstance(args[1], ast.Name):
            example = "integral(x^2, x, 0, 1)" if name == "integral" else f"{name}(n^2, n, 1, 10)"
            raise CalcError(f"{name} takes a formula, a variable and two limits, as in {example}")
        body, var = args[0], args[1].id
        if var in RESERVED:
            raise CalcError(f"'{var}' is a built-in name, not a variable")
        a, b = self.eval(args[2], scope), self.eval(args[3], scope)
        if name == "integral":
            return self.integral(body, var, a, b, scope)
        return self.series(name, body, var, a, b, scope)

    def _float_function(self, body: ast.AST, var: str, scope: dict, quiet: bool):
        def f(t: float):
            try:
                value = self.eval(body, {**scope, var: t})
            except UnknownName:
                raise
            except CalcError as e:
                if quiet and str(e) != "That takes too long to work out":
                    return None  # undefined here (a root of x - 1/x is near its pole at 0)
                raise
            except (ArithmeticError, ValueError, TypeError):
                if quiet:
                    return None
                raise
            if not isinstance(value, Number):
                raise CalcError("Not a single number")
            return float(value)
        return f

    def integral(self, body: ast.AST, var: str, a, b, scope: dict):
        exact = self.calc.exact_integral(body, var, a, b, scope)
        if exact is not None:
            return exact
        try:
            return integrate(self._float_function(body, var, scope, quiet=False), float(a), float(b))
        except (ArithmeticError, ValueError) as e:
            if isinstance(e, ZeroDivisionError) or "domain" in str(e):
                raise CalcError("The formula is undefined somewhere between the limits") from None
            raise

    def series(self, name: str, body: ast.AST, var: str, a, b, scope: dict):
        a, b = _whole(a), _whole(b)
        if isinstance(b, float) and math.isinf(b):
            raise CalcError(f"{'A sum' if name == 'sum' else 'A product'} needs a finite upper limit")
        if not isinstance(a, int) or not isinstance(b, int):
            raise CalcError("The limits must be whole numbers")
        if b - a + 1 > MAX_TERMS:
            raise CalcError("That is too many terms")
        total = 0 if name == "sum" else 1
        for n in range(a, b + 1):
            term = self.eval(body, {**scope, var: n})
            total = total + term if name == "sum" else total * term
            if isinstance(total, Fraction) and total.denominator.bit_length() > EXACT_SERIES_BITS:
                total = float(total)  # 1/n² summed exactly grows a thousand-digit denominator
            if _bits(total) > MAX_INT_BITS:
                raise CalcError("Result is too large")
        return _exact(total)

    def solve(self, args: list, scope: dict):
        if len(args) not in (2, 3, 4) or not isinstance(args[1], ast.Name):
            raise CalcError("solve takes an equation and its variable, as in solve(x^2 = 2, x); "
                            "add a guess, or two limits to search between")
        eq, var = args[0], args[1].id
        if var in RESERVED:
            raise CalcError(f"'{var}' is a built-in name, not a variable")
        if isinstance(eq, ast.Compare):
            if len(eq.ops) != 1 or not isinstance(eq.ops[0], ast.Eq):
                raise CalcError("solve takes one equation, as in solve(x^2 = 2, x)")
            eq = ast.BinOp(left=eq.left, op=ast.Sub(), right=eq.comparators[0])
        limits = [self.eval(a, scope) for a in args[2:]]
        g = self._float_function(eq, var, scope, quiet=True)
        if len(limits) == 1:
            root = root_near(g, float(limits[0]))
            if root is None:
                raise CalcError(f"No solution found near {format_value(limits[0])}")
            return self._snap(eq, var, root, scope)
        lo, hi = (float(min(limits)), float(max(limits))) if limits else (None, None)
        roots, more = find_roots(g, lo, hi)
        if not roots:
            where = f"between {format_value(limits[0])} and {format_value(limits[1])}" if limits else "between −10⁶ and 10⁶"
            raise CalcError(f"No solution found {where}")
        values = tuple(self._snap(eq, var, r, scope) for r in roots)
        return values[0] if len(values) == 1 and not more else Roots(var, values, more)

    def _snap(self, eq: ast.AST, var: str, root: float, scope: dict):
        """A root that is exactly a simple fraction, as that fraction (2.0000000001 → 2)."""
        for limit in (1, 1000):
            c = Fraction(root).limit_denominator(limit)
            if abs(c - Fraction(root)) <= Fraction(1, 10**9) * max(1, abs(c)):
                try:
                    if self.eval(eq, {**scope, var: _exact(c)}) == 0:
                        return _exact(c)
                except (CalcError, ArithmeticError, ValueError, TypeError):
                    pass
        return 0.0 if root == 0 else root


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
    "Functions: sqrt cbrt exp ln log (base 10; log(x, base) for another) log2 abs floor ceil round trunc\n"
    "  sin cos tan asin acos atan atan2 sinh cosh tanh asinh acosh atanh\n"
    "  factorial gcd lcm comb perm hypot min max sign deg rad\n"
    "Your own functions: f(x) = x^3 + 2x (or g(x, y) = …), then f(2), f'(2), f''(x); a plot comes with each\n"
    "  (drag to pan, Ctrl+scroll to zoom, double-click to reset). With x unset, f'(x) is shown as a formula\n"
    "Derivatives: type ddx for d/dx (d/dx (x^2 sin x)), or partial for ∂ (∂g/∂x, ∂²g/∂x∂y)\n"
    "int for ∫ with limits, ending in dx · sum and prod for Σ and Π (n=1 below, 10 above)\n"
    "solve(x^2 = 2, x) for every solution, solve(…, x, 1) for the one near 1, solve(…, x, 0, 5) between 0 and 5\n"
    "Whole numbers and decimals stay exact: click a result's ½ button to see it as a fraction"
)

# What a history row holds, by its stored value: a number, a definition, a formula or solve's roots.
FUNCTION_VALUE, FORMULA_VALUE = "function", "formula"


def value_kind(value: str) -> str:
    if value == FUNCTION_VALUE:
        return "function"
    if value == FORMULA_VALUE:
        return "formula"
    if value.startswith("{"):
        return "roots"
    return "number"


def name_latex(name: str) -> str:
    """f_1 → f_{1}, fʹ → f^{\\prime}."""
    base = name.rstrip(PRIME)
    primes = len(name) - len(base)
    if "_" in base:
        head, sub = base.split("_", 1)
        base = f"{head}_{{{sub}}}"
    return base + ("^{" + "\\prime" * primes + "}" if primes else "")


def signature_latex(fn: Function) -> str:
    return f"{name_latex(fn.name)}\\left({','.join(name_latex(p) for p in fn.params)}\\right)"


def roots_text(roots: Roots) -> str:
    exact = all(not isinstance(v, float) for v in roots.values)
    text = f"{roots.var} {'=' if exact else '≈'} " + ", ".join(format_value(v) for v in roots.values)
    return text + (", …" if roots.more else "")


PLOT_POINTS = 800  # samples at most per curve


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

    @Slot(int)
    def menu(self, key: int) -> None:
        self.panel.history_menu(key)

    @Slot(int, float, float, int)
    def plot(self, key: int, xmin: float, xmax: float, n: int) -> None:
        self.panel.plot(key, xmin, xmax, n)


class CalculatorPanel(QWidget):
    """Header and hint in Qt; history and the math-field input in calculator.html. There are two:
    the open book's, and the Practice page's (shared=True), which shows the history of the book of
    the question being practiced and, for a question with no book, a shared one (book_id None). closable: a
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
        self.draft = ""  # what the input holds, as the page last said

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
        hint = QLabel("Enter to calculate · / for a fraction · ↑ for earlier · x = … to store · f(x) = … to define")
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
        kind = value_kind(entry["value"])
        latex = entry["latex"] or expr_to_latex(entry["expr"])
        if kind == "function":
            fn = entry.get("fn")
            return {
                "key": entry["key"], "kind": kind, "latex": latex, "result": "",
                "signature": signature_latex(fn) if fn else "", "args": len(fn.params) if fn else 0,
                "derivative": self.calc.derivative_latex(fn) if fn else None,
                "plot": bool(fn and len(fn.params) == 1),
                "names": [name_latex(fn.name), name_latex(fn.name + PRIME)] if fn else [],
            }
        if kind == "formula":
            return {"key": entry["key"], "kind": kind, "latex": latex, "result": entry["result"]}
        if kind == "roots":
            data = json.loads(entry["value"])
            values = [parse_value(v) for v in data["roots"]]
            return {
                "key": entry["key"], "kind": kind, "latex": latex, "var": name_latex(data["var"]),
                "roots": [number_latex(format_value(v)) for v in values], "more": data.get("more", False),
                "exact": all(not isinstance(v, float) for v in values),
            }
        value = parse_value(entry["value"])
        fraction = fraction_of(value)
        return {
            "key": entry["key"], "kind": kind, "latex": latex,
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
        """Show a book's history (None: none, or for the Practice page's, the shared one); the input clears."""
        self.book_id = book_id
        self.clear_btn.setToolTip("Clear this book's calculator history" if book_id is not None
                                  else "Clear the calculator history of questions with no book")
        self._load()
        self._set_input("")

    def reload(self) -> None:
        """The history was changed by the other calculator: show it again, keeping the input."""
        self._load()
        self._js("refreshPreview")

    def _load(self) -> None:
        self.calc.reset()
        self.entries = []
        rows = self.db.calculations(self.book_id) if self.saves else []
        for row in rows:
            entry = self._keyed(dict(row))
            self._restore(entry)
            self.entries.append(entry)
        self._render()

    def _render(self) -> None:
        self._js("setEntries", [self._page_entry(e) for e in self.entries])
        self._sync_empty()

    def _replay(self) -> None:
        """Variables, functions and ans again from the history as it now stands (a row went)."""
        self.calc.reset()
        for entry in self.entries:
            self._restore(entry)

    def _keyed(self, entry: dict) -> dict:
        entry["key"] = self._next_key
        self._next_key += 1
        return entry

    def _restore(self, entry: dict) -> None:
        """Rebuild `ans`, variables and functions from saved rows, numbers by their saved values rather
        than by re-evaluating (the angle unit may differ now); definitions are read again."""
        kind = value_kind(entry["value"])
        if kind == "function":
            entry["fn"] = None
            if m := DEFINITION.match(entry["expr"]):
                try:
                    entry["fn"] = self.calc.define(m.group(1), [p.strip() for p in m.group(2).split(",")], m.group(3))
                except CalcError:
                    pass
            return
        if kind != "number":
            return
        value = parse_value(entry["value"])
        if m := ASSIGNMENT.match(entry["expr"]):
            self.calc.store(m.group(1), value)
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
            expr = latex_to_expr(latex, self.calc.function_names())
            _, value = self.calc.evaluate(expr)
        except (LatexError, CalcError) as e:
            self._show_preview(error=str(e))
            return False
        entry = {"expr": expr, "latex": latex, **self._stored(value)}
        if self.saves:
            entry["id"] = self.db.add_calculation(
                self.book_id, expr, entry["result"], entry["value"], latex,
            )
        if isinstance(value, Function):
            entry["fn"] = value
        self.entries.append(self._keyed(entry))
        self._js("addEntry", self._page_entry(entry))
        self._set_input("")
        self._sync_empty()
        self._js("refreshPlots")  # a variable or function they use may have changed
        if self.saves:
            self.historyChanged.emit(self.book_id)
        return True

    def import_math(self, latex: str) -> bool:
        """Define a function or variable from a question's formula, keeping what was being typed."""
        draft = self.draft
        if self.submit(latex):
            if draft:
                self._set_input(draft)
            return True
        error = self.preview_text
        self._set_input(latex, focus=True)
        self._show_preview(error=error)
        return False

    @staticmethod
    def _stored(value) -> dict:
        """The result and value columns for an evaluated row."""
        if isinstance(value, Function):
            return {"result": value.signature, "value": FUNCTION_VALUE}
        if isinstance(value, Formula):
            return {"result": value.latex, "value": FORMULA_VALUE}
        if isinstance(value, Roots):
            data = {"var": value.var, "roots": [store_value(v) for v in value.values], "more": value.more}
            return {"result": roots_text(value), "value": json.dumps(data)}
        return {"result": format_value(value), "value": store_value(value)}

    def _sync_empty(self) -> None:
        self.clear_btn.setEnabled(bool(self.entries))

    def _entry(self, key: int) -> dict | None:
        return next((e for e in self.entries if e["key"] == key), None)

    def history_menu(self, key: int) -> None:
        entry = self._entry(key)
        if not entry:
            return
        page = self._page_entry(entry)
        kind = page["kind"]
        clipboard = QGuiApplication.clipboard()
        menu = QMenu(self)
        if kind == "number":
            fraction = fraction_of(parse_value(entry["value"]))
            menu.addAction("Copy result").triggered.connect(lambda: clipboard.setText(entry["result"]))
            if fraction:
                menu.addAction("Copy as fraction").triggered.connect(lambda: clipboard.setText(str(fraction)))
        elif kind == "formula":
            menu.addAction("Copy formula (LaTeX)").triggered.connect(lambda: clipboard.setText(entry["result"]))
        elif kind == "roots":
            menu.addAction("Copy solutions").triggered.connect(lambda: clipboard.setText(entry["result"]))
        elif page.get("derivative"):
            menu.addAction("Copy derivative (LaTeX)").triggered.connect(lambda: clipboard.setText(page["derivative"]))
        menu.addAction("Copy definition" if kind == "function" else "Copy expression").triggered.connect(
            lambda: clipboard.setText(entry["expr"]))
        menu.addAction("Copy LaTeX").triggered.connect(lambda: clipboard.setText(page["latex"]))
        menu.addAction("Edit definition" if kind == "function" else "Edit expression").triggered.connect(
            lambda: self._set_input(page["latex"], focus=True))
        menu.addSeparator()
        menu.addAction("Delete").triggered.connect(lambda: self._delete(entry))
        menu.exec(QCursor.pos())

    def _delete(self, entry: dict) -> None:
        if "id" in entry:
            self.db.delete_calculation(entry["id"])
        self.entries.remove(entry)
        self._js("removeEntry", entry["key"])
        self._sync_empty()
        self._replay()
        self._js("refreshPlots")
        self._js("refreshPreview")
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
        """Live result while typing; unfinished input shows nothing (errors show on Enter). A
        definition shows its derivative."""
        self.draft = latex
        try:
            expr = latex_to_expr(latex, self.calc.function_names())
            if m := DEFINITION.match(expr):
                fn = self.calc.make_function(m.group(1), [p.strip() for p in m.group(2).split(",")], m.group(3))
                derivative = self.calc.derivative_latex(fn)
                self._show_preview(derivative or "", plain=True)
                return
            if m := ASSIGNMENT.match(expr):
                expr = m.group(2)
            value = self.calc.compute(expr, steps=PREVIEW_STEPS, rich=True) if expr.strip() else None
        except (LatexError, CalcError):
            value = None
        if value is None:
            self._show_preview()
        elif isinstance(value, Formula):
            self._show_preview(value.latex)
        elif isinstance(value, Roots):
            page = self._page_entry({"key": -1, "expr": "", "latex": "", **self._stored(value)})
            self._show_preview(f"{page['var']}{'=' if page['exact'] else '≈'}" + ",\\;".join(page["roots"])
                               + (",\\;\\ldots" if page["more"] else ""), plain=True)
        else:
            fraction = fraction_of(value)
            self._show_preview(number_latex(format_value(value)), fraction_latex(fraction) if fraction else None)

    def _show_preview(self, result: str = "", fraction: str | None = None, error: str = "", plain: bool = False) -> None:
        self.preview_text = error or result
        self.preview_error = bool(error)
        self._js("setPreview", {"result": result, "fraction": fraction, "error": error, "plain": plain})

    # -- plots ---------------------------------------------------------------------------------
    def plot(self, key: int, xmin: float, xmax: float, n: int) -> None:
        """Samples of a definition row's function and its derivative, for the page to draw."""
        entry = self._entry(key)
        fn = entry.get("fn") if entry else None
        if not fn or len(fn.params) != 1 or not (math.isfinite(xmin) and math.isfinite(xmax)) or xmax <= xmin:
            return
        n = max(2, min(int(n), PLOT_POINTS))
        xs = [xmin + (xmax - xmin) * i / (n - 1) for i in range(n)]
        try:
            derived = self.calc.derived(fn, list(fn.params))
            if derived.body is None:
                derived = None
        except CalcError:
            derived = None

        def samples(f: Function) -> list[float | None]:
            out = []
            for x in xs:
                try:
                    y = float(self.calc.call(f, [x]))
                except (CalcError, SymbolicError, ArithmeticError, ValueError, TypeError, RecursionError):
                    y = None
                out.append(y if y is not None and math.isfinite(y) else None)
            return out

        ys = samples(fn)
        self._js("setPlot", key, {"xmin": xmin, "xmax": xmax, "f": ys, "df": samples(derived) if derived else None})

    def _set_degrees(self, on: bool) -> None:
        self.calc.degrees = on
        self.db.set_setting("calc_angle", "deg" if on else "rad")
        self._sync_angle()
        if any(value_kind(e["value"]) == "function" for e in self.entries):
            self._render()  # derivatives of sin and the like change with the unit
        self._js("refreshPreview")

    def _sync_angle(self) -> None:
        self.angle.setText("DEG" if self.calc.degrees else "RAD")
