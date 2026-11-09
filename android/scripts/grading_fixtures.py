"""Write core/src/test/resources/grading_cases.json: the desktop's own answers for quiz grading, so the
Kotlin port (core/.../Grading.kt, CalcLatex.kt, Calculator.kt) can be checked against them.

Run with the desktop's Python environment, from the repository root:

    PYTHONPATH=src .pixi/envs/default/bin/python android/scripts/grading_fixtures.py [out.json]

It records, for MathLive-style LaTeX, latex_to_expr's expression (or LatexError message) and
Calculator().compute's value (or CalcError message); for plain expressions, compute alone; and
quiz.grade / quiz.expected_value verdicts, values and errors. The fuzzed cases use a fixed seed.
"""

from __future__ import annotations

import json
import math
import os
import random
import sys
from pathlib import Path

ANDROID = Path(__file__).resolve().parents[1]
SRC = ANDROID.parent / "src"
if (SRC / "study_reader").is_dir():
    sys.path.insert(0, str(SRC))

from study_reader import quiz  # noqa: E402
from study_reader.calc_latex import LatexError, expr_to_latex, latex_to_expr  # noqa: E402
from study_reader.calculator import CalcError, Calculator  # noqa: E402

OUT = Path(sys.argv[1]) if len(sys.argv) > 1 else ANDROID / "core/src/test/resources/grading_cases.json"
# More fuzzed cases (GRADING_FUZZ=10) or another seed (GRADING_SEED), for a one-off deeper check.
SCALE = int(os.environ.get("GRADING_FUZZ", "1"))
SEED = int(os.environ.get("GRADING_SEED", "20261102"))

# -- LaTeX as the math field writes it -------------------------------------------------------
LATEX = [
    # from tests/test_calculator.py and tests/test_quiz.py
    r"\frac{1}{2+3}", r"\frac34x", r"2\pi r", r"3\left(x+1\right)", r"\sqrt{2}\cdot\sqrt[3]{8}", r"\sqrt[4]{16}",
    r"x^{10}+e^2", r"\sin2x", r"\sin^{-1}\left(1\right)", r"\sin^2\left(x\right)", r"\log_{2}\left(8\right)",
    r"\left|x\right|+|y|", r"\left\lfloor2.5\right\rfloor", r"5!", r"7\%2", r"\operatorname{comb}\left(5,2\right)",
    r"x_{1}=2.5e3", r"1.5\times10^{20}", r"\infty", r"\frac{\placeholder{}}{2}", r"\int x", r"!", r"r=\frac{3}{2}",
    r"2r", r"\frac{1}{\placeholder{}}", r"\frac{1}{3}", r"\frac{82}{2}", "41", "40.7", "40", r"\frac{\sqrt{2}}{2}",
    "0.7071", r"\frac{1}{",
    # fractions
    r"\frac{1}{2}", r"\frac{3}{8}", r"-\frac{3}{8}", r"\frac{-3}{8}", r"\frac{\frac{1}{2}}{\frac{3}{4}}",
    r"\frac{1}{1+\frac{1}{2}}", r"\dfrac{2}{3}", r"\tfrac12", r"\cfrac{1}{2+\cfrac{1}{2}}", r"\frac{1}{0}",
    r"\frac{0}{0}", r"\frac{2\pi}{3}", r"\frac{1}{2}x", r"\frac{1}{2}\left(3\right)", r"\frac12\frac34",
    r"\frac{a}{b}", r"\frac{}{2}", r"\frac{1}", r"\frac", r"\frac{1}{3}+\frac{1}{6}", r"\frac{0.1}{0.3}",
    r"\frac{22}{7}-\pi", r"\frac{1}{3}\cdot3", r"\frac{10^{20}+1}{10^{20}}",
    # roots
    r"\sqrt{16}", r"\sqrt{-1}", r"\sqrt[3]{27}", r"\sqrt[3]{-8}", r"\sqrt[4]{81}", r"\sqrt[n]{8}", r"\sqrt[]{9}",
    r"\sqrt[2]{9}", r"\sqrt9", r"\sqrt{\frac{9}{4}}", r"2\sqrt{2}", r"\sqrt{2}^2", r"\sqrt{2}\sqrt{8}", r"\sqrt{}",
    r"\sqrt", r"\sqrt[5]{-32}", r"\sqrt[0]{2}", r"\sqrt{10^{400}}", r"\sqrt{2}^{10000}",
    # powers
    r"2^{10}", r"2^10", r"2^{-1}", r"2^-1", r"(-8)^{\frac{1}{3}}", r"\left(-8\right)^{\frac{1}{3}}", r"10^{-3}",
    r"e^{i\pi}", r"e^{2}", r"e^2", r"2^{2^{3}}", r"2^{0.5}", r"0^0", r"0^{-1}", r"0.0^{-1}", r"9^{9^{9}}",
    r"2^{20000}", r"2^{1000}", r"2^{1024}", r"2^{1023}", r"10^{400}", r"10^{-400}", r"1.5^{2000}", r"x^2",
    r"2^", r"^2", r"2^{}", r"(-1)^{10^{100}}", r"1^{10^{100}}", r"0^{10^{100}}", r"(-2)^{0.5}", r"2^{\pi}",
    r"\left(\frac{1}{2}\right)^{-3}", r"\left(-\frac{2}{3}\right)^{-3}", r"1.0001^{10000}", r"e^{1000}",
    r"10^{308}\cdot10", r"10^{308}\cdot10.0", r"\sqrt{2}\cdot10^{308}\cdot10",
    # brackets
    r"\left(1+2\right)\left(3+4\right)", r"(1+2)(3+4)", r"2(3)", r"(2)3", r"\left[1+2\right]", r"[1+2]3",
    r"\lbrack 2\rbrack", r"\left\{1\right\}", r"\left.1+2\right|", r"\left(1+2", r"1+2\right)", r"(1+2", r"1+2)",
    r"\left(\right)", r"()", r"\left|-3\right|", r"|-3|", r"|-3|+|4|", r"\lvert -2\rvert", r"\vert -2\vert",
    r"\left\lceil2.1\right\rceil", r"\lceil 2.1\rceil", r"\lfloor -2.5\rfloor", r"\left\lfloor\frac{7}{2}\right\rfloor",
    r"\left\langle1\right\rangle", r"{1+2}3", r"{}", r"\left\lvert-5\right\rvert", r"\left\vert3\right\vert",
    r"\left[2\right)", r"\left(", r"\left", r"\right", r"\right)", r"]", r"1]", r"|", r"||",
    # operators
    r"2\cdot3", r"2\times3", r"6\div4", r"2\ast3", r"7\bmod3", r"7\mod3", r"7\%3", r"1-\left(-1\right)", r"--1",
    r"-1", r"+1", r"2--3", r"2+-3", r"1/2", r"1/0", r"2*3", r"2=3", r"1,2", r"\pm1", r"1\le2", r"2**3", r"2//3",
    r"-7\bmod3", r"7.5\bmod2", r"\sqrt{2}\bmod1", r"7\bmod0", r"7.5\%0", r"x=", r"=",
    # implied multiplication
    r"2\pi", r"\pi2", r"2e", r"2e3", r"2e-3", r"2e+3", r"2e-x", r"2\exponentialE", r"\exponentialE^{2}",
    r"\frac{1}{2}\pi", r"\pi\pi", r"\pi r^2", r"2\tau", r"2\infty", r"-\infty", r"\infty-\infty", r"0\infty",
    r"2(3)(4)", r"2|3|", r"ab", r"xy", r"ee", r"2ee", r"eee", r"3.5e", r"e2", r"e3x",
    # functions
    r"\sin\left(\frac{\pi}{6}\right)", r"\sin\frac{\pi}{6}", r"\sin \pi", r"\sin 2x", r"\sin2\pi", r"\sin x+1",
    r"\sin(x)", r"\sin(\pi)", r"\cos\pi", r"\cos 0", r"\tan\frac{\pi}{4}", r"\tan\left(\frac{\pi}{2}\right)",
    r"\sin^2\left(\frac{\pi}{4}\right)+\cos^2\left(\frac{\pi}{4}\right)", r"\sin^{-1}\left(0.5\right)",
    r"\cos^{-1}(0)", r"\tan^{-1}1", r"\sin^{\left(-1\right)}0.5", r"\arcsin\left(2\right)", r"\arccos\left(0.5\right)",
    r"\arctan\left(1\right)", r"\sinh\left(1\right)", r"\cosh 0", r"\tanh(1)", r"\sinh^{-1}\left(1\right)",
    r"\arsinh 1", r"\arcosh 2", r"\arcosh 0.5", r"\artanh 0.5", r"\artanh 1", r"\artanh -0.5", r"\ln\left(e\right)",
    r"\ln e", r"\ln 0", r"\ln\left(-1\right)", r"\ln", r"\ln 10^{400}", r"\log\left(100\right)", r"\log 8",
    r"\log_2 8", r"\log_{10}\left(1000\right)", r"\log_{2}(8)", r"\log_{1}8", r"\lg 100", r"\log10(100)",
    r"\exp\left(1\right)", r"\exp 1000", r"\exp(-1000)", r"\min\left(3,4\right)", r"\max(3,4,5)", r"\max 3",
    r"\max\left(\frac{1}{3},0.3333\right)", r"\min(2,2.0)", r"\max(\infty,1)", r"\min()", r"\gcd\left(12,18\right)",
    r"\gcd(2.5,5)", r"\gcd(\sqrt{4},2)", r"\deg(\pi)", r"\operatorname{lcm}\left(4,6\right)", r"\operatorname{lcm}()",
    r"\operatorname{gcd}()", r"\operatorname{perm}\left(5,2\right)", r"\operatorname{perm}(5)",
    r"\operatorname{perm}(-1)", r"\operatorname{perm}(3,5)", r"\operatorname{round}\left(2.5\right)",
    r"\operatorname{round}\left(3.5\right)", r"\operatorname{round}\left(-2.5\right)",
    r"\operatorname{round}\left(3.14159,2\right)", r"\operatorname{round}\left(1250,-2\right)",
    r"\operatorname{round}\left(\sqrt{2},3\right)", r"\operatorname{round}(2.675,2)", r"\operatorname{round}(\pi)",
    r"\operatorname{round}(2.5,1.5)", r"\operatorname{round}(2.5,-1.5)", r"\operatorname{round}()",
    r"\operatorname{abs}\left(-2\right)", r"\operatorname{floor}(2.7)", r"\operatorname{floor}(\infty)",
    r"\operatorname{ceil}(2.1)", r"\operatorname{ceil}(-\frac{7}{2})", r"\operatorname{trunc}(-2.7)",
    r"\operatorname{trunc}(\infty-\infty)", r"\operatorname{hypot}(3,4)", r"\operatorname{hypot}(1,2,2)",
    r"\operatorname{hypot}()", r"\operatorname{atan2}(1,1)", r"\operatorname{atan2}(1)",
    r"\operatorname{\mathrm{atan2}}(1,1)", r"\operatorname{cbrt}(27)", r"\operatorname{log2}(8)",
    r"\operatorname{log2}(10)", r"\operatorname{log10}(1000)", r"\operatorname{sqrt}(2)", r"\operatorname{rad}(180)",
    r"\operatorname{foo}(2)", r"\operatorname{2x}", r"\operatorname{}", r"\operatorname", r"\mathrm{e}",
    r"\mathrm{pi}", r"\text{ans}", r"\mathit{x}", r"\mathbf{y}", r"\textrm{inf}", r"\mathnormal{tau}",
    r"\mathrm{\alpha}", r"\operatorname{comb}(5)", r"\operatorname{comb}(-1,2)", r"\operatorname{comb}(5,-1)",
    r"\operatorname{comb}(2.5,1)", r"\operatorname{comb}(52,5)", r"\operatorname{factorial}(5)",
    r"\operatorname{factorial}(2.5)", r"\operatorname{factorial}(1001)", r"\operatorname{factorial}(-1)",
    r"\operatorname{factorial}(\sqrt{25})", r"\operatorname{factorial}(5,2)", r"\operatorname{sin}(1,2)",
    r"\operatorname{sinh}(1000)", r"\operatorname{cosh}(-1000)", r"\operatorname{acosh}(1)", r"\operatorname{asin}(-1)",
    r"\operatorname{exp}(709)", r"\operatorname{exp}(710)", r"\operatorname{exp}(\infty)", r"\operatorname{sqrt}(\infty)",
    r"\sin\infty", r"\sin^{2}", r"\sin^2x", r"\cos^{-1}\left(2\right)", r"\log_{\placeholder{}}8",
    r"\log_{x}(8)", r"\log_{2}", r"\ln^{2}\left(e\right)", r"\sin\left(30\right)", r"\sin 1e300",
    # names typed as letters
    r"sqrt(16)", r"sqrt{16}", r"log10(100)", r"log2(8)", r"atan2(1,1)", r"sin(pi/6)", r"sinx", r"sin x", r"pi",
    r"tau", r"inf", r"e", r"ans", r"x", r"x_1", r"x_{12}", r"x_{a+b}", r"sqrt_2", r"log_{2}(8)", r"log_2(8)",
    r"sin", r"max(1)", r"min(2.5)", r"max(\sqrt{2})", r"foo(1)", r"pi(1)", r"ans(1)", r"e(2)", r"lcm(4,6)",
    r"perm(5,2)", r"hypot(3,4)", r"trunc(2.5)", r"x_1^2", r"x_{1}_{2}", r"a_\alpha",
    # factorial
    r"0!", r"3!!", r"(3)!", r"\left(3\right)!", r"2.5!", r"-3!", r"1001!", r"170!", r"171!", r"20!/18!", r"+!",
    r"\frac{5!}{3!}", r"(-1)!", r"\infty!", r"(\infty-\infty)!", r"(\sqrt{4})!", r"!2",
    # percent
    r"50\%", r"50\%\cdot 2", r"10\%3", r"5\%", r"\%", r"10\%-3", r"-10\%3",
    # scientific notation and decimals
    r"1.5\times10^{-3}", r"1.5\cdot10^{3}", r"6.02\times10^{23}", r"1e5", r"1.5e-3", r"2.5e+3", r"1e", r"1e+",
    r"1E5", r"1.2.3", r"..5", r".5", r"5.", r"1,5", r"1{,}5", r"1\,000", r"1\ 000", r"0.1+0.2", r"0.1+0.2-0.3",
    r"1e400", r"1e-400", r"1e308\cdot10", r"123456789012345678901234567890", r"0.000001", r"1.5e3e2",
    r"3.14159265358979323846", r"1e5e", r"2e10x",
    # constants
    r"\pi", r"\tau", r"\exponentialE", r"\pi^2", r"\frac{\pi}{2}", r"\infty^0", r"1^\infty", r"\infty\cdot0",
    # spaces and formatting commands
    r"1 + 2", r" 1+2 ", r"1\,+\,2", r"1\;+\;2", r"1\quad+2", r"1\qquad+2", r"1\!+2", r"1\:+2",
    r"\displaystyle\frac{1}{2}", r"\textstyle 2", r"\sum_{i=1}^{3}i", r"\alpha", r"\theta", r"\placeholder{}",
    r"\placeholder", "\\\\", "\\", "", " ", "\\ ", "1\n+2", "1\t+\u00a02", "x²", "²", "½", "١٢", "π", "3×4", "−1",
    r"\frac{1}{2}\placeholder{}", r"\limits", r"\nolimits 2", r"\mathrm{x}_{1}", r"{}^2",
    # misc errors
    r"}", r"{", r"1}", r"&", r"#", r"_2", r"x_", r"\sin", r"\sin^", r"\sin_", r"\log_", r"x_{}",
    r"2^{\placeholder{}}", r"30^{\circ}", r"\deg 30", r"\operatorname{x y}", r"\operatorname x", r"\text{a'b}",
    r"\text{a\"b}", r"\text{\n}", r"\mathrm{é}", "\\operatorname{a\u00e9}", r"\$", r"\#", r"\{", r"a\_b",
]

# -- plain expressions (expected answers, older calculator input) ----------------------------
EXPRS = [
    "3/8", "sqrt(2)/2", "2e-3", "1.5 × 10^3", "6.02e23", "pi/4", "-1/3", "2^-2", "0.1+0.2", "1/3", "(1+sqrt(5))/2",
    "e^2", "ln(2)", "log(8, 2)", "log10(1000)", "100%7", "7//2", "10**-2", "3 ÷ 4", "−5", " 42 ", "1,000",
    "\\frac{3}{8}", "", "   ", "1/3 + 1/6", "gcd(8/2, 6)", "abs(-3)", "round(2.5)", "round(3.5)", "round(-2.5)",
    "round(2.675, 2)", "round(1234, -2)", "round(1250, -2)", "round(5/2)", "round(7/2, 0)", "round(7/3, 3)",
    "round(1e300, 2)", "round(sqrt(2), 400)", "round(sqrt(2), -400)", "round(123.456, -1)", "round(10^20+0.5)",
    # (round(2.5, 10^30) and round(5, -10^30) never finish in Python: it computes 10 ** 10^30.)
    "round(5, 10^30)", "round(1/3, 10)", "round(1/3, 30)", "round(5, -3)", "round(-5, -1)", "round(15, -1)",
    "2 + 3 ^ 2", "-2**2", "7 // 2 + 7 % 2", "sqrt(16) + log(8, 2) + ln(e)", "factorial(5) / comb(5, 2)",
    "sin(pi / 6)", "sin(pi)", "1/0", "sqrt(-1)", "(-8) ^ (1/3)", "9 ^ 9 ^ 9", "factorial(100000)",
    "__import__('os')", "x", "sin", "2 +", "(1).real", "[1, 2]", "True + 1", "abs(1, 2)", "6/3", "2^-2",
    "sqrt(2) * sin(pi/4) + 3^10 % 7", "log10(100) + 2**10 // 3", "sqrt(16)", "-7 // 2", "-7 % 3", "7 % -3",
    "-7.5 // 2", "7.5 % -2", "-sqrt(2) // 1", "sqrt(2) % -1", "inf % 2", "inf // 1", "2 % inf", "-2 % inf",
    "1 // inf", "-1 // inf", "inf - inf", "inf * 0", "0 * inf", "inf / inf", "1 / inf", "(inf-inf) ** 0",
    "(-inf) ** 0.5", "(-inf) ** 3", "(-inf) ** -3", "inf ** -1", "0.5 ** inf", "2 ** inf", "(-1) ** inf",
    "2 ** -inf", "(-2) ** 3.0", "(-2) ** 0.5", "0 ** 0", "0.0 ** 0", "0 ** -1", "0.0 ** -1", "0.0 ** -0.5",
    "sqrt(0) ** -2", "(-0.0) ** -1", "10.0 ** 400", "10 ** 400", "10 ** 4000", "10 ** 3000", "(10 ** 3000) ** 2",
    "10 ** 3000 * 10 ** 3000", "2 ** 13999", "2 ** 14000", "2 ** 14001", "(1/2) ** 7000", "(1/2) ** 7001",
    "3 ** 0.5", "4 ** (1/2)", "8 ** (1/3)", "27 ** (2/3)", "(1/4) ** (1/2)", "(-1) ** (10**100)",
    "(-1) ** (10**100 + 1)", "1 ** (10 ** 100)", "0 ** (10 ** 100)", "0 ** -(10 ** 100)", "2 ** 0.5 ** 2",
    "1e308 * 10", "1e308 * 10.5", "1e400", "-1e400", "1e-400", "1e-400 * 1e400", "float(1)", "1if 1 else 2",
    "2pi", "1_000", "1_000.5", "0x1F", "0X1f", "0x_1f", "0o17", "0b101", "0b2", "0o8", "0x", "01", "00", "0_0",
    "1__0", "1_", "1e", "1e+", "1e5", "1E5", "1.e5", ".5", "5.", "1.5j", "1j", "True", "None", "...", '"a"',
    "1/0 + 'a'", "[1]", "f(x=1)", "sin(1,2)", "atan2(1)", "ans", "ee", "eee", "pi(1)", "ans(2)", "foo(1/0)",
    "foo(1)", "sin(inf)", "tan(1e300)", "sin(1e300)", "cos(1e22)", "exp(709)", "exp(710)", "exp(-inf)",
    "exp(inf)", "log(0)", "log(-1)", "log(inf)", "log(-inf)", "log(10 ** 400)", "log(10 ** 400, 10)",
    "log10(10 ** 400)", "log2(2 ** 1100)", "log2(10 ** 400)", "log(2 ** 1100 + 1/2)", "log(1/10 ** 400)",
    "log(8, 1)", "log(8, 0)", "log(8, 2, 3)", "log()", "log2(0.1)", "log2(1024)", "log2(2 ** -1074 * 1.0)",
    "log10(0.001)", "hypot(1e308, 1e308)", "hypot(3e307, 4e307)", "hypot(inf, inf - inf)", "hypot(inf - inf, 1)",
    "hypot(-3)", "hypot(1, 2, 3, 4)", "hypot(10 ** 400)", "comb(-1, 2)", "comb(5, -1)", "comb(-1, -1)",
    "comb(-1, 2.5)", "comb(5, 7)", "comb(10 ** 30, 2)", "comb(1000, 500)", "comb(10 ** 6, 5 * 10 ** 5)",
    "perm(-1)", "perm(3, -1)", "perm(5)", "perm(5, 0)", "perm(2.5)", "perm(sqrt(4))", "perm()", "perm(5, 2, 1)",
    "perm(10 ** 6, 3)", "min(3)", "min(2.5)", "max(sqrt(2))", "min()", "max()", "min(1, 1.0)", "max(1.0, 1)",
    "min(inf - inf, 1)", "min(1, inf - inf)", "max(1/3, 0.3333333333333333)", "max(0.3333333333333333, 1/3)",
    "min(10 ** 400, inf)", "max(-inf, -10 ** 400)", "gcd(2.5, 1)", "gcd(sqrt(4), 2)", "gcd(-4, 6)", "gcd(0, 0)",
    "gcd()", "gcd(12)", "gcd(12, 18, 8)", "lcm()", "lcm(4, 6)", "lcm(0, 5)", "lcm(-4, 6)", "lcm(4, 2.5)",
    "round(2.5, 1.5)", "round(2.5, -1.5)", "round(2.5, sqrt(4))", "round(2.5, -sqrt(4))", "round(250.5, -sqrt(4))",
    "round(sqrt(2), 2.5)", "round(sqrt(2), 2)", "round(inf)", "round(inf - inf)", "round(inf, 2)",
    "round(1.7e308 * 10, -308)", "round(1.7e308, -308)", "round(sqrt(1.7e308)**2, -308)", "round()",
    "round(2.5, 1, 1)", "round(-0.5)", "round(0.5)", "round(1.5)", "round(-1.5)", "round(sqrt(6.25))",
    "floor(inf - inf)", "floor(inf)", "floor(-inf)", "floor(-2.5)", "floor(7/2)", "floor(-7/2)", "ceil(-7/2)",
    "ceil(sqrt(2))", "trunc(-7/2)", "trunc(-sqrt(2))", "trunc(inf - inf)", "floor(1e300)", "floor(2, 3)",
    "factorial(inf)", "factorial(inf - inf)", "factorial(2.5)", "factorial(5.0)", "factorial(sqrt(25))",
    "factorial(-1)", "factorial(1000)", "factorial(1001)", "factorial()", "factorial(170) * 1.0",
    "factorial(171) * 1.0", "atanh(1)", "atanh(-1)", "atanh(0.5)", "atanh(1e-20)", "asinh(1e-20)", "asinh(-2)",
    "asinh(1e300)", "acosh(0.5)", "acosh(1)", "acosh(1e300)", "acosh(1.5)", "sinh(1000)", "cosh(1000)",
    "tanh(1000)", "sinh(-inf)", "asin(2)", "acos(-1)", "atan(inf)", "atan2(0, -1)", "atan2(-0.0, -1)",
    "atan2(inf, inf)", "deg(1e308)", "deg(pi)", "rad(180)", "deg(10 ** 400)", "sqrt(10 ** 400)",
    "sqrt(1e308 * 10)", "cbrt(-27)", "cbrt(10 ** 400)", "sqrt()", "abs()", "abs(-2.5)", "abs(-inf)",
    "abs(-7/2)", "sqrt(2)^10000", "sqrt(2) ** 2000", "sqrt(2) ** 2047", "sqrt(2) ** 2048", "1.5e308 * 10",
    "(1e308 * 10) * 1.0", "10 ** 309 * 1.0", "10 ** 309 + 0.5", "10 ** 308 / 0.1", "1 / 10 ** 400 * 1.0",
    "1 / 10 ** 400", "x = 1", "1 # c", "# c", "(1\n+2)", "1\n+2", "1\n", "1\\\n+2", "1 \\", "'''a'''",
    "await 1", "lambda: 1", "lambda x, y=2: x", "1 if 2 else 3", "not 1", "1 and 2", "1 or 2", "a.b", "x[1]",
    "x[1:2]", "x[::2, 1]", "{1}", "{}", "{1: 2}", "{**a}", "{*a}", "()", "(1,)", "1,2", "1,", "*a", "*a, 1",
    "f(*a)", "f(**a)", "f(a, *b)", "f(1/0, *b)", "f(x=1, 2)", "f(**a, *b)", "f(x for x in y)",
    "f(x for x in y, 1)", "sin(x for x in y)", "[x for x in y]", "{x: 1 for x in y}", "(x for x in y if z)",
    "1 < 2", "1 < 2 < 3", "1 is not 2", "1 not in 2", "1 in 2", "yield", "(yield)", "(yield 1)",
    "(yield from x)", "x := 1", "(x := 1)", "f(x := 1)", "1/0 + (yield)", "1/0+[1+]", "1/0 + (1 < 2)",
    "(1 < 2) + 1/0", "1/0 @ 2", "2 @ 1/0", "1 @ 2", "~1", "-~1", "1 | 2", "1 & 2", "1 << 2", "1 >> 2",
    "b'a'", "b'é'", "f'a{1}'", "u'a'", "rb'a'", "'a' 'b'", "'a' b'b'", "'a", "'a\nb'", "'a\\'b'",
    "1andx", "1 and x", "1or 2", "1else", "1is 2", "1in x", "1not in x", "1for", "0x1for", "2x", "1abc",
    "1e5j", "1_2.3_4e1_0", "α", "x1", "_", "__debug__", "ｘ", "１", "ℯ", "π", "x²", "a²", "ﬁ",
    "1\x00", "1\u00a0+2", "\u00a01+2\u00a0", "\u20021", "1\x0b+2", "1\f+2", "1\t+2", "2 ** -1", "7 // 0",
    "7.5 % 0", "sqrt(2) % 0", "sqrt(2) // 0", "0 % 0", "1 / 0.0", "1.0 / 0", "0.0 / 0.0", "1 // 0.0",
    "1 % 0.0", "-(-1)", "+-+1", "--1", "- 1", "(((1)))", "(" * 200 + "1" + ")" * 200, "(" * 201 + "1" + ")" * 201,
    "-" * 300 + "1", "1" + "+1" * 300, "f(" * 50 + "1" + ")" * 50, "1)", "(1", "[1)", "1]", "1 2", "1 (2)",
    "2(3)", "(2)(3)", "sin(1)(2)", "(sin)(pi/2)", "((sin))(pi/2)", "sin (pi/2)", "sin\t(pi/2)", "sin.x",
    "$", "?", "`1`", "!1", "1!", "1 != 2", "1 <> 2", "1 -> 2", "1; 2", "1 = 2", "1 == 2", "1 += 2",
    "1 ^ 2", "1 ^^ 2", "1 ^* 2", "2 **", "** 2", "e ** 2", "e^-1", "pi^e", "tau/2 - pi", "inf - 1",
    "-inf", "1/inf", "0.1 + 0.2 - 0.3", "10 ** 20 + 1 - 10 ** 20", "1e20 + 1 - 1e20", "2 ** 0.5 * 2 ** 0.5",
    "1/3 * 3", "(1/3) ** -2", "(-2/3) ** -3", "0.5 ** -1", "(2/3) ** 0.5", "(-8) ** (1/3)", "(-8.0) ** 3",
    "(-1/8) ** (1/3)", "(-0.5) ** 2.5", "1 ** inf", "1.0 ** (inf - inf)", "(inf - inf) ** 1",
    "hypot(1.7e308, 1.7e308)", "hypot(5e-324, 5e-324)", "hypot(3, 4, 12)", "hypot(pi, 8 * pi)", "cbrt(5e-324)",
    "cbrt(-1e-310)", "cbrt(44.22)", "cbrt(sinh(67.65))", "cbrt(-inf)", "log2(80)", "log2(1.0000001)",
    "log2(0.7)", "log2(5e-324)", "log2(3 * 2 ** 1000)", "(-2) ** 1e10", "(-2) ** 1e10.5", "(-2) ** 10000.5",
    "(-0.5) ** 10000.5", "(-589) ^ 1e3.5", "(-1.5) ** 1750.5", "(-1.5) ** 1751.5",
]

EXPR_TO_LATEX = [
    "2**10", "2 ** -3", "2^x", "x ** 2.5", "2 **(3)", "a**b**c", "50%", "10 % 3", "2***3", "x**  -y", "**2",
    "2**-", "2 ** - 3", "sqrt(2) * sin(pi/4) + 3^10 % 7", "log10(100) + 2**10 // 3", "e**é", "2**_a", "1**.5",
    "2**x²", "2^\u00a0x", "", "plain",
]


def enc(v: float):
    if math.isnan(v):
        return "nan"
    if math.isinf(v):
        return "inf" if v > 0 else "-inf"
    return v


def computed(expr: str) -> dict:
    try:
        v = Calculator().compute(expr)
    except CalcError as e:
        return {"error": str(e)}
    except RecursionError as e:
        return {"crash": type(e).__name__}
    try:
        return {"value": enc(float(v))}
    except OverflowError as e:
        return {"crash": type(e).__name__}


def latex_case(latex: str) -> dict:
    case = {"latex": latex}
    try:
        expr = latex_to_expr(latex)
    except LatexError as e:
        case["latex_error"] = str(e)
        return case
    case["expr"] = expr
    case.update(computed(expr))
    return case


def raised(f, *args) -> dict:
    try:
        result = f(*args)
        return {"result": enc(result) if isinstance(result, float) else result}
    except quiz.AnswerError as e:
        return {"error": "AnswerError", "message": str(e)}
    except Exception as e:  # ValueError, KeyError, TypeError, OverflowError…
        return {"error": type(e).__name__, "message": str(e)}


# -- fuzzing ----------------------------------------------------------------------------------
LATEX_TOKENS = [
    *"0123456789", "0", "1", "2", "3", ".", "x", "e", "e", "a", "+", "-", "-", "\\cdot", "\\times", "\\div", "^",
    "^{", "_", "{", "}", "}", "(", ")", "\\left(", "\\right)", "\\frac{", "\\sqrt{", "\\sqrt[3]{", "\\sqrt[",
    "]", "[", "\\pi", "\\sin", "\\cos", "\\tan", "\\ln", "\\log", "\\log_{", "|", "\\left|", "\\right|", "!",
    "\\%", "\\infty", "\\exponentialE", "\\operatorname{", "comb", ",", "\\lfloor", "\\rfloor", " ", "\\,", "E",
    "=", "\\placeholder{}", "\\mathrm{", "\\tau", "\\sinh", "\\arcsin", "\\lceil", "\\rceil", "\\lbrack",
    "\\rbrack", "\\vert", "\\right.", "\\left.", "\\bmod", "\\gcd", "\\max", "\\min", "\\exp", "\\lg", "\\deg",
    "\\text{", "sin", "pi", "sqrt", "log", "10", "2", "^{-1}", "^2", "\\right", "\\left", "\\lvert", "\\rvert",
]
PLAIN_TOKENS = [
    *"0123456789", "1", "2", "10", "0.5", "1.5", "1e3", "2e-2", ".5", "x", "e", "pi", "tau", "inf", "ans", "+",
    "-", "-", "*", "/", "//", "%", "**", "^", "(", ")", "(", ")", ",", " ", "sqrt(", "sin(", "cos(", "log(",
    "ln(", "exp(", "abs(", "floor(", "ceil(", "round(", "factorial(", "gcd(", "lcm(", "comb(", "perm(", "min(",
    "max(", "hypot(", "atan2(", "asin(", "acosh(", "atanh(", "trunc(", "deg(", "×", "÷", "−", "0x1f", "1_0",
    "True", "'s'", "<", "==", "[", "]", "j", "1j", "lambda", ":", "if", "else", "not", ".", "e5",
]


def fuzz_latex_tokens(rng: random.Random) -> str:
    return "".join(rng.choice(LATEX_TOKENS) for _ in range(rng.randint(1, 10)))


def fuzz_plain(rng: random.Random) -> str:
    while True:
        s = "".join(rng.choice(PLAIN_TOKENS) for _ in range(rng.randint(1, 10)))
        # Python can take forever on these (round(0.5, 10**10) computes 10 ** 10**10)
        if not (any(f in s for f in ("round(", "perm(", "comb(")) and ("**" in s or "^" in s)):
            return s


def gen_latex(rng: random.Random, depth: int = 0) -> str:
    """A well-formed expression as the math field writes it."""
    if depth > 3 or rng.random() < 0.3:
        return rng.choice([
            str(rng.randint(0, 20)), str(rng.randint(0, 999)), f"{rng.randint(0, 99)}.{rng.randint(0, 99)}",
            "\\pi", "e", "\\tau", "\\exponentialE", f"{rng.randint(1, 9)}\\pi", f"{rng.randint(1, 9)}.{rng.randint(0, 9)}e-{rng.randint(1, 5)}",
            f"{rng.randint(1, 9)}\\times10^{{{rng.randint(-5, 5)}}}", f"{rng.randint(0, 12)}!", "\\infty",
        ])
    a = gen_latex(rng, depth + 1)
    b = gen_latex(rng, depth + 1)
    f = rng.choice(["\\sin", "\\cos", "\\tan", "\\ln", "\\log", "\\exp", "\\sqrt", "\\arctan", "\\sinh", "\\lg"])
    return rng.choice([
        f"{a}+{b}", f"{a}-{b}", f"{a}\\cdot{b}", f"{a}\\times{b}", f"{a}\\div{b}", f"\\frac{{{a}}}{{{b}}}",
        f"\\sqrt{{{a}}}", f"\\sqrt[3]{{{a}}}", f"\\sqrt[{rng.randint(2, 5)}]{{{a}}}", f"{a}^{{{rng.randint(-3, 4)}}}",
        f"\\left({a}\\right)^{{{b}}}", f"\\left({a}\\right)", f"{rng.randint(2, 9)}\\left({a}\\right)",
        f"{f}\\left({a}\\right)", f"{f} {a}", f"{f}^{{-1}}\\left({a}\\right)", f"{f}^2\\left({a}\\right)",
        f"\\left|{a}\\right|", f"\\left\\lfloor{a}\\right\\rfloor", f"\\left\\lceil{a}\\right\\rceil", f"-{a}",
        f"{a}\\%", f"\\log_{{{rng.randint(2, 10)}}}\\left({a}\\right)", f"{a}\\bmod{b}",
        f"\\operatorname{{{rng.choice(['round', 'floor', 'ceil', 'abs', 'trunc', 'cbrt', 'log2', 'log10'])}}}\\left({a}\\right)",
        f"\\max\\left({a},{b}\\right)", f"\\min\\left({a},{b}\\right)", f"\\operatorname{{hypot}}\\left({a},{b}\\right)",
        f"\\operatorname{{round}}\\left({a},{rng.randint(-2, 3)}\\right)", f"\\frac{{{a}}}{{{b}}}{rng.choice(['x', '\\pi', ''])}",
    ])


def gen_plain(rng: random.Random, depth: int = 0) -> str:
    if depth > 3 or rng.random() < 0.3:
        return rng.choice([
            str(rng.randint(0, 20)), str(rng.randint(0, 999)), f"{rng.randint(0, 99)}.{rng.randint(0, 99)}",
            "pi", "e", "tau", f"{rng.randint(1, 9)}e{rng.randint(-5, 5)}", f"{rng.randint(1, 9)}.{rng.randint(0, 9)}e-{rng.randint(1, 5)}",
        ])
    a = gen_plain(rng, depth + 1)
    b = gen_plain(rng, depth + 1)
    f = rng.choice(["sin", "cos", "tan", "ln", "log", "exp", "sqrt", "atan", "sinh", "log10", "log2", "abs", "floor",
                    "ceil", "round", "trunc", "cbrt", "asinh", "deg", "rad"])
    return rng.choice([
        f"{a} + {b}", f"{a} - {b}", f"{a} * {b}", f"{a} / {b}", f"{a} // {b}", f"{a} % {b}", f"({a}) ** {rng.randint(-3, 4)}",
        f"({a}) ^ ({b})", f"{f}({a})", f"-{a}", f"({a})", f"log({a}, {b})", f"max({a}, {b})", f"min({a}, {b})",
        f"hypot({a}, {b})", f"atan2({a}, {b})", f"round({a}, {rng.randint(-2, 3)})", f"factorial({rng.randint(0, 25)})",
        f"comb({rng.randint(0, 30)}, {rng.randint(0, 30)})", f"gcd({rng.randint(0, 99)}, {rng.randint(0, 99)})",
        f"{a} × {b}", f"{a} ÷ {b}", f"{a} − {b}",
    ])


# -- grading ----------------------------------------------------------------------------------
def grade_cases() -> list[dict]:
    cases: list[tuple[dict, dict]] = []
    choice = {"type": "choice", "question": "Pick", "choices": ["a", "b", "c"], "answer": 1}
    for r in [{"choice": 1}, {"choice": 0}, {"choice": 2}, {"choice": 1.0}, {"choice": True}, {"choice": False},
              {"choice": "1"}, {"choice": None}, {}, {"choice": [1]}, {"choice": 1.5}, {"value": True}]:
        cases.append((choice, r))
    cases.append(({**choice, "answer": 0}, {"choice": False}))
    cases.append(({"type": "choice", "choices": ["a", "b"]}, {"choice": 1}))
    tf = {"type": "truefalse", "question": "Yes?", "answer": False}
    for r in [{"value": False}, {"value": True}, {"value": 0}, {"value": "false"}, {"value": None}, {}]:
        cases.append((tf, r))
        cases.append(({**tf, "answer": True}, r))
    cases.append(({"type": "truefalse", "answer": None}, {}))
    cases.append(({"type": "truefalse"}, {"value": True}))
    card = {"type": "flashcard", "question": "Front", "answer": "Back"}
    for r in [{"rating": 1}, {"rating": 2}, {"rating": 3}, {"rating": 4}, {"rating": 0}, {"rating": 5},
              {"rating": 3.0}, {"rating": 3.5}, {"rating": True}, {"rating": False}, {"rating": "3"},
              {"rating": None}, {}, {"rating": [3]}, {"rating": {"a": 1}}, {"rating": -1}, {"rating": 2 ** 40}]:
        cases.append((card, r))
    for kind in ["written", "free", "cloze", "occlusion", "basic", "", 5, None]:
        cases.append(({"type": kind, "question": "Why?", "answer": "Because"}, {"text": "x"}))
    cases.append(({"question": "no type"}, {"choice": 1}))

    numeric = {"type": "numeric", "question": "How many?", "answer": "41", "unit": "s"}
    for latex in ["41", r"\frac{82}{2}", "40.7", "40.59", "40.58", "41.41", "41.42", "40", "", "   ", r"\frac{1}{",
                  r"\frac{1}{0}", "x", r"\sqrt{-1}", "41.0", "4.1e1", r"4.1\times10^{1}", r"4.1\cdot10", "41\\%",
                  r"\placeholder{}", r"10^{400}", r"\infty", "-41", " 41 ", "\u00a041\u00a0", "1,5", r"\int"]:
        cases.append((numeric, {"latex": latex}))
    for r in [{}, {"latex": None}, {"latex": 41}, {"latex": 41.0}, {"latex": 4.1e1}, {"latex": 1e20},
              {"latex": 1e-7}, {"latex": True}, {"latex": [41]}, {"latex": [1, 2]}, {"latex": {"a": 1}},
              {"latex": 1e300}, {"latex": -0.0}, {"latex": 123456789012345678901234567890}]:
        cases.append((numeric, r))

    answers = [41, 41.0, -41, 0, 0.0, 1e-9, 1e300, 100, "41", "3/8", "sqrt(2)/2", "2e-3", "1.5 × 10^3", "6.02e23",
               "pi/4", r"\frac{3}{8}", r"\frac{\sqrt{2}}{2}", r"1.5\times10^{-3}", "2\\pi", "x", r"\frac{1}{0}",
               "1/0", "", "inf", "-inf", True, None, [1], {"a": 1}, "10**400", r"\sqrt{2}", "1,000", "  7  ",
               "e", "ee", "0.1+0.2", "\\left(1+2\\right)", "1e400", 10 ** 400]
    responses = ["41", "40.6", "0", "-41", "1e-9", r"\frac{3}{8}", "0.375", "0.37", "0.371", "0.38", "0.379",
                 r"\frac{\sqrt{2}}{2}", "0.7071", "0.707", "0.714", "0.7", "0.002", "0.00202", "0.0021", "1500",
                 r"1.5\times10^{3}", "6.02e23", r"6.02\times10^{23}", r"\frac{\pi}{4}", "0.785", "0.0015", r"2\pi",
                 "6.28", r"\infty", "-\\infty", "1000", "7", "e", r"\exponentialE^{2}", "7.389", "0.3", "3", "100",
                 "101", "99", "101.0000001", "1e300", r"10^{300}", "1.01e300"]
    for a in answers:
        for latex in responses:
            cases.append(({"type": "numeric", "answer": a}, {"latex": latex}))
    for tol in [0, 0.0, 0.5, 1, 1.0, 1.5, 2, -1, -1e-12, -2e-12, 1e-12, True, False, None, "1", [1], {"a": 1}, 1e300,
                10 ** 400]:
        for latex in ["41", "41.5", "42", "42.5", "43", "40", "39", "41.000000000001", "41.000000000002"]:
            cases.append(({"type": "numeric", "answer": 41, "tolerance": tol}, {"latex": latex}))
    cases.append(({"type": "numeric"}, {"latex": "1"}))
    cases.append(({"type": "numeric"}, {"latex": ""}))
    cases.append(({"type": "numeric", "answer": "x"}, {"latex": "y"}))
    cases.append(({"type": "numeric", "answer": 0.1, "tolerance": 0}, {"latex": "0.1"}))
    cases.append(({"type": "numeric", "answer": "0.1+0.2", "tolerance": 0}, {"latex": "0.3"}))
    cases.append(({"type": "numeric", "answer": 0.3, "tolerance": 0}, {"latex": "0.1+0.2"}))
    cases.append(({"type": "numeric", "answer": 1, "tolerance": 0}, {"latex": r"10^{20}+1-10^{20}"}))
    cases.append(({"type": "numeric", "answer": 1}, {"latex": r"10^{400}\cdot1.0"}))
    cases.append(({"type": "numeric", "answer": 1}, {"latex": r"10^{400}"}))
    cases.append(({"type": "numeric", "answer": "10^400"}, {"latex": "1"}))
    out = []
    for q, r in cases:
        out.append({"question": q, "response": r, **raised(quiz.grade, q, r)})
    return out


def expected_cases() -> list[dict]:
    answers = [41, 41.5, -0.0, True, False, None, "41", " 41 ", "3/8", "sqrt(2)/2", "2e-3", "1.5 × 10^3",
               r"\frac{3}{8}", r"\frac{1}{0}", "1/0", "x", "", "  ", [1], [1, 2], {"a": 1}, "None", "10**400",
               "10**309", 10 ** 400, 1e300, r"\sqrt{2}", "2\\pi", "2pi", "inf", "1e400", "1,000", "it's",
               "\\x", "é", "a\nb", "\u200b", "\U0001f600", "'\"", "\x7f", "\u0085"]
    return [{"answer": a, **raised(quiz.expected_value, a)} for a in answers]


def main() -> None:
    rng = random.Random(SEED)
    latex = list(LATEX)
    latex += [gen_latex(rng) for _ in range(900 * SCALE)]
    latex += [fuzz_latex_tokens(rng) for _ in range(900 * SCALE)]
    exprs = list(EXPRS)
    exprs += [gen_plain(rng) for _ in range(700 * SCALE)]
    exprs += [fuzz_plain(rng) for _ in range(700 * SCALE)]
    data = {
        "latex": [latex_case(s) for s in dict.fromkeys(latex)],
        "compute": [{"expr": s, **computed(s)} for s in dict.fromkeys(exprs)],
        "expr_to_latex": [{"expr": s, "latex": expr_to_latex(s)} for s in dict.fromkeys(EXPR_TO_LATEX + EXPRS)],
        "grade": grade_cases(),
        "expected_value": expected_cases(),
    }
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps(data, ensure_ascii=False, indent=0) + "\n", encoding="utf-8")
    counts = ", ".join(f"{len(v)} {k}" for k, v in data.items())
    print(f"wrote {OUT}: {counts}")


if __name__ == "__main__":
    main()
