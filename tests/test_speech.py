import pytest

from study_reader.speech_text import (
    MAX_CHARS, Utterance, align_tokens, ends_sentence, math_symbols, reply_utterances, sentence_ends,
    speak_text, speak_word, tex_to_speech,
)


@pytest.mark.parametrize("tex, spoken", [
    (r"\frac{a}{b}", "a over b"),
    (r"\dfrac{a+b}{c}", "the fraction a plus b over c"),
    (r"\frac12", "1 over 2"),
    (r"x^2 + y^3 = z^n", "x squared plus y cubed equals z to the n"),
    (r"x^{n+1}", "x to the power of n plus 1"),
    (r"A^T A^{-1} f'", "A transpose A inverse f prime"),
    (r"x_1 + x_{ij} + a_{n+1}", "x 1 plus x sub i j plus a sub n plus 1"),
    (r"\sqrt{x} + \sqrt[3]{y} + \sqrt[n]{z}", "the square root of x plus the cube root of y plus the n-th root of z"),
    (r"\alpha + \Gamma + \varepsilon", "alpha plus capital gamma plus epsilon"),
    (r"a \le b \neq c > d \approx e", "a is less than or equal to b is not equal to c is greater than d is approximately e"),
    (r"\sum_{i=1}^{n} x_i", "the sum from i equals 1 to n of x i"),
    (r"\int_a^b f(x)\,dx", "the integral from a to b of f of x d x"),
    (r"\lim_{x\to 0} f(x)", "the limit as x goes to 0 of f of x"),
    (r"f: A \to B", "f A to B"),
    (r"x \in \mathbb{R}", "x in the reals"),
    (r"\text{if } x > 0", "if x is greater than 0"),
    (r"\foo{xy}", "x y"),
    (r"\begin{pmatrix} 1 & 2 \\ 3 & 4 \end{pmatrix}", "the matrix 1, 2; 3, 4"),
    (r"\left| x \right| + \|v\|", "the absolute value of x plus the norm of v"),
    (r"P(A \mid B) = P(A | B)", "P of A given B equals P of A given B"),
    (r"\hat{x} + \vec{v}", "x hat plus vector v"),
    (r"\binom{n}{k}", "n choose k"),
    (r"\log_2 n + \ln x + n!", "log base 2 n plus natural log x plus n factorial"),
    (r"\max_{x} f(x)", "the max over x of f of x"),
    (r"\begin{cases} 1 & x>0 \\ 0 & \text{otherwise}\end{cases}", "1 x is greater than 0, 0 otherwise"),
    (r"123 + 4.5", "123 plus 4.5"),
])
def test_tex_to_speech(tex, spoken):
    assert tex_to_speech(tex) == spoken


@pytest.mark.parametrize("tex", [r"\frac{a", "}}^", r"\sqrt[", r"\begin{pmatrix} 1 &", "|", r"\left", "_^{", "\\"])
def test_tex_to_speech_never_raises(tex):
    assert isinstance(tex_to_speech(tex), str)


def test_math_symbols():
    assert math_symbols("x² ≤ α − β") == "x squared is less than or equal to alpha minus beta"
    assert math_symbols("a≤b, x₁") == "a is less than or equal to b, x 1"
    assert math_symbols("10⁻³ and Γ") == "10 to the minus 3 and capital gamma"
    plain = "Nothing (to) see here."
    assert math_symbols(plain) is plain


@pytest.mark.parametrize("word, spoken", [
    ("e.g.,", "for example,"), ("i.e.", "that is"), ("Fig.", "Figure"), ("figs.", "Figures"), ("Eq.", "Equation"),
    ("(cf.", "(compare"), ("pp.", "pages"), ("§3.2", "section 3.2"), ("§§", "sections"), ("ﬁnd", "find"),
    ("hy­phen", "hyphen"), ("note↩", "note"), ("https://www.example.com/a/b?q=1", "example.com link"),
    ("(www.example.com).", "(example.com link)."), ("[a](b)", "[a] (b)"), ("x²", "x squared"),
])
def test_speak_word(word, spoken):
    assert speak_word(word) == spoken


@pytest.mark.parametrize("word, nxt, ends", [
    ("e.g.", "The", False), ("i.e.", "A", False), ("Fig.", "2", False), ("Dr.", "Smith", False),
    ("al.", "2020", False), ("3.14", "is", False), ("J.", "R.", False), ("R.", "Tolkien", False),
    ('"Stop."', "He", True), ("home.", "Then", True), ("etc.", "and", False), ("etc.", "The", True),
    # "?" and "!" end even after an abbreviation, but not before a lowercase word.
    ("Wait?", "no", False), ("Wait?", "No", True), ("etc.?", "So", True),
    ("so…", "Then", True), ("so…", "then", False), ("follows:", "First", False), ("end.", None, True),
    ("end", None, False),
])
def test_ends_sentence(word, nxt, ends):
    assert ends_sentence(word, nxt) is ends


def test_sentence_ends():
    words = "See Fig. 2 by Dr. Smith et al. 2020. J. R. R. Tolkien wrote it. Done".split()
    assert sentence_ends(words) == [9, 15, 16]
    assert sentence_ends([]) == []


def test_sentence_ends_caps_long_sentences():
    words = ["alpha,"] * 40 + ["beta"] * 100
    ends = sentence_ends(words)
    assert ends[-1] == len(words)
    starts = [0] + ends[:-1]
    lengths = [len(" ".join(words[a:b])) for a, b in zip(starts, ends)]
    assert all(n <= MAX_CHARS for n in lengths)
    assert words[ends[0] - 1].endswith(",") and lengths[0] > 250


def test_speak_text():
    assert speak_text(r"So $x^2$ and $$\frac{a}{b}$$, or \(y\).") == "So x squared and a over b, or y."
    assert speak_text("It costs $5 and $10 (e.g. lunch).") == "It costs $5 and $10 (for example lunch)."


def test_reply_utterances():
    utts = reply_utterances([(0, "A heading"), (1, "  "), (2, "First one. Then $x^2$!"), (3, "List item:")])
    assert [(u.block, u.text, u.pause) for u in utts] == [
        (0, "A heading.", 0.35), (2, "First one.", 0.12), (2, "Then x squared!", 0.35), (3, "List item:", 0.35)]
    assert all(not u.marks and not u.rects for u in utts)


def test_align_tokens():
    text = "Hello, world. It's 3 o'clock."
    tokens = [("Hello", 0.0, 0.4), (",", 0.4, 0.5), ("world", 0.5, 0.9), ("zebra", 0.9, 1.0), (".", 1.0, 1.1),
              ("it's", 1.2, 1.4), ("3", None, 1.6), ("o'clock", 1.6, 2.0), ("  ", 2.0, 2.1)]
    assert align_tokens(text, tokens, t0=10.0) == [
        (10.0, 10.4, 0, 5), (10.5, 10.9, 7, 12), (11.2, 11.4, 14, 18), (11.6, 12.0, 21, 28)]
    assert align_tokens("a b", [("far", 0.0, 1.0)]) == []


def test_utterance_word_at_and_covers():
    r1, r2 = {3: [(0.0, 0.0, 1.0, 1.0)]}, {4: [(1.0, 1.0, 2.0, 2.0)]}
    u = Utterance("one two", first=(3, 5), last=(4, 1), marks=[(0, 3, r1), (4, 7, r2)])
    assert u.word_at(0) is r1 and u.word_at(3) is r1 and u.word_at(5) is r2 and u.word_at(99) is r2
    assert Utterance("x", marks=[(2, 3, r1)]).word_at(0) is None
    assert u.covers(3, 5) and u.covers(3, 900) and u.covers(4, 1)
    assert not u.covers(3, 4) and not u.covers(4, 2)
    assert not Utterance("reply", block=0).covers(0, 0)
