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
# The calculator's own, beyond what a quiz answer is read with (the phone's port reads those alike).
CALCULUS_NAMES = {"sign", "diff", "integral", "sum", "prod", "solve"}
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
NAME_COMMANDS_TOKENS = {"\\" + c for c in NAME_COMMANDS}
IGNORED = {",", ":", ";", "!", " ", "quad", "qquad", "displaystyle", "textstyle", "limits", "nolimits"}
# Openers of bracketed groups, as {token: (function or "", closing token)}.
DELIMITERS = {
    "(": ("", ")"), "[": ("", "]"), "\\lbrack": ("", "\\rbrack"), "|": ("abs", "|"), "\\vert": ("abs", "\\vert"),
    "\\lvert": ("abs", "\\rvert"), "\\lfloor": ("floor", "\\rfloor"), "\\lceil": ("ceil", "\\rceil"),
}
# Where a function written without brackets (`\sin 2x`) stops taking its argument.
BARE_STOP = {"+", "-", "=", ",", ")", "]", "}", "|", "*", "/", "\\right", "\\cdot", "\\times", "\\div", "\\%", "\\bmod"}
# Where what a sum, a product or d/dx applies to stops (\sum_{n=1}^{5} n/2 is the sum of n/2).
TERM_STOP = {"+", "-", "=", ",", ")", "]", "}", "|", "\\right"}
PRIME_TOKENS = {"\\prime": 1, "'": 1, "\\doubleprime": 2, "\\tripleprime": 3}

TOKEN = re.compile(r"\\([A-Za-z]+|.)|(\s+)|(.)", re.DOTALL)


class LatexError(Exception):
    pass


def latex_to_expr(latex: str, functions=None) -> str:
    """Plain calculator expression for the math field's LaTeX. Raises LatexError.

    `functions` (the calculator's; None for a quiz answer) are the names of the user's own functions,
    so f(x) is a call rather than f times x, and turns on what only the calculator reads: definitions
    f(x) = …, f'(x), d/dx and ∂/∂x, integrals, sums and products, sign and solve."""
    return _Parser(latex, functions).parse()


def expr_to_latex(expr: str) -> str:
    """Rough LaTeX for a plain expression, enough for the math field to show and edit it."""
    expr = re.sub(r"(\*\*|\^)\s*(-?[\w.]+)", r"^{\2}", expr)
    return expr.replace("**", "^").replace("%", "\\%")


class _Parser:
    def __init__(self, latex: str, functions=None, toks: list[str] | None = None):
        self.calculus = functions is not None
        self.functions = set(functions or ())
        self.names = FUNCTION_NAMES | CALCULUS_NAMES if self.calculus else FUNCTION_NAMES
        self.toks: list[str] = [] if toks is None else toks
        if toks is None:
            for m in TOKEN.finditer(latex):
                if m.group(1) is not None:
                    self.toks.append("\\" + m.group(1))
                elif m.group(3) is not None:
                    self.toks.append(m.group(3))
        self.i = 0

    def sub(self, toks: list[str]) -> str:
        """An expression made of some of the tokens (an integral's body)."""
        parser = _Parser("", self.functions, toks)
        return parser.parse()

    def parse(self) -> str:
        head = self.definition_head() if self.calculus else ""
        out = self.seq(set())
        if self.i < len(self.toks):
            raise LatexError(f"Unexpected {self.toks[self.i]}")
        return head + _join(out)

    def definition_head(self) -> str:
        """`f(x, y) =` at the start, as `f(x,y)=`, with f taken as a function from then on."""
        toks, j = self.toks, 0
        name = ""
        while j < len(toks) and len(toks[j]) == 1 and toks[j].isalpha() and toks[j].isascii():
            name += toks[j]
            j += 1
        if not name:
            return ""
        if j + 1 < len(toks) and toks[j] == "_":
            sub, j2 = self.raw(j + 1)
            if not sub or not all(re.fullmatch(r"\w", t) for t in sub):
                return ""
            name += "_" + "".join(sub)
            j = j2
        if j < len(toks) and toks[j] == "\\left":
            j += 1
        if j >= len(toks) or toks[j] != "(":
            return ""
        j += 1
        params, current = [], ""
        while j < len(toks) and toks[j] not in (")", "\\right"):
            t = toks[j]
            if len(t) == 1 and t.isalpha() and t.isascii():
                current += t
            elif t == "," and current:
                params.append(current)
                current = ""
            elif t == "_" and current:
                sub, j = self.raw(j + 1)
                if not sub or not all(re.fullmatch(r"\w", s) for s in sub):
                    return ""
                current += "_" + "".join(sub)
                continue
            else:
                return ""
            j += 1
        if not current or j >= len(toks):
            return ""
        params.append(current)
        j += 2 if toks[j] == "\\right" else 1
        if j >= len(toks) or toks[j] != "=" or name in self.names:
            return ""
        self.i = j + 1
        self.functions.add(name)  # it may use itself (which the calculator refuses, by name)
        return f"{name}({','.join(params)})="

    def raw(self, j: int) -> tuple[list[str], int]:
        """The tokens of the TeX argument at j, without its braces, and the index after it."""
        toks = self.toks
        if j >= len(toks):
            return [], j
        if toks[j] != "{":
            return [toks[j]], j + 1
        depth, k = 0, j
        while k < len(toks):
            depth += {"{": 1, "}": -1}.get(toks[k], 0)
            if depth == 0:
                return toks[j + 1:k], k + 1
            k += 1
        return toks[j + 1:], len(toks)

    def term(self) -> str:
        """What a sum, product or d/dx applies to: brackets, or everything up to the next + or −."""
        parts: list[str] = []
        if self.peek() in ("+", "-"):
            parts.append(self.take())
        while (tok := self.peek()) is not None and tok not in TERM_STOP:
            self.item(parts)
        if not parts or parts == ["-"] or parts == ["+"]:
            raise LatexError("Something is missing after it")
        return _join(parts)

    def call_args(self) -> str | None:
        """`(…)` or `\\left(…\\right)` right here, as its inside; None if there is none."""
        if self.peek() == "(":
            self.i += 1
            return self.group(")")
        if self.peek() == "\\left" and self.peek(1) == "(":
            self.i += 1
            parts: list[str] = []
            self.command(parts, "left")
            return parts[0][1:-1]
        return None

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
        if digits and name + digits in self.names:  # log10, log2, atan2
            self.i += len(digits)
            name += digits
        if self.peek() == "_" and name not in self.names:
            self.i += 1
            sub = self.arg()
            if not re.fullmatch(r"\w+", sub):
                raise LatexError("A subscript can only hold letters and digits")
            name += "_" + sub
        primes = self.primes() if self.calculus else 0
        if name in self.names and not primes:
            self.function(out, name)
            return
        called = self.peek() == "(" or self.peek() == "\\left" and self.peek(1) == "("
        if called and self.functions:
            # af(x) is a times f(x): the longest name the user defined that ends it
            own = next((name[k:] for k in range(len(name)) if name[k:] in self.functions), None)
            if own:
                if own != name:
                    out.append(name[:-len(own)])
                out.append(f"{own}{chr(39) * primes}({self.call_args()})")
                return
        out.append(name + "'" * primes)

    def primes(self) -> int:
        """f', f^{\\prime}, f^{\\prime\\prime}, f^{\\prime}^{\\prime}: how many primes follow."""
        count = 0
        while (tok := self.peek()) is not None:
            if tok == "'":
                count += 1
                self.i += 1
            elif tok == "^":
                arg, j = self.raw(self.i + 1)
                if not arg or not all(t in PRIME_TOKENS for t in arg):
                    break
                count += sum(PRIME_TOKENS[t] for t in arg)
                self.i = j
            elif tok in PRIME_TOKENS and tok != "'":
                count += PRIME_TOKENS[tok]
                self.i += 1
            else:
                break
        return count

    def command(self, out: list[str], cmd: str) -> None:
        if cmd in IGNORED:
            return
        if cmd in ("frac", "dfrac", "tfrac", "cfrac"):
            if self.calculus and self.leibniz(out):
                return
            num = self.arg()
            out.append(f"({num})/({self.arg()})")
        elif cmd in ("int", "intop", "intlimits") and self.calculus:
            self.integral(out)
        elif cmd in ("sum", "prod") and self.calculus:
            self.series(out, cmd)
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
            if text in self.names:
                self.function(out, text)
            else:
                out.append(text)
        elif cmd == "placeholder":
            raise LatexError("Fill in the empty box first")
        else:
            raise LatexError(f"The calculator doesn't know \\{cmd}")

    # -- calculus ------------------------------------------------------------------------
    def leibniz(self, out: list[str]) -> bool:
        r"""\frac{d}{dx}, \frac{d^2}{dx^2}, \frac{df}{dx}, \frac{\partial^2 g}{\partial x\partial y}: the
        fraction's two arguments read as a derivative, or False (and nothing read) if they aren't one."""
        num, j = self.raw(self.i)
        den, k = self.raw(j)
        num, den = _plain_tokens(num), _plain_tokens(den)
        if not num or num[0] not in ("d", "\\partial") or not den:
            return False
        rest, order = num[1:], None
        if rest[:1] == ["^"]:
            digits = "".join(t for t in rest[1:] if t.isdigit())
            if not digits or rest[1:1 + len(digits)] != list(digits):
                return False
            order, rest = int(digits), rest[1 + len(digits):]
        if not all(len(t) == 1 and t.isalpha() and t.isascii() or t == "_" or t.isdigit() for t in rest):
            return False
        target = "".join(rest)
        variables: list[str] = []
        d = 0
        while d < len(den):
            if den[d] not in ("d", "\\partial"):
                return False
            d += 1
            name = ""
            while d < len(den) and len(den[d]) == 1 and den[d].isalpha() and den[d].isascii() and den[d] != "d":
                name += den[d]
                d += 1
            if d + 1 < len(den) and den[d] == "_":
                name += "_" + den[d + 1]
                d += 2
            if not name:
                return False
            times = 1
            if d < len(den) and den[d] == "^":
                digits = ""
                d += 1
                while d < len(den) and den[d].isdigit():
                    digits += den[d]
                    d += 1
                if not digits:
                    return False
                times = int(digits)
            variables += [name] * times
        if not variables or order is not None and order != len(variables) or order is None and len(variables) != 1:
            return False
        self.i = k
        names = ", ".join(variables)
        if target:  # df/dx: f's derivative, at the values that follow if any
            args = self.call_args()
            out.append(f"diff({target}, {names})" + (f"({args})" if args is not None else ""))
        else:
            inner = self.call_args()
            out.append(f"diff({inner if inner is not None else self.term()}, {names})")
        return True

    def skip_ignored(self) -> None:
        while (tok := self.peek()) is not None and tok in IGNORED_TOKENS:
            self.i += 1

    def integral(self, out: list[str]) -> None:
        lo = hi = None
        self.skip_ignored()
        while self.peek() in ("_", "^"):
            if self.take() == "_":
                lo = self.arg()
            else:
                hi = self.arg()
        if lo is None or hi is None:
            raise LatexError("Give the integral its limits, as in ∫₀¹ x² dx")
        j, depth = self.i, 0
        while j < len(self.toks):
            tok = self.toks[j]
            if depth == 0 and (end := self.differential(j)):
                body, var = self.toks[self.i:j], end[0]
                if not body:
                    raise LatexError("The integral has nothing to integrate")
                out.append(f"integral({self.sub(body)}, {var}, {lo}, {hi})")
                self.i = end[1]
                return
            depth += {"{": 1, "(": 1, "[": 1, "\\left": 1, "}": -1, ")": -1, "]": -1, "\\right": -1}.get(tok, 0)
            if depth < 0:
                break
            j += 1
        raise LatexError("End the integral with its variable, as in dx")

    def differential(self, j: int) -> tuple[str, int] | None:
        """(variable, index after it) if a differential (dx, \\mathrm{d}x, \\differentialD x) starts at j
        and ends the integral."""
        toks = self.toks
        if toks[j] in ("d", "\\differentialD"):
            k = j + 1
        elif toks[j] == "\\mathrm" and toks[j + 1:j + 4] == ["{", "d", "}"]:
            k = j + 4
        else:
            return None
        while k < len(toks) and toks[k] in ("\\,", "\\ ", "\\;", "\\:", "\\!"):
            k += 1
        if k >= len(toks) or not (len(toks[k]) == 1 and toks[k].isalpha() and toks[k].isascii()):
            return None
        var, k = toks[k], k + 1
        if k + 1 < len(toks) and toks[k] == "_":
            sub, k = self.raw(k + 1)
            var += "_" + "".join(sub)
        if k < len(toks) and toks[k] not in TERM_STOP and toks[k] not in IGNORED_TOKENS:
            return None
        return var, k

    def series(self, out: list[str], cmd: str) -> None:
        sub = upper = None
        self.skip_ignored()
        while self.peek() in ("_", "^"):
            if self.take() == "_":
                sub, self.i = self.raw(self.i)
            else:
                upper = self.arg()
        if not sub or "=" not in sub or upper is None:
            raise LatexError(f"Give the {'sum' if cmd == 'sum' else 'product'} its limits, as in n=1 below and 10 above")
        eq = sub.index("=")
        var = "".join(sub[:eq])
        if not re.fullmatch(r"[A-Za-z]\w*", var):
            raise LatexError("The variable below the sign must be a name, as in n=1")
        lower = self.sub(sub[eq + 1:])
        name = "sum" if cmd == "sum" else "prod"
        out.append(f"{name}({self.term()}, {var}, {lower}, {upper})")

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
IGNORED_TOKENS = {"\\" + c for c in IGNORED}


def _plain_tokens(toks: list[str]) -> list[str]:
    """A fraction argument's tokens without braces, spacing and \\mathrm/\\operatorname wrappers."""
    return [("d" if t == "\\differentialD" else t) for t in toks
            if t not in ("{", "}") and t not in NAME_COMMANDS_TOKENS and t not in IGNORED_TOKENS]


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
