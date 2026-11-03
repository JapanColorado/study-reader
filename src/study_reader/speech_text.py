"""Spoken text for read-aloud: TeX and Unicode math said in words, abbreviations expanded, text split
into sentences, and the TTS engine's timed tokens mapped back onto what was spoken. Qt-free.

Book text is split by the reader with `sentence_ends` over a page's words, each word spoken through
`speak_word`; a chat reply's blocks become sentences through `reply_utterances`.
"""

from __future__ import annotations

import re
import unicodedata
from bisect import bisect_right
from dataclasses import dataclass, field
from typing import Iterable
from urllib.parse import urlsplit

Rect = tuple[float, float, float, float]

# Sentences longer than SOFT_CHARS end at the next clause break; none runs past MAX_CHARS.
MAX_CHARS = 350
SOFT_CHARS = 250
SPLIT_AFTER = 30  # Utterance.split: the shortest first part, and the shortest rest

ABBREVIATIONS = {
    "e.g.", "i.e.", "etc.", "vs.", "cf.", "fig.", "figs.", "eq.", "eqs.", "dr.", "mr.", "mrs.", "ms.", "prof.",
    "st.", "no.", "vol.", "pp.", "p.", "ch.", "sec.", "al.", "approx.", "jr.", "sr.", "inc.", "ltd.", "ed.",
    "eds.", "resp.", "viz.", "ca.", "c.", "thm.", "def.", "prop.", "lem.", "cor.", "ex.",
}
SPOKEN = {
    "e.g.": "for example", "i.e.": "that is", "fig.": "Figure", "figs.": "Figures", "eq.": "Equation",
    "eqs.": "Equations", "cf.": "compare", "pp.": "pages", "vs.": "versus", "etc.": "et cetera",
    "approx.": "approximately", "ch.": "Chapter", "sec.": "Section", "thm.": "Theorem", "def.": "Definition",
    "prop.": "Proposition", "lem.": "Lemma", "cor.": "Corollary", "resp.": "respectively", "viz.": "namely",
    "w.r.t.": "with respect to", "vol.": "Volume",
}

_GREEK = ("alpha beta gamma delta epsilon zeta eta theta iota kappa lambda mu nu xi omicron pi rho sigma tau "
          "upsilon phi chi psi omega").split()
_BLACKBOARD = {"R": "the reals", "N": "the naturals", "Z": "the integers", "Q": "the rationals",
               "C": "the complex numbers", "E": "the expectation of", "P": "the probability of", "1": "the indicator"}

_UNICODE_MATH = {
    "≤": "is less than or equal to", "≥": "is greater than or equal to", "≠": "is not equal to",
    "≈": "is approximately", "±": "plus or minus", "×": "times", "÷": "divided by", "·": "times", "⋅": "times",
    "−": "minus", "∞": "infinity", "∑": "the sum of", "∏": "the product of", "∫": "the integral of",
    "√": "the square root of", "∂": "partial", "∇": "del", "∀": "for all", "∃": "there exists", "∈": "in",
    "∉": "not in", "⊂": "is a subset of", "⊆": "is a subset of", "∪": "union", "∩": "intersect", "→": "to",
    "⇒": "implies", "⇔": "if and only if", "↦": "maps to", "∝": "is proportional to", "≡": "is equivalent to",
    "⊥": "is perpendicular to", "∘": "composed with", "′": "prime", "″": "double prime", "≪": "is much less than",
    "≫": "is much greater than", "∅": "the empty set", "ℝ": "the reals", "ℕ": "the naturals",
    "ℤ": "the integers", "ℚ": "the rationals", "ℂ": "the complex numbers",
}
for _i, _name in enumerate(_GREEK[:17] + ["sigma"] + _GREEK[17:]):
    _UNICODE_MATH[chr(0x3B1 + _i)] = _name
    if _i != 17:
        _UNICODE_MATH[chr(0x391 + _i)] = "capital " + _name
_UNICODE_MATH.update({"ϵ": "epsilon", "ϕ": "phi", "ϑ": "theta", "ϖ": "pi", "ϱ": "rho"})
_SUPERSCRIPTS = str.maketrans("⁰¹²³⁴⁵⁶⁷⁸⁹⁻⁺ⁿⁱ", "0123456789-+ni")
_SUBSCRIPTS = str.maketrans("₀₁₂₃₄₅₆₇₈₉₋₊ₙᵢⱼₖ", "0123456789-+nijk")
_MATH_CHARS = re.compile("([⁰¹²³⁴⁵⁶⁷⁸⁹⁻⁺ⁿⁱ]+)|([₀₁₂₃₄₅₆₇₈₉₋₊ₙᵢⱼₖ]+)|([" + "".join(_UNICODE_MATH) + "])")

_TEX_WORDS = {
    "neq": "is not equal to", "ne": "is not equal to", "le": "is less than or equal to",
    "leq": "is less than or equal to", "leqslant": "is less than or equal to", "ge": "is greater than or equal to",
    "geq": "is greater than or equal to", "geqslant": "is greater than or equal to", "ll": "is much less than",
    "gg": "is much greater than", "approx": "is approximately", "simeq": "is approximately",
    "sim": "is distributed as", "cong": "is congruent to", "propto": "is proportional to",
    "equiv": "is equivalent to", "in": "in", "notin": "not in", "ni": "contains", "subset": "is a subset of",
    "subseteq": "is a subset of", "supset": "is a superset of", "supseteq": "is a superset of", "cup": "union",
    "cap": "intersect", "setminus": "minus", "emptyset": "the empty set", "varnothing": "the empty set",
    "to": "to", "rightarrow": "to", "longrightarrow": "to", "Rightarrow": "implies", "implies": "implies",
    "Longrightarrow": "implies", "iff": "if and only if", "Leftrightarrow": "if and only if",
    "Longleftrightarrow": "if and only if", "mapsto": "maps to", "infty": "infinity", "partial": "partial",
    "nabla": "del", "forall": "for all", "exists": "there exists", "neg": "not", "lnot": "not", "land": "and",
    "wedge": "and", "lor": "or", "vee": "or", "pm": "plus or minus", "mp": "minus or plus", "cdot": "times",
    "times": "times", "div": "divided by", "ast": "star", "star": "star", "circ": "composed with",
    "perp": "is perpendicular to", "parallel": "is parallel to", "mid": "given", "dots": "and so on",
    "ldots": "and so on", "cdots": "and so on", "vdots": "and so on", "ddots": "and so on", "ell": "ell",
    "hbar": "h bar", "dagger": "dagger", "prime": "prime", "top": "transpose", "angle": "angle", "%": "percent",
    "$": "dollars", "&": "and", "#": "number", "log": "log", "ln": "natural log", "exp": "the exponential of",
    "sin": "sine", "cos": "cosine", "tan": "tangent", "sec": "secant", "csc": "cosecant", "cot": "cotangent",
    "arcsin": "arc sine", "arccos": "arc cosine", "arctan": "arc tangent", "sinh": "hyperbolic sine",
    "cosh": "hyperbolic cosine", "tanh": "hyperbolic tangent", "det": "the determinant of", "arg": "arg",
    "Pr": "the probability of", "E": "the expectation of", "Var": "the variance of", "Cov": "the covariance of",
    "gcd": "the g c d of", "deg": "degree", "dim": "dimension", "ker": "kernel", "mod": "mod", "bmod": "mod",
    "pmod": "mod",
}
for _name in _GREEK:
    _TEX_WORDS[_name] = _name
    _TEX_WORDS[_name.capitalize()] = "capital " + _name
_TEX_WORDS.update({f"var{n}": n for n in ("epsilon", "theta", "phi", "pi", "rho", "sigma", "kappa")})
_TEX_CHARS = {"+": "plus", "-": "minus", "=": "equals", "<": "is less than", ">": "is greater than", "/": "over",
              "*": "times", "!": "factorial", ",": ",", ";": ","}
_DROP = {
    "left", "right", "middle", "big", "Big", "bigg", "Bigg", "bigl", "bigr", "Bigl", "Bigr", "biggl", "biggr",
    "Biggl", "Biggr", ",", ";", "!", ":", " ", "quad", "qquad", "displaystyle", "textstyle", "scriptstyle",
    "limits", "nolimits", "langle", "rangle", "{", "}", "lbrace", "rbrace", "lbrack", "rbrack", "lfloor",
    "rfloor", "lceil", "rceil", "rvert", "rVert", "nonumber", "notag", "colon", "\n",
}
_SKIP_ARG = {"label", "tag", "hspace", "vspace", "phantom", "color", "end"}
_FRAC = {"frac", "dfrac", "tfrac", "cfrac"}
_BINOM = {"binom", "dbinom", "tbinom"}
_ACCENTS = {"hat": "hat", "widehat": "hat", "bar": "bar", "overline": "bar", "tilde": "tilde", "widetilde": "tilde",
            "dot": "dot", "ddot": "double dot", "check": "check", "breve": "breve"}
_BEFORE = {"vec": "vector", "overrightarrow": "vector"}
_FONTS = {"mathbf", "mathit", "mathcal", "mathsf", "boldsymbol", "bm", "mathscr", "mathfrak", "mathnormal", "pmb",
          "underline"}
_TEXT = {"text", "textbf", "textit", "textrm", "textsf", "texttt", "mathrm", "operatorname", "mbox", "hbox", "emph",
         "textnormal"}
_BIG = {"sum": "the sum", "prod": "the product", "coprod": "the coproduct", "int": "the integral",
        "iint": "the double integral", "iiint": "the triple integral", "oint": "the contour integral",
        "bigcup": "the union", "bigcap": "the intersection"}
_OVER = {"max": "the max", "min": "the min", "sup": "the supremum", "inf": "the infimum", "argmax": "the arg max",
         "argmin": "the arg min"}
_LIM = {"lim": "the limit", "limsup": "the limit superior", "liminf": "the limit inferior"}
_MATRICES = {"matrix": "the matrix", "pmatrix": "the matrix", "bmatrix": "the matrix", "Bmatrix": "the matrix",
             "smallmatrix": "the matrix", "vmatrix": "the determinant of the matrix",
             "Vmatrix": "the norm of the matrix"}
_POWERS = {"2": "squared", "3": "cubed", "T": "transpose", "\\top": "transpose", "\\intercal": "transpose",
           "-1": "inverse", "\\prime": "prime", "\\prime\\prime": "double prime", "*": "star", "\\ast": "star",
           "\\star": "star", "\\circ": "degrees", "\\dagger": "dagger"}
_ARITY = {**dict.fromkeys(_FRAC | _BINOM, 2), **dict.fromkeys(
    {"sqrt", "mathbb", *_ACCENTS, *_BEFORE, *_FONTS, *_TEXT, *_SKIP_ARG}, 1)}
_PRIMES = {1: "prime", 2: "double prime", 3: "triple prime"}
_TOKEN = re.compile(r"\\[A-Za-z]+|\\.|\d+(?:\.\d+)?|\s+|.", re.DOTALL)


def _tidy(s: str) -> str:
    s = re.sub(r"\s+", " ", s)
    s = re.sub(r"\s+([,;])", r"\1", s)
    return re.sub(r"([,;])(?:\s*[,;])+", r"\1", s).strip(" ,;")


_FUNCTION_LETTERS = set("PEFGHfghpq")  # letters that, before a parenthesis, are usually applied


class _Tex:
    """Reads tokens one argument at a time, saying each piece in words."""

    def __init__(self, toks: list[str], to_word: str = "to", matrix: bool = False):
        self.toks, self.pos, self.to_word, self.matrix = toks, 0, to_word, matrix

    def read(self) -> str:
        out = []
        while self.pos < len(self.toks):
            self.pos += 1
            out.append(self.atom(self.toks[self.pos - 1]))
        return " ".join(w for w in out if w)

    def speak(self, toks: list[str], to_word: str | None = None) -> str:
        return _Tex(list(toks), to_word or self.to_word, self.matrix).read()

    def peek(self) -> str:
        while self.pos < len(self.toks) and self.toks[self.pos].isspace():
            self.pos += 1
        return self.toks[self.pos] if self.pos < len(self.toks) else ""

    def arg(self) -> list[str]:
        t = self.peek()
        if not t:
            return []
        self.pos += 1
        if t == "{":
            depth, start = 1, self.pos
            while self.pos < len(self.toks):
                self.pos += 1
                u = self.toks[self.pos - 1]
                depth += (u == "{") - (u == "}")
                if not depth:
                    return self.toks[start:self.pos - 1]
            return self.toks[start:]
        if t == "}":
            return []
        if t.isdigit() and len(t) > 1:
            self.pos -= 1
            self.toks[self.pos] = t[1:]
            return [t[0]]
        if t.startswith("\\"):
            out = [t]
            if t == "\\sqrt" and (opt := self.optional()) is not None:
                out += ["[", *opt, "]"]
            for _ in range(_ARITY.get(t[1:], 0)):
                out += ["{", *self.arg(), "}"]
            return out
        return [t]

    def optional(self) -> list[str] | None:
        if self.peek() != "[":
            return None
        depth, start = 0, self.pos + 1
        for j in range(start, len(self.toks)):
            depth += (self.toks[j] == "{") - (self.toks[j] == "}")
            if depth == 0 and self.toks[j] == "]":
                self.pos = j + 1
                return self.toks[start:j]
        return None

    def raw(self, toks: list[str]) -> str:
        return "".join(f" {self.speak([t])} " if t.startswith("\\") else t for t in toks if t not in ("{", "}", "$"))

    def limits(self) -> tuple[list[str] | None, list[str] | None]:
        lo = hi = None
        while True:
            t = self.peek()
            if t in ("\\limits", "\\nolimits"):
                self.pos += 1
            elif t == "_" and lo is None:
                self.pos += 1
                lo = self.arg()
            elif t == "^" and hi is None:
                self.pos += 1
                hi = self.arg()
            else:
                return lo, hi

    def atom(self, t: str) -> str:
        if t.isspace():
            return ""
        if t == "{":
            self.pos -= 1
            return self.speak(self.arg())
        if t == "^":
            return self.power(self.arg())
        if t == "_":
            toks = [u for u in self.arg() if not u.isspace()]
            return self.speak(toks) if len(toks) == 1 else "sub " + self.speak(toks)
        if t == "'":
            n = 1
            while self.pos < len(self.toks) and self.toks[self.pos] == "'":
                n, self.pos = n + 1, self.pos + 1
            return _PRIMES.get(n, "prime")
        if t == "&":
            return "," if self.matrix else ""
        if t == "\\\\":
            return ";" if self.matrix else ","
        if t in ("|", "\\vert", "\\lvert"):
            return self.bars({"|", "\\vert", "\\rvert"}, "the absolute value of", "given" if t != "\\lvert" else "")
        if t in ("\\|", "\\Vert", "\\lVert"):
            return self.bars({"\\|", "\\Vert", "\\rVert"}, "the norm of", "")
        if t.startswith("\\") and len(t) > 1:
            return self.command(t[1:])
        if t in _TEX_CHARS:
            return _TEX_CHARS[t]
        if (named := math_symbols(t)) != t:
            return named.strip()
        if t in _FUNCTION_LETTERS and self.peek() == "(":
            return f"{t} of"  # P(A), f(x)
        return t if t[0].isalnum() else ""

    def bars(self, closers: set[str], phrase: str, unmatched: str) -> str:
        depth = 0
        for j in range(self.pos, len(self.toks)):
            u = self.toks[j]
            depth += (u == "{") - (u == "}")
            if depth < 0:
                break
            if depth == 0 and u in closers:
                inner, self.pos = self.toks[self.pos:j], j + 1
                return f"{phrase} {self.speak(inner)}"
        return unmatched

    def power(self, toks: list[str]) -> str:
        toks = [u for u in toks if not u.isspace()]
        key = "".join(toks)
        if key in _POWERS:
            return _POWERS[key]
        if len(toks) == 1 and toks[0][0].isalnum():
            return "to the " + toks[0]
        return "to the power of " + self.speak(toks)

    def command(self, name: str) -> str:
        if name in _DROP:
            return ""
        if name in _SKIP_ARG:
            self.arg()
            return ""
        if name in _FRAC:
            n, d = self.speak(self.arg()), self.speak(self.arg())
            return f"{'the fraction ' if ' ' in n else ''}{n} over {d}"
        if name in _BINOM:
            n, k = self.speak(self.arg()), self.speak(self.arg())
            return f"{n} choose {k}"
        if name == "sqrt":
            index = self.optional()
            body = self.speak(self.arg())
            n = self.speak(index) if index else "2"
            root = {"2": "square", "3": "cube"}.get(n) or (f"{n}th" if n.isdigit() else f"{n}-th")
            return f"the {root} root of {body}"
        if name in _ACCENTS:
            return f"{self.speak(self.arg())} {_ACCENTS[name]}"
        if name in _BEFORE:
            return f"{_BEFORE[name]} {self.speak(self.arg())}"
        if name in _FONTS:
            return self.speak(self.arg())
        if name == "mathbb":
            letter = self.raw(self.arg()).strip()
            return _BLACKBOARD.get(letter, letter)
        if name in _TEXT:
            if self.peek() == "*":
                self.pos += 1
            text = " ".join(self.raw(self.arg()).split())
            key = text.replace(" ", "")
            if key in _OVER:
                return self.over(_OVER[key])
            return _TEX_WORDS.get(text, text) if name == "operatorname" else text
        if name in _BIG:
            lo, hi = self.limits()
            if lo and hi:
                return f"{_BIG[name]} from {self.speak(lo)} to {self.speak(hi)} of"
            if lo or hi:
                return f"{_BIG[name]} {'over' if lo else 'to'} {self.speak(lo or hi)} of"
            return f"{_BIG[name]} of"
        if name in _LIM:
            lo, _ = self.limits()
            return f"{_LIM[name]} as {self.speak(lo, 'goes to')} of" if lo else f"{_LIM[name]} of"
        if name in _OVER:
            return self.over(_OVER[name])
        if name == "log" and self.peek() == "_":
            self.pos += 1
            return f"log base {self.speak(self.arg())}"
        if name == "not":
            t = self.peek()
            self.pos += 1
            if t == "=":
                return "is not equal to"
            word = self.atom(t) if t else ""
            return word.replace("is ", "is not ", 1) if word.startswith("is ") else f"not {word}"
        if name in ("to", "rightarrow"):
            return self.to_word
        if name == "begin":
            return self.environment()
        return _TEX_WORDS.get(name, "")

    def over(self, phrase: str) -> str:
        lo, _ = self.limits() if self.peek() == "_" else (None, None)
        return f"{phrase} over {self.speak(lo)} of" if lo else f"{phrase} of"

    def environment(self) -> str:
        env = self.raw(self.arg()).strip().rstrip("*")
        if env == "array":
            self.arg()
        depth, start, j = 1, self.pos, self.pos
        while j < len(self.toks):
            depth += (self.toks[j] == "\\begin") - (self.toks[j] == "\\end")
            if not depth:
                break
            j += 1
        inner, self.pos = self.toks[start:j], min(j + 1, len(self.toks))
        if j < len(self.toks):
            self.arg()
        body = _tidy(_Tex(inner, self.to_word, env in _MATRICES).read())
        return f"{_MATRICES[env]} {body}" if env in _MATRICES else body


def tex_to_speech(tex: str) -> str:
    """TeX math said in words ("\\frac{a}{b}" -> "a over b"). Never raises."""
    try:
        return _tidy(_Tex(_TOKEN.findall(tex)).read())
    except Exception:
        return _tidy(re.sub(r"[\\{}^_$&]", " ", tex))


def math_symbols(text: str) -> str:
    """Unicode math in text (x² ≤ α) named in words; other text is returned as it is."""
    if not _MATH_CHARS.search(text):
        return text

    def name(m: re.Match) -> str:
        if m.group(1):
            n = m.group(1).translate(_SUPERSCRIPTS)
            word = _POWERS.get(n) if n in ("2", "3", "-1") else "to the " + n.replace("-", "minus ").replace("+", "plus ")
        elif m.group(2):
            word = m.group(2).translate(_SUBSCRIPTS)
        else:
            word = _UNICODE_MATH[m.group(3)]
        return f" {word} "

    s = re.sub(r" {2,}", " ", _MATH_CHARS.sub(name, text))
    s = re.sub(r"([(\[]) | ([,.;:!?)\]])", r"\1\2", s)
    if not text[:1].isspace():
        s = s.lstrip(" ")
    return s if text[-1:].isspace() else s.rstrip(" ")


_URL = re.compile(r"(?:https?://|www\.)\S+", re.I)
_WORD_PARTS = re.compile(r"^([(\[\"'“‘«]*)(.*?)([,;:)\]\"'”’»]*)$", re.DOTALL)
# Soft hyphens, back-reference arrows, variation selectors, and bullets and boxes (read as
# "black square" otherwise).
_JUNK = dict.fromkeys(map(ord, "­↩↑⤴︎️■□▪▫●○•◦◆◇▸▹►▶✓✔✗✘❑❒➢➤"))


def speak_word(text: str) -> str:
    """A word of book text as it should be said: ligatures undone, abbreviations and URLs expanded,
    math named, and nothing misaki would take for its `[text](/phonemes/)` markup."""
    text = text.translate(_JUNK)
    if any(c.isspace() for c in text):
        return " ".join(w for w in map(speak_word, text.split()) if w)
    lead, core, trail = _WORD_PARTS.match(text).groups()
    if _URL.fullmatch(core):
        stripped = core.rstrip(".!?,;:)]\"'”’»")
        trail = core[len(stripped):] + trail
        host = urlsplit(stripped if "://" in stripped else "http://" + stripped).hostname or stripped
        core = f"{host.removeprefix('www.')} link"
    elif core.lower() in SPOKEN:
        core = SPOKEN[core.lower()]
    elif core.startswith("§"):
        core = re.sub(r"^§§\s*", "sections ", re.sub(r"^§(?!§)\s*", "section ", core)).strip()
    s = unicodedata.normalize("NFKC", math_symbols(lead + core + trail)).replace("⁄", "/")
    return s.replace("](", "] (")


_END = re.compile(r"([.!?…]+)[\"'”’)\]»]*$")
_OPENERS = "\"'“‘([«¿¡"


def ends_sentence(word: str, next_word: str | None) -> bool:
    """Whether a sentence ends after `word`. Not before a lowercase word (even after "?" or "!"), nor
    after an abbreviation or initial; "etc." ends one only before a capital."""
    m = _END.search(word)
    if not m:
        return False
    if next_word is None:
        return True
    nxt = next_word.lstrip(_OPENERS)
    if nxt[:1].islower():
        return False
    if "?" in m.group(1) or "!" in m.group(1):
        return True
    core = word[:m.end(1)].lstrip(_OPENERS).lower()
    if core == "etc.":
        return nxt[:1].isupper()
    return not (core in ABBREVIATIONS or re.fullmatch(r"(?:[^\W\d_]\.)+", core))


def sentence_ends(words: list[str]) -> list[int]:
    """Exclusive end indices of the sentences in `words`, long ones cut at clause breaks."""
    ends, run = [], 0
    for i, w in enumerate(words):
        run += len(w) + 1
        nxt = words[i + 1] if i + 1 < len(words) else None
        if (nxt is None or ends_sentence(w, nxt)
                or (run > SOFT_CHARS and w.rstrip("\"'”’)]»")[-1:] in (";", ":", "—", "–", ","))
                or run + len(nxt) > MAX_CHARS):
            ends.append(i + 1)
            run = 0
    return ends


_TEX_MATH = re.compile(r"\$\$(.+?)\$\$|\\\[(.+?)\\\]|\\\((.+?)\\\)|(?<![\\$])\$(?=[^\s$])([^$]+?)(?<=\S)\$(?!\d)",
                       re.DOTALL)


def speak_text(text: str) -> str:
    """A reply block's plain text, with its TeX math, as it should be said."""
    out, pos = [], 0
    for m in _TEX_MATH.finditer(text):
        out += [speak_word(text[pos:m.start()].replace("\\$", "$")), tex_to_speech(next(g for g in m.groups() if g))]
        pos = m.end()
    out.append(speak_word(text[pos:].replace("\\$", "$")))
    return re.sub(r"\s+([,.;:!?])", r"\1", " ".join(p for p in out if p)).strip()


@dataclass
class Utterance:
    """One sentence to say, with where it sits in the book (or reply) and its words' highlights."""

    text: str
    first: tuple[int, int] = (-1, -1)
    last: tuple[int, int] = (-1, -1)
    rects: dict[int, list[Rect]] = field(default_factory=dict)
    marks: list[tuple[int, int, dict[int, list[Rect]]]] = field(default_factory=list)
    pause: float = 0.12
    block: int = -1

    def word_at(self, char: int) -> dict[int, list[Rect]] | None:
        """Highlight of the word being said at `char` of `text`: the last mark starting at or before it."""
        i = bisect_right([m[0] for m in self.marks], char) - 1
        return self.marks[i][2] if i >= 0 else None

    def covers(self, page: int, idx: int) -> bool:
        return self.first <= (page, idx) <= self.last

    def split(self, after: int = SPLIT_AFTER) -> list[Utterance]:
        """The sentence in pieces at its commas (each at least ``after`` characters, with as
        much left after it), so speaking can start before the whole sentence is made; the
        pieces keep the sentence's highlight."""
        k = self.text.find(", ", after)
        if k < 0 or len(self.text) - k < SPLIT_AFTER:
            return [self]
        k += 1
        head = Utterance(self.text[:k], self.first, self.last, self.rects,
                         [m for m in self.marks if m[0] < k], 0.0, self.block)
        tail = Utterance(self.text[k + 1:], self.first, self.last, self.rects,
                         [(a - k - 1, b - k - 1, r) for a, b, r in self.marks if a > k], self.pause, self.block)
        return [head, *tail.split(after)]


def reply_utterances(blocks: list[tuple[int, str]]) -> list[Utterance]:
    """Sentences of a reply's blocks, given as (block index, plain text); headings and list items
    without closing punctuation get a period, and each block ends with a longer pause."""
    out = []
    for n, text in blocks:
        words = speak_text(text).split()
        if not words:
            continue
        start, block = 0, []
        for end in sentence_ends(words):
            block.append(Utterance(" ".join(words[start:end]), block=n))
            start = end
        if not _END.search(block[-1].text) and not block[-1].text.endswith((":", ";")):
            block[-1].text += "."
        block[-1].pause = 0.35
        out += block
    return out


def align_tokens(text: str, tokens: Iterable, t0: float = 0.0) -> list[tuple[float, float, int, int]]:
    """(start, end, char start, char end) of each timed token found in `text`, in order; tokens with
    no times, punctuation, or no match near the cursor are left out."""
    out, cursor, lower = [], 0, text.lower()
    for tok, start, end in tokens:
        tok = (tok or "").strip()
        if not tok or start is None or end is None or not any(c.isalnum() for c in tok):
            continue
        limit = cursor + 40 + len(tok)
        i = text.find(tok, cursor, limit)
        if i < 0 and len(lower) == len(text):
            i = lower.find(tok.lower(), cursor, limit)
        if i >= 0:
            out.append((t0 + start, t0 + end, i, i + len(tok)))
            cursor = i + len(tok)
    return out
