package dev.russell.studyreader.core

/** LaTeX the calculator can't read, worded for the user (port of calc_latex.LatexError). */
class LatexError(message: String) : Exception(message)

/**
 * Port of the desktop's calc_latex.py: LaTeX from the MathLive math field to [Calc]'s plain expressions,
 * e.g. `\frac{1}{2}\sqrt{x}+\sin\left(\pi\right)` to `(1)/(2)*sqrt(x)+sin(pi)`, inserting the multiplications
 * written math leaves out (`2\pi`, `3\left(x+1\right)`, `\frac{1}{2}x`).
 */
object CalcLatex {
    /** Calculator functions, so names can be told apart from variables. */
    internal val FUNCTION_NAMES = setOf(
        "sqrt", "cbrt", "exp", "ln", "log", "log10", "log2", "abs", "floor", "ceil", "round", "trunc",
        "factorial", "gcd", "lcm", "comb", "perm", "hypot", "min", "max", "sinh", "cosh", "tanh",
        "asinh", "acosh", "atanh", "deg", "rad", "sin", "cos", "tan", "asin", "acos", "atan", "atan2",
    )
    private val COMMAND_FUNCTIONS = mapOf(
        "sin" to "sin", "cos" to "cos", "tan" to "tan", "sinh" to "sinh", "cosh" to "cosh", "tanh" to "tanh",
        "arcsin" to "asin", "arccos" to "acos", "arctan" to "atan", "arsinh" to "asinh", "arcosh" to "acosh",
        "artanh" to "atanh", "ln" to "ln", "log" to "log", "lg" to "log10", "exp" to "exp", "min" to "min",
        "max" to "max", "gcd" to "gcd", "deg" to "deg",
    )
    private val INVERSES = mapOf(
        "sin" to "asin", "cos" to "acos", "tan" to "atan", "sinh" to "asinh", "cosh" to "acosh", "tanh" to "atanh",
    )
    private val CONSTANT_COMMANDS = mapOf("pi" to "pi", "tau" to "tau", "infty" to "inf", "exponentialE" to "e")
    private val OPERATOR_COMMANDS = mapOf(
        "cdot" to "*", "times" to "*", "ast" to "*", "div" to "/", "%" to "%", "bmod" to "%", "mod" to "%",
    )
    private val NAME_COMMANDS = setOf("operatorname", "mathrm", "mathit", "mathbf", "text", "textrm", "mathnormal")
    private val IGNORED = setOf(",", ":", ";", "!", " ", "quad", "qquad", "displaystyle", "textstyle", "limits", "nolimits")

    /** Openers of bracketed groups: token to (function or "", closing token). */
    private val DELIMITERS = mapOf(
        "(" to ("" to ")"), "[" to ("" to "]"), "\\lbrack" to ("" to "\\rbrack"), "|" to ("abs" to "|"),
        "\\vert" to ("abs" to "\\vert"), "\\lvert" to ("abs" to "\\rvert"), "\\lfloor" to ("floor" to "\\rfloor"),
        "\\lceil" to ("ceil" to "\\rceil"),
    )

    /** Where a function written without brackets (`\sin 2x`) stops taking its argument. */
    private val BARE_STOP = setOf(
        "+", "-", "=", ",", ")", "]", "}", "|", "*", "/", "\\right", "\\cdot", "\\times", "\\div", "\\%", "\\bmod",
    )
    private val OPERATORS = setOf("+", "-", "*", "/", "^", "%", "=", ",", ")", "]", "|")

    /** Plain calculator expression for the math field's LaTeX. Throws [LatexError]. */
    fun latexToExpr(latex: String): String = Parser(latex).parse()

    /** Rough LaTeX for a plain expression, enough for the math field to show and edit it. */
    fun exprToLatex(expr: String): String {
        // re.sub(r"(\*\*|\^)\s*(-?[\w.]+)", r"^{\2}", expr)
        val sb = StringBuilder()
        var i = 0
        while (i < expr.length) {
            val opLen = if (expr.startsWith("**", i)) 2 else if (expr.startsWith("^", i)) 1 else 0
            if (opLen > 0) {
                var j = i + opLen
                while (j < expr.length && PyText.isSpace(expr.codePointAt(j))) j += Character.charCount(expr.codePointAt(j))
                val group = j
                if (j < expr.length && expr[j] == '-') j++
                val word = j
                while (j < expr.length && (expr[j] == '.' || PyText.isWord(expr.codePointAt(j)))) {
                    j += Character.charCount(expr.codePointAt(j))
                }
                if (j > word) {
                    sb.append("^{").append(expr, group, j).append("}")
                    i = j
                    continue
                }
            }
            val cp = expr.codePointAt(i)
            sb.appendCodePoint(cp)
            i += Character.charCount(cp)
        }
        return sb.toString().replace("**", "^").replace("%", "\\%")
    }

    /** Concatenates operands and operators, multiplying operands that sit side by side (2x, 3(…)). */
    internal fun join(items: List<String>): String {
        val text = StringBuilder()
        var prevOperand = false
        for (item in items) {
            val operand = item !in OPERATORS
            if (operand && prevOperand) text.append('*')
            text.append(item)
            prevOperand = operand
        }
        return text.toString()
    }

    private fun single(tok: String) = tok.codePointCount(0, tok.length) == 1
    private fun isDigitTok(tok: String) = single(tok) && PyText.isDigit(tok.codePointAt(0))
    private fun isLetterTok(tok: String) = tok.length == 1 && (tok[0] in 'a'..'z' || tok[0] in 'A'..'Z')
    private fun isAlnumTok(tok: String) = single(tok) && PyText.isAlnum(tok.codePointAt(0))

    /** re.fullmatch(r"\w+", s) */
    private fun allWord(s: String) = s.isNotEmpty() && s.codePoints().allMatch { PyText.isWord(it) }

    /** re.fullmatch(r"[A-Za-z_]\w*", s) */
    private fun isName(s: String) = s.isNotEmpty() && (isLetterTok(s.substring(0, 1)) || s[0] == '_') &&
        s.codePoints().skip(1).allMatch { PyText.isWord(it) }

    private class Parser(latex: String) {
        val toks = ArrayList<String>()
        var i = 0

        init {
            // TOKEN = re.compile(r"\\([A-Za-z]+|.)|(\s+)|(.)", re.DOTALL)
            var k = 0
            while (k < latex.length) {
                val cp = latex.codePointAt(k)
                if (cp == '\\'.code && k + 1 < latex.length) {
                    val next = latex.codePointAt(k + 1)
                    if (next < 128 && Character.isLetter(next)) {
                        var end = k + 1
                        while (end < latex.length && latex[end].code < 128 && Character.isLetter(latex[end])) end++
                        toks.add(latex.substring(k, end))
                        k = end
                    } else {
                        val end = k + 1 + Character.charCount(next)
                        toks.add(latex.substring(k, end))
                        k = end
                    }
                } else if (PyText.isSpace(cp)) {
                    k += Character.charCount(cp)
                    while (k < latex.length && PyText.isSpace(latex.codePointAt(k))) k += Character.charCount(latex.codePointAt(k))
                } else {
                    toks.add(String(Character.toChars(cp)))
                    k += Character.charCount(cp)
                }
            }
        }

        fun parse(): String {
            val out = seq(emptySet())
            if (i < toks.size) throw LatexError("Unexpected ${toks[i]}")
            return join(out)
        }

        // -- helpers -------------------------------------------------------------------------
        fun peek(offset: Int = 0): String? = toks.getOrNull(i + offset)

        fun take(): String = toks[i++]

        /** Consume `tok` if it's next; a missing closer at the end is forgiven (the field auto-closes). */
        fun expect(tok: String) {
            if (peek() == tok) {
                i++
            } else if (peek() != null) {
                throw LatexError("Expected $tok")
            }
        }

        fun seq(stop: Set<String>): MutableList<String> {
            val out = ArrayList<String>()
            while (true) {
                val tok = peek() ?: break
                if (tok in stop) break
                item(out)
            }
            return out
        }

        fun group(closer: String): String {
            val inner = join(seq(setOf(closer)))
            expect(closer)
            return inner
        }

        /** A TeX argument: `{...}` or a single token. */
        fun arg(): String {
            val tok = peek() ?: throw LatexError("Something is missing")
            if (tok == "{") {
                i++
                return group("}")
            }
            if (isAlnumTok(tok)) {
                i++
                return tok
            }
            val out = ArrayList<String>()
            item(out)
            return join(out)
        }

        /** An argument's text as written, for names like \operatorname{comb}. */
        fun rawArg(): String {
            if (peek() != "{") return if (peek() != null) take() else ""
            i++
            val text = StringBuilder()
            var depth = 1
            while (true) {
                val tok = peek() ?: break
                i++
                depth += when (tok) {
                    "{" -> 1
                    "}" -> -1
                    else -> 0
                }
                if (depth == 0) break
                if (!tok.startsWith("\\") && tok != "{" && tok != "}") text.append(tok) // \operatorname{\mathrm{atan2}}
            }
            return text.toString()
        }

        // -- items ---------------------------------------------------------------------------
        fun item(out: MutableList<String>) {
            val tok = take()
            when {
                isDigitTok(tok) || tok == "." -> out.add(number(tok))
                isLetterTok(tok) -> name(out, tok)
                tok in DELIMITERS -> {
                    val (func, closer) = DELIMITERS.getValue(tok)
                    out.add("$func(${group(closer)})")
                }
                tok == "{" -> out.add("(${group("}")})")
                tok == "^" -> {
                    val power = arg()
                    out.add("^")
                    out.add("($power)")
                }
                tok == "_" -> arg() // a stray subscript carries no meaning for a calculation
                tok == "!" -> {
                    if (out.isEmpty() || out.last() in OPERATORS) throw LatexError("! needs a number before it")
                    out[out.size - 1] = "factorial(${out.last()})"
                }
                tok in OPERATORS -> out.add(tok)
                tok.startsWith("\\") -> command(out, tok.substring(1))
                else -> throw LatexError("Can't use $tok here")
            }
        }

        fun number(first: String): String {
            var text = first
            while (true) {
                val tok = peek() ?: break
                if (!(isDigitTok(tok) || tok == ".")) break
                text += take()
            }
            // Scientific notation: 1e5, 2.5e-3 (an e followed by anything else is the constant)
            if (peek() == "e") {
                val nxt = peek(1)
                val after = peek(2)
                if (nxt != null && isDigitTok(nxt)) {
                    i++
                    text += "e" + number(take())
                } else if ((nxt == "+" || nxt == "-") && after != null && isDigitTok(after)) {
                    i += 2
                    text += "e" + nxt + number(take())
                }
            }
            return text
        }

        fun name(out: MutableList<String>, first: String) {
            var name = first
            while (true) {
                val tok = peek() ?: break
                if (!isLetterTok(tok)) break
                name += take()
            }
            var digits = ""
            var count = 0
            while (true) {
                val tok = peek(count) ?: break
                if (!isDigitTok(tok)) break
                digits += tok
                count++
            }
            if (digits.isNotEmpty() && (name + digits) in FUNCTION_NAMES) { // log10, log2, atan2
                i += count
                name += digits
            }
            if (peek() == "_" && name !in FUNCTION_NAMES) {
                i++
                val sub = arg()
                if (!allWord(sub)) throw LatexError("A subscript can only hold letters and digits")
                name += "_$sub"
            }
            if (name in FUNCTION_NAMES) function(out, name) else out.add(name)
        }

        fun command(out: MutableList<String>, cmd: String) {
            if (cmd in IGNORED) return
            when {
                cmd == "frac" || cmd == "dfrac" || cmd == "tfrac" || cmd == "cfrac" -> {
                    val num = arg()
                    out.add("($num)/(${arg()})")
                }
                cmd == "sqrt" -> {
                    var index: String? = null
                    if (peek() == "[") {
                        i++
                        index = group("]")
                    }
                    val radicand = arg()
                    when (index) {
                        null, "", "2" -> out.add("sqrt($radicand)")
                        "3" -> out.add("cbrt($radicand)")
                        else -> out.add("($radicand)^(1/($index))")
                    }
                }
                cmd == "left" -> {
                    val opener = if (peek() != null) take() else "."
                    val func = if (opener == ".") "" else DELIMITERS[opener]?.first ?: ""
                    val inner = join(seq(setOf("\\right")))
                    if (peek() == "\\right") {
                        i++
                        if (peek() != null) i++ // the closing delimiter
                    }
                    out.add("$func($inner)")
                }
                cmd == "right" -> { // unmatched
                    if (peek() != null) i++
                    out.add(")")
                }
                "\\$cmd" in DELIMITERS -> {
                    val (func, closer) = DELIMITERS.getValue("\\$cmd")
                    out.add("$func(${group(closer)})")
                }
                cmd in OPERATOR_COMMANDS -> out.add(OPERATOR_COMMANDS.getValue(cmd))
                cmd in CONSTANT_COMMANDS -> out.add(CONSTANT_COMMANDS.getValue(cmd))
                cmd in COMMAND_FUNCTIONS -> function(out, COMMAND_FUNCTIONS.getValue(cmd))
                cmd in NAME_COMMANDS -> {
                    val text = rawArg()
                    if (!isName(text)) throw LatexError("Unknown name ${PyText.repr(text)}")
                    if (text in FUNCTION_NAMES) function(out, text) else out.add(text)
                }
                cmd == "placeholder" -> throw LatexError("Fill in the empty box first")
                else -> throw LatexError("The calculator doesn't know \\$cmd")
            }
        }

        fun function(out: MutableList<String>, name0: String) {
            var name = name0
            var power: String? = null
            var base: String? = null
            while (peek() == "^" || peek() == "_") {
                if (take() == "^") power = arg() else base = arg()
            }
            if ((power == "-1" || power == "(-1)") && name in INVERSES) {
                name = INVERSES.getValue(name)
                power = null
            }
            val tok = peek()
            val inner: String
            if (tok == "(") {
                i++
                inner = group(")")
            } else if (tok == "\\left" && peek(1) == "(") {
                i++
                val parts = ArrayList<String>()
                command(parts, "left")
                inner = parts[0].substring(1, parts[0].length - 1)
            } else { // \sin 2x
                val parts = ArrayList<String>()
                while (true) {
                    val t = peek() ?: break
                    if (t in BARE_STOP) break
                    item(parts)
                }
                inner = join(parts)
            }
            val call = if (base != null) "$name($inner, $base)" else "$name($inner)"
            out.add(if (power != null) "($call)^($power)" else call)
        }
    }
}
