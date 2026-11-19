"""The calculator's symbolic side: derivatives and formulas, through SymPy.

`Calculator` (calculator.py) evaluates its own `ast` trees with exact `Fraction`s. To differentiate,
a tree goes to SymPy (`to_sympy`: user functions inlined, numbers exact, never SymPy's parser, which
runs `eval`), SymPy differentiates it, and the result comes back as an `ast` tree (`from_sympy`) that
`Calculator` evaluates like anything typed, so a derivative's value keeps the calculator's exactness
and limits. Formulas (an expression with variables that hold no value, such as f'(x) with x unset)
are printed by SymPy as LaTeX the math field can read back (`formula_latex`).

Angles: in degrees, sin(u) goes to SymPy as sin(πu/180) and asin(u) as 180·asin(u)/π, so what comes
back is in radians throughout and is evaluated with degrees off (`Derived.radians`).

SymPy is imported on first use (it takes most of a second).
"""

from __future__ import annotations

import ast
import math
import re
from fractions import Fraction
from functools import lru_cache

MAX_ORDER = 10  # derivatives of higher order grow too fast to be worth it
MAX_FORMULA_CHARS = 6000  # longer formulas are refused rather than shown
MAX_SERIES_TERMS = 200  # a sum or product of terms with variables in them, written out
MAX_POWER_BITS = 14_000  # as calculator.MAX_INT_BITS: SymPy would work out 2^(10^6) exactly
PRIME = "ʹ"  # U+02B9, a letter Python allows in names: f'(x) is read as fʹ(x)

_sp = None


def sympy():
    global _sp
    if _sp is None:
        import sympy as module
        _sp = module
    return _sp


class SymbolicError(Exception):
    pass


def symbol(name: str):
    return sympy().Symbol(name, real=True)


def number(value):
    """A calculator number as an exact SymPy number (floats as the decimal they print as)."""
    sp = sympy()
    if isinstance(value, bool):
        raise SymbolicError("Not a number")
    if isinstance(value, int):
        return sp.Integer(value)
    if isinstance(value, Fraction):
        return sp.Rational(value.numerator, value.denominator)
    if isinstance(value, float):
        if math.isinf(value):
            return sp.oo if value > 0 else -sp.oo
        return sp.Rational(Fraction(repr(value)))
    raise SymbolicError("Not a number")


def to_number(expr):
    """A SymPy number back as a calculator number: exact when rational, else a float."""
    sp = sympy()
    if expr.free_symbols:
        raise SymbolicError("Not a number")
    if expr.is_Rational:
        return Fraction(int(expr.p), int(expr.q)) if expr.q != 1 else int(expr.p)
    value = complex(sp.N(expr, 20))
    if abs(value.imag) > 1e-12 * max(1.0, abs(value.real)):
        raise SymbolicError("Result is not a real number")
    return value.real


# -- calculator ast -> SymPy ------------------------------------------------------------------
# Functions SymPy works with; the rest (floor, round, gcd …) only on numbers.
def _functions(degrees: bool) -> dict:
    sp = sympy()
    k = sp.pi / 180
    trig = {"sin": sp.sin, "cos": sp.cos, "tan": sp.tan}
    inverse = {"asin": sp.asin, "acos": sp.acos, "atan": sp.atan}
    table = {
        "sqrt": lambda x: sp.sqrt(x), "cbrt": lambda x: x ** sp.Rational(1, 3), "exp": sp.exp, "ln": sp.log,
        "log": lambda x, b=None: sp.log(x, 10) if b is None else sp.log(x, b),
        "log10": lambda x: sp.log(x, 10), "log2": lambda x: sp.log(x, 2), "abs": sp.Abs, "sign": sp.sign,
        "sinh": sp.sinh, "cosh": sp.cosh, "tanh": sp.tanh, "asinh": sp.asinh, "acosh": sp.acosh, "atanh": sp.atanh,
        "hypot": lambda a, b: sp.sqrt(a**2 + b**2), "deg": lambda x: 180 * x / sp.pi, "rad": lambda x: x * sp.pi / 180,
        "floor": sp.floor, "ceil": sp.ceiling, "min": sp.Min, "max": sp.Max, "factorial": sp.factorial,
    }
    if degrees:
        table |= {name: (lambda f: lambda x: f(k * x))(f) for name, f in trig.items()}
        table |= {name: (lambda f: lambda x: f(x) / k)(f) for name, f in inverse.items()}
        table["atan2"] = lambda y, x: sp.atan2(y, x) / k
    else:
        table |= trig | inverse
        table["atan2"] = sp.atan2
    return table


class Converter:
    """Turns a calculator expression into SymPy. `calc` supplies user functions, constants and, when
    `values` is on, the values of variables (else every variable stays a symbol)."""

    def __init__(self, calc, values: bool = False):
        self.calc = calc
        self.values = values
        self.funcs = _functions(calc.degrees)
        self.stack: list[str] = []

    def convert(self, node: ast.AST, bound: dict | None = None):
        try:
            expr = self._node(node, bound or {})
        except (SymbolicError, RecursionError):
            raise
        except Exception as e:  # SymPy refusing something odd: never more than a failed calculation
            if type(e).__name__ == "CalcError":
                raise SymbolicError(str(e)) from None
            raise SymbolicError("Couldn't work that out") from None
        if len(str(expr)) > MAX_FORMULA_CHARS:
            raise SymbolicError("That formula is too long to work with")
        return expr

    def _node(self, node: ast.AST, bound: dict):
        sp = sympy()
        match node:
            case ast.Constant(value=v) if isinstance(v, (int, float)) and not isinstance(v, bool):
                return number(v)
            case ast.BinOp(op=ast.Add()):
                return self._node(node.left, bound) + self._node(node.right, bound)
            case ast.BinOp(op=ast.Sub()):
                return self._node(node.left, bound) - self._node(node.right, bound)
            case ast.BinOp(op=ast.Mult()):
                return self._node(node.left, bound) * self._node(node.right, bound)
            case ast.BinOp(op=ast.Div()):
                a, b = self._node(node.left, bound), self._node(node.right, bound)
                if b == 0:
                    raise SymbolicError("Division by zero")
                return a / b
            case ast.BinOp(op=ast.Pow()):
                return self._power(self._node(node.left, bound), self._node(node.right, bound))
            case ast.BinOp(op=ast.Mod() | ast.FloorDiv()):
                return self._numeric(node, bound)
            case ast.UnaryOp(op=ast.USub()):
                return -self._node(node.operand, bound)
            case ast.UnaryOp(op=ast.UAdd()):
                return self._node(node.operand, bound)
            case ast.Name(id=name):
                return self._name(name, bound)
            case ast.Call(func=ast.Name(id="diff"), args=args, keywords=[]):
                return self._diff(args, bound)
            case ast.Call(func=ast.Call(func=ast.Name(id="diff"), args=inner, keywords=[]), args=args, keywords=[]):
                fn, variables = self.calc.derivative_target(inner)
                return self._inline(self.calc.derived(fn, variables), [self._node(a, bound) for a in args])
            case ast.Call(func=ast.Name(id=name), args=args, keywords=[]):
                return self._call(name, args, bound)
        if isinstance(node, ast.Compare):
            raise SymbolicError("= only goes in solve(…)")
        raise SymbolicError("Couldn't read that expression")

    def _power(self, base, exp):
        sp = sympy()
        if base.is_Number and exp.is_Integer and abs(int(exp)) > 1 and abs(base) not in (0, 1):
            bits = max(int(abs(base.p)).bit_length(), int(base.q).bit_length()) if base.is_Rational else 64
            if bits * abs(int(exp)) > MAX_POWER_BITS:
                raise SymbolicError("Result is too large")
        return sp.Pow(base, exp)

    def _numeric(self, node: ast.AST, bound: dict):
        """Something SymPy shouldn't work with (5 mod 3, gcd …): only on numbers, by the calculator."""
        names = {n.id for n in ast.walk(node) if isinstance(n, ast.Name)}
        scope = {}
        for name in names:
            value = self._name(name, bound) if name in bound or self.values else None
            if value is not None and not value.free_symbols:
                scope[name] = to_number(value)
        try:
            return number(self.calc.value_of(node, scope))
        except Exception as e:  # CalcError, or a name that still has no value
            raise SymbolicError(_what(node, e)) from None

    def _name(self, name: str, bound: dict):
        sp = sympy()
        calc = self.calc
        if name in bound:
            return bound[name]
        if name == "ans":
            if calc.ans is None:
                raise SymbolicError("No previous answer yet")
            return number(calc.ans)
        if name in calc.variables:
            return number(calc.variables[name]) if self.values else symbol(name)
        consts = {"pi": sp.pi, "e": sp.E, "tau": 2 * sp.pi, "inf": sp.oo}
        if name in consts:
            return consts[name]
        if calc.is_function(name):
            raise SymbolicError(f"'{name.replace(PRIME, chr(39))}' is a function — try {name.replace(PRIME, chr(39))}(…)")
        if len(name) == 1 or "_" in name:
            return symbol(name)
        from .calculator import BY_LENGTH  # sinx is sin(x)
        for func in BY_LENGTH:
            if name.startswith(func) and len(name) > len(func) and func in self.funcs:
                return self.funcs[func](self._name(name[len(func):], bound))
        return sp.Mul(*(self._name(c, bound) for c in name))  # xy is x times y

    def _call(self, name: str, args: list, bound: dict):
        calc = self.calc
        call = ast.Call(func=ast.Name(id=name), args=args, keywords=[])
        if name in ("sum", "prod") and len(args) == 4 and isinstance(args[1], ast.Name):
            return self._series(name, call, bound)
        if name in ("integral", "sum", "prod", "solve"):
            return self._numeric(call, bound)
        values = [self._node(a, bound) for a in args]
        if calc.is_function(name):
            return self._inline(calc.function(name), values)
        if name in self.funcs:
            try:
                return self.funcs[name](*values)
            except TypeError:
                raise SymbolicError("Wrong number of values for that function") from None
        if all(not v.free_symbols for v in values):
            return self._numeric(ast.Call(func=ast.Name(id=name), args=args, keywords=[]), bound)
        raise SymbolicError(f"{name} only works on numbers, not on a variable")

    def _series(self, name: str, call: ast.Call, bound: dict):
        """A sum or product over a variable, term by term (Σ n·x for n = 1…3 is 6x)."""
        body, var, lo, hi = call.args[0], call.args[1].id, *(self._node(a, bound) for a in call.args[2:])
        if not (lo.is_Integer and hi.is_Integer):
            return self._numeric(call, bound)
        if int(hi) - int(lo) + 1 > MAX_SERIES_TERMS:
            try:
                return self._numeric(call, bound)
            except SymbolicError:
                raise SymbolicError(f"That {name} has too many terms to work with as a formula") from None
        sp = sympy()
        terms = [self._node(body, bound | {var: sp.Integer(n)}) for n in range(int(lo), int(hi) + 1)]
        return sp.Add(*terms) if name == "sum" else sp.Mul(*terms)

    def _inline(self, fn, values: list):
        """A user function applied to SymPy values: its body, with its parameters bound to them."""
        if len(values) != len(fn.params):
            raise SymbolicError(f"{fn.label} takes {len(fn.params)} value{'s' if len(fn.params) != 1 else ''}")
        if fn.name in self.stack:
            raise SymbolicError(f"{fn.name} can't call itself")
        if len(self.stack) > 30:
            raise SymbolicError("Functions call each other too deeply")
        if fn.sympy is not None:  # a derivative, already worked out
            expr = fn.sympy.xreplace(dict(zip(fn.sympy_params, values)))
            if self.values:  # the variables it uses, with their values now
                expr = expr.xreplace({symbol(n): number(v) for n, v in self.calc.variables.items()
                                      if symbol(n) in fn.sympy.free_symbols})
            return expr
        self.stack.append(fn.name)
        try:
            return self._node(fn.body, dict(zip(fn.params, values)))
        finally:
            self.stack.pop()

    def _diff(self, args: list, bound: dict):
        """diff(expr, x, …): with x's current value (or symbol) put back in after differentiating."""
        target, variables = self.calc.diff_args(args)
        sp = sympy()
        if isinstance(target, str):  # diff(f, x): f of its own parameters, whatever they hold here
            fn = self.calc.function(target)
            return self._inline(self.calc.derived(fn, variables), [self._name(p, bound) for p in fn.params])
        dummies = {v: sp.Dummy(v, real=True) for v in dict.fromkeys(variables)}
        expr = self._node(target, bound | dummies)
        result = differentiate(expr, tuple(dummies[v] for v in variables))
        return result.xreplace({d: self._name(v, bound) for v, d in dummies.items()})


def _what(node: ast.AST, error: Exception) -> str:
    text = str(error)
    if text.startswith("Unknown name"):
        name = ast.unparse(node).split("(")[0]
        return f"{name} only works on numbers, not on a variable"
    return text


@lru_cache(maxsize=512)
def _diff_cached(expr, variables: tuple):
    return sympy().diff(expr, *variables)


def differentiate(expr, variables: tuple):
    if len(variables) > MAX_ORDER:
        raise SymbolicError(f"Derivatives go up to order {MAX_ORDER}")
    try:
        result = _diff_cached(expr, variables)
    except RecursionError:
        raise SymbolicError("That derivative is too long to work with") from None
    except Exception:
        raise SymbolicError("Couldn't work out that derivative") from None
    sp = sympy()
    if result.has(sp.Derivative, sp.Subs):
        bad = next((f for f in result.atoms(sp.Derivative)), None)
        name = type(bad.expr).__name__ if bad is not None and hasattr(bad, "expr") else "that"
        raise SymbolicError(f"Can't differentiate {LATEX_NAMES.get(name, name)}")
    if len(str(result)) > MAX_FORMULA_CHARS:
        raise SymbolicError("That derivative is too long to work with")
    return result


LATEX_NAMES = {"floor": "floor", "ceiling": "ceil", "factorial": "factorial", "Abs": "abs"}


# -- SymPy -> calculator ast ------------------------------------------------------------------
def from_sympy(expr) -> ast.expr:
    """A calculator expression tree for a SymPy result. Raises SymbolicError for what the calculator
    has no way of writing (a Heaviside step, polygamma …)."""
    return ast.fix_missing_locations(_back(expr))


def _call(name: str, *args) -> ast.Call:
    return ast.Call(func=ast.Name(id=name, ctx=ast.Load()), args=list(args), keywords=[])


def _bin(a, op, b) -> ast.BinOp:
    return ast.BinOp(left=a, op=op, right=b)


BACK_FUNCTIONS = {
    "exp": "exp", "log": "ln", "sin": "sin", "cos": "cos", "tan": "tan", "asin": "asin", "acos": "acos",
    "atan": "atan", "sinh": "sinh", "cosh": "cosh", "tanh": "tanh", "asinh": "asinh", "acosh": "acosh",
    "atanh": "atanh", "Abs": "abs", "sign": "sign", "floor": "floor", "ceiling": "ceil", "factorial": "factorial",
    "Min": "min", "Max": "max", "atan2": "atan2",
}
RECIPROCALS = {"sec": "cos", "csc": "sin", "cot": "tan", "sech": "cosh", "csch": "sinh", "coth": "tanh"}


def _back(expr) -> ast.expr:
    sp = sympy()
    if expr.is_Integer:
        return ast.Constant(value=int(expr))
    if expr.is_Rational:
        return _bin(ast.Constant(value=int(expr.p)), ast.Div(), ast.Constant(value=int(expr.q)))
    if expr.is_Float:
        return ast.Constant(value=float(expr))
    if expr is sp.pi:
        return ast.Name(id="pi", ctx=ast.Load())
    if expr is sp.E:
        return ast.Name(id="e", ctx=ast.Load())
    if expr in (sp.oo, -sp.oo):
        inf = ast.Name(id="inf", ctx=ast.Load())
        return inf if expr is sp.oo else ast.UnaryOp(op=ast.USub(), operand=inf)
    if isinstance(expr, sp.Dummy):
        raise SymbolicError("Couldn't work that out")
    if expr.is_Symbol:
        return ast.Name(id=expr.name, ctx=ast.Load())
    if expr.is_Add:
        terms = [_back(t) for t in expr.args]
        out = terms[0]
        for t in terms[1:]:
            out = _bin(out, ast.Add(), t)
        return out
    if expr.is_Mul:
        factors = [_back(f) for f in expr.args]
        out = factors[0]
        for f in factors[1:]:
            out = _bin(out, ast.Mult(), f)
        return out
    if expr.is_Pow:
        base, exp = expr.args
        if exp == sp.Rational(1, 2):
            return _call("sqrt", _back(base))
        if exp == sp.Rational(-1, 2):
            return _bin(ast.Constant(value=1), ast.Div(), _call("sqrt", _back(base)))
        return _bin(_back(base), ast.Pow(), _back(exp))
    name = type(expr).__name__
    if name in BACK_FUNCTIONS:
        return _call(BACK_FUNCTIONS[name], *(_back(a) for a in expr.args))
    if name in RECIPROCALS:
        return _bin(ast.Constant(value=1), ast.Div(), _call(RECIPROCALS[name], *(_back(a) for a in expr.args)))
    raise SymbolicError(f"The calculator can't work with {name}")


# -- formulas ---------------------------------------------------------------------------------
LATEX_RENAMES = {"arsinh": "asinh", "arcosh": "acosh", "artanh": "atanh", "sign": "sign"}


def formula_latex(expr) -> str:
    """LaTeX for a formula, written so the calculator's math field reads it back."""
    sp = sympy()
    try:
        text = sp.latex(expr, ln_notation=True, inv_trig_style="full")
    except Exception:
        raise SymbolicError("Couldn't write that formula") from None
    for old, new in LATEX_RENAMES.items():
        text = text.replace(f"\\operatorname{{{old}}}", f"\\operatorname{{{new}}}")
    if len(text) > MAX_FORMULA_CHARS:
        raise SymbolicError("That formula is too long to show")
    return _unbrace(text)


def _unbrace(text: str) -> str:
    r"""SymPy writes \sin{\left(x \right)}; the braces would make the math field's \sin take
    everything after it, so they go: \sin\left(x \right)."""
    out, i = [], 0
    while (j := text.find("{\\left(", i)) != -1:
        depth, k = 0, j
        while k < len(text):
            depth += {"{": 1, "}": -1}.get(text[k], 0)
            if depth == 0:
                break
            k += 1
        if k >= len(text) or not text[:k].endswith("\\right)"):
            out.append(text[i:j + 1])
            i = j + 1
            continue
        out.append(text[i:j] + _unbrace(text[j + 1:k]))
        i = k + 1
    out.append(text[i:])
    return "".join(out)


def plain(expr) -> str:
    """The plain (copyable) form of a SymPy formula, in the calculator's own notation."""
    text = ast.unparse(from_sympy(expr)) if expr is not None else ""
    return re.sub(r"\s*\*\*\s*", "^", text)
