"""LaTeX from the calculator's math field -> the calculator's plain expressions, and results back to LaTeX.

The math field (MathLive) produces LaTeX like `\\frac{1}{2}\\sqrt{x}+\\sin\\left(\\pi\\right)`; this turns
it into `(1)/(2)*sqrt(x)+sin(pi)` for `Calculator`, inserting the multiplications that written math
leaves out (`2\\pi`, `3\\left(x+1\\right)`, `\\frac{1}{2}x`). Plain expressions typed before the math
field existed read the same way, so they can be loaded back into it.
"""

from __future__ import annotations

import math
import re
from fractions import Fraction

# Calculator functions, so names can be told apart from variables.
FUNCTION_NAMES = {
    "sqrt", "cbrt", "exp", "ln", "log", "log10", "log2", "abs", "floor", "ceil", "round", "trunc",
    "factorial", "gcd", "lcm", "comb", "perm", "hypot", "min", "max", "sinh", "cosh", "tanh",
    "asinh", "acosh", "atanh", "deg", "rad", "sin", "cos", "tan", "asin", "acos", "atan", "atan2",
}
COMMAND_FUNCTIONS = {
    "sin": "sin", "cos": "cos", "tan": "tan", "sinh": "sinh", "cosh": "cosh", "tanh": "tanh",
    "arcsin": "asin", "arccos": "acos", "arctan": "atan", "arsinh": "asinh", "arcosh": "acosh",
    "artanh": "atanh", "ln": "ln", "log": "log", "lg": "log10", "exp": "exp", "min": "min", "max": "max",
    "gcd": "gcd", "deg": "deg",
}
INVERSES = {"sin": "asin", "cos": "acos", "tan": "atan", "sinh": "asinh", "cosh": "acosh", "tanh": "atanh"}
CONSTANT_COMMANDS = {"pi": "pi", "tau": "tau", "infty": "inf", "exponentialE": "e"}
OPERATOR_COMMANDS = {"cdot": "*", "times": "*", "ast": "*", "div": "/", "%": "%", "bmod": "%", "mod": "%"}
NAME_COMMANDS = {"operatorname", "mathrm", "mathit", "mathbf", "text", "textrm", "mathnormal"}
IGNORED = {",", ":", ";", "!", " ", "quad", "qquad", "displaystyle", "textstyle", "limits", "nolimits"}
# Openers of bracketed groups, as {token: (function or "", closing token)}.
DELIMITERS = {
    "(": ("", ")"), "[": ("", "]"), "\\lbrack": ("", "\\rbrack"), "|": ("abs", "|"), "\\vert": ("abs", "\\vert"),
    "\\lvert": ("abs", "\\rvert"), "\\lfloor": ("floor", "\\rfloor"), "\\lceil": ("ceil", "\\rceil"),
}
# Where a function written without brackets (`\sin 2x`) stops taking its argument.
BARE_STOP = {"+", "-", "=", ",", ")", "]", "}", "|", "*", "/", "\\right", "\\cdot", "\\times", "\\div", "\\%", "\\bmod"}

TOKEN = re.compile(r"\\([A-Za-z]+|.)|(\s+)|(.)", re.DOTALL)


class LatexError(Exception):
    pass


def latex_to_expr(latex: str) -> str:
    """Plain calculator expression for the math field's LaTeX. Raises LatexError."""
    return _Parser(latex).parse()


def expr_to_latex(expr: str) -> str:
    """Rough LaTeX for a plain expression, enough for the math field to show and edit it."""
    expr = re.sub(r"(\*\*|\^)\s*(-?[\w.]+)", r"^{\2}", expr)
    return expr.replace("**", "^").replace("%", "\\%")


class _Parser:
    def __init__(self, latex: str):
        self.toks: list[str] = []
        for m in TOKEN.finditer(latex):
            if m.group(1) is not None:
                self.toks.append("\\" + m.group(1))
            elif m.group(3) is not None:
                self.toks.append(m.group(3))
        self.i = 0

    def parse(self) -> str:
        out = self.seq(set())
        if self.i < len(self.toks):
            raise LatexError(f"Unexpected {self.toks[self.i]}")
        return _join(out)

    # -- helpers -------------------------------------------------------------------------
    def peek(self, offset: int = 0) -> str | None:
        j = self.i + offset
        return self.toks[j] if j < len(self.toks) else None

    def take(self) -> str:
        tok = self.toks[self.i]
        self.i += 1
        return tok

    def expect(self, tok: str) -> None:
        """Consume `tok` if it's next; a missing closer at the end is forgiven (the field auto-closes)."""
        if self.peek() == tok:
            self.i += 1
        elif self.peek() is not None:
            raise LatexError(f"Expected {tok}")

    def seq(self, stop: set[str]) -> list[str]:
        out: list[str] = []
        while (tok := self.peek()) is not None and tok not in stop:
            self.item(out)
        return out

    def group(self, closer: str) -> str:
        inner = _join(self.seq({closer}))
        self.expect(closer)
        return inner

    def arg(self) -> str:
        """A TeX argument: `{...}` or a single token."""
        tok = self.peek()
        if tok is None:
            raise LatexError("Something is missing")
        if tok == "{":
            self.i += 1
            return self.group("}")
        if len(tok) == 1 and tok.isalnum():
            self.i += 1
            return tok
        out: list[str] = []
        self.item(out)
        return _join(out)

    def raw_arg(self) -> str:
        """An argument's text as written, for names like \\operatorname{comb}."""
        if self.peek() != "{":
            return self.take() if self.peek() is not None else ""
        self.i += 1
        text, depth = "", 1
        while (tok := self.peek()) is not None:
            self.i += 1
            depth += {"{": 1, "}": -1}.get(tok, 0)
            if depth == 0:
                break
            if not tok.startswith("\\") and tok not in "{}":
                text += tok  # the name inside \operatorname{\mathrm{atan2}}
        return text

    # -- items ---------------------------------------------------------------------------
    def item(self, out: list[str]) -> None:
        tok = self.take()
        if tok.isdigit() or tok == ".":
            out.append(self.number(tok))
        elif tok.isalpha() and tok.isascii():
            self.name(out, tok)
        elif tok in DELIMITERS:
            func, closer = DELIMITERS[tok]
            out.append(f"{func}({self.group(closer)})")
        elif tok == "{":
            out.append(f"({self.group('}')})")
        elif tok == "^":
            out += ["^", f"({self.arg()})"]
        elif tok == "_":
            self.arg()  # a stray subscript carries no meaning for a calculation
        elif tok == "!":
            if not out or out[-1] in OPERATORS:
                raise LatexError("! needs a number before it")
            out[-1] = f"factorial({out[-1]})"
        elif tok in OPERATORS:
            out.append(tok)
        elif tok.startswith("\\"):
            self.command(out, tok[1:])
        else:
            raise LatexError(f"Can't use {tok} here")

    def number(self, first: str) -> str:
        text = first
        while (tok := self.peek()) is not None and (tok.isdigit() or tok == "."):
            text += self.take()
        # Scientific notation: 1e5, 2.5e-3 (an e followed by anything else is the constant)
        if self.peek() == "e":
            nxt, after = self.peek(1), self.peek(2)
            if nxt is not None and nxt.isdigit():
                self.i += 1
                text += "e" + self.number(self.take())
            elif nxt in ("+", "-") and after is not None and after.isdigit():
                self.i += 2
                text += "e" + nxt + self.number(self.take())
        return text

    def name(self, out: list[str], first: str) -> None:
        name = first
        while (tok := self.peek()) is not None and tok.isalpha() and tok.isascii():
            name += self.take()
        digits = ""
        while (tok := self.peek(len(digits))) is not None and tok.isdigit():
            digits += tok
        if digits and name + digits in FUNCTION_NAMES:  # log10, log2, atan2
            self.i += len(digits)
            name += digits
        if self.peek() == "_" and name not in FUNCTION_NAMES:
            self.i += 1
            sub = self.arg()
            if not re.fullmatch(r"\w+", sub):
                raise LatexError("A subscript can only hold letters and digits")
            name += "_" + sub
        if name in FUNCTION_NAMES:
            self.function(out, name)
        else:
            out.append(name)

    def command(self, out: list[str], cmd: str) -> None:
        if cmd in IGNORED:
            return
        if cmd in ("frac", "dfrac", "tfrac", "cfrac"):
            num = self.arg()
            out.append(f"({num})/({self.arg()})")
        elif cmd == "sqrt":
            index = None
            if self.peek() == "[":
                self.i += 1
                index = self.group("]")
            radicand = self.arg()
            if index in (None, "", "2"):
                out.append(f"sqrt({radicand})")
            elif index == "3":
                out.append(f"cbrt({radicand})")
            else:
                out.append(f"({radicand})^(1/({index}))")
        elif cmd == "left":
            opener = self.take() if self.peek() is not None else "."
            func, _ = DELIMITERS.get(opener, ("", None))
            if opener == ".":
                func = ""
            inner = _join(self.seq({"\\right"}))
            if self.peek() == "\\right":
                self.i += 1
                if self.peek() is not None:
                    self.i += 1  # the closing delimiter
            out.append(f"{func}({inner})")
        elif cmd == "right":  # unmatched
            if self.peek() is not None:
                self.i += 1
            out.append(")")
        elif "\\" + cmd in DELIMITERS:
            func, closer = DELIMITERS["\\" + cmd]
            out.append(f"{func}({self.group(closer)})")
        elif cmd in OPERATOR_COMMANDS:
            out.append(OPERATOR_COMMANDS[cmd])
        elif cmd in CONSTANT_COMMANDS:
            out.append(CONSTANT_COMMANDS[cmd])
        elif cmd in COMMAND_FUNCTIONS:
            self.function(out, COMMAND_FUNCTIONS[cmd])
        elif cmd in NAME_COMMANDS:
            text = self.raw_arg()
            if not re.fullmatch(r"[A-Za-z_]\w*", text):
                raise LatexError(f"Unknown name {text!r}")
            if text in FUNCTION_NAMES:
                self.function(out, text)
            else:
                out.append(text)
        elif cmd == "placeholder":
            raise LatexError("Fill in the empty box first")
        else:
            raise LatexError(f"The calculator doesn't know \\{cmd}")

    def function(self, out: list[str], name: str) -> None:
        power = base = None
        while self.peek() in ("^", "_"):
            if self.take() == "^":
                power = self.arg()
            else:
                base = self.arg()
        if power in ("-1", "(-1)") and name in INVERSES:
            name, power = INVERSES[name], None
        tok = self.peek()
        if tok == "(":
            self.i += 1
            inner = self.group(")")
        elif tok == "\\left" and self.peek(1) == "(":
            self.i += 1
            parts: list[str] = []
            self.command(parts, "left")
            inner = parts[0][1:-1]
        else:  # \sin 2x
            parts = []
            while (tok := self.peek()) is not None and tok not in BARE_STOP:
                self.item(parts)
            inner = _join(parts)
        call = f"{name}({inner}, {base})" if base is not None else f"{name}({inner})"
        out.append(f"({call})^({power})" if power is not None else call)


OPERATORS = {"+", "-", "*", "/", "^", "%", "=", ",", ")", "]", "|"}


def _join(items: list[str]) -> str:
    """Concatenates operands and operators, multiplying operands that sit side by side (2x, 3(…))."""
    text = ""
    prev_operand = False
    for item in items:
        operand = item not in OPERATORS
        if operand and prev_operand:
            text += "*"
        text += item
        prev_operand = operand
    return text


# -- results ------------------------------------------------------------------------------
MAX_FRACTION_DIGITS = 12  # longer numerators/denominators aren't worth reading as a fraction
APPROX_DENOMINATOR = 10_000  # floats (from functions like sin) shown as fractions only when this simple


def fraction_of(value) -> Fraction | None:
    """The value as a fraction worth showing (not a whole number), or None."""
    if isinstance(value, Fraction):
        f = value
    elif isinstance(value, float) and math.isfinite(value) and not value.is_integer():
        f = Fraction(value).limit_denominator(APPROX_DENOMINATOR)
        if abs(float(f) - value) > 1e-12 * max(1.0, abs(value)):
            return None
    else:
        return None
    if f.denominator == 1 or len(str(f.denominator)) > MAX_FRACTION_DIGITS or len(str(abs(f.numerator))) > MAX_FRACTION_DIGITS:
        return None
    return f


def fraction_latex(f: Fraction) -> str:
    sign = "-" if f < 0 else ""
    return f"{sign}\\frac{{{abs(f.numerator)}}}{{{f.denominator}}}"


def number_latex(text: str) -> str:
    """LaTeX for a displayed number (`format_value` output), e.g. 1.5e+20 -> 1.5\\times10^{20}."""
    if text in ("∞", "-∞"):
        return text.replace("∞", "\\infty")
    if m := re.fullmatch(r"(-?[\d.]+)e([+-]?\d+)", text):
        return f"{m.group(1)}\\times10^{{{int(m.group(2))}}}"
    return text
