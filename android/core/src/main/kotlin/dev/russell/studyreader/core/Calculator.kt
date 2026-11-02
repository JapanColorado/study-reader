package dev.russell.studyreader.core

import java.math.BigDecimal
import java.math.BigInteger
import java.math.MathContext
import java.math.RoundingMode
import java.text.Normalizer
import kotlin.math.abs
import kotlin.math.floor

/** An expression the calculator can't evaluate, worded for the user (port of calculator.CalcError). */
class CalcError(message: String) : Exception(message)

/**
 * Port of the desktop's `Calculator.compute` (calculator.py) with degrees off and no variables or `ans`.
 *
 * Expressions are read with Python's expression grammar, as `ast.parse` reads them, and evaluated the way
 * the desktop does: whole numbers and decimals stay exact (rationals over BigInteger, like Python's ints and
 * `Fraction`s), functions and constants give doubles. That keeps the desktop's errors (results too large,
 * int-only functions given fractions) and its verdicts; only the final value is converted to a Double.
 */
object Calc {
    const val MAX_INT_BITS = 14_000
    const val MAX_FACTORIAL = 1_000

    // Kotlin-side limits where Python would only fail by recursing or computing for a very long time.
    private const val MAX_PARSE_DEPTH = 1_000
    private const val MAX_EVAL_DEPTH = 2_000
    private const val MAX_FACTORS = 20_000L
    private const val MAX_ROUND_DIGITS = 20_000

    /** Value of an expression as a Double. Throws [CalcError] with the desktop's messages. */
    fun compute(expr: String): Double = try {
        computeFloat(expr)
    } catch (e: ArithmeticException) {
        throw CalcError("Result is too large")
    }

    /**
     * quiz._number: float(Calculator().compute(expr)). An exact result beyond the double range throws
     * ArithmeticException, where Python's float() raises an OverflowError the desktop never catches.
     */
    internal fun computeFloat(expr: String): Double {
        val value = computeExact(expr)
        return try {
            value.float()
        } catch (e: PyErr) {
            throw ArithmeticException("Result is too large")
        }
    }

    internal fun computeExact(text: String): Num {
        val expr = PyText.strip(text).replace("^", "**").replace("×", "*").replace("÷", "/").replace("−", "-")
        if (PyText.strip(expr).isEmpty()) throw CalcError("Nothing to calculate")
        val tree = try {
            PyParser(PyLexer(expr).lex()).parse()
        } catch (e: PyErr) {
            throw CalcError("Couldn't read that expression")
        }
        val value = try {
            eval(tree, 0)
        } catch (e: PyErr) {
            throw CalcError(
                when (e.kind) {
                    PyErr.Kind.ZERO -> "Division by zero"
                    PyErr.Kind.OVERFLOW -> "Result is too large"
                    else -> friendly(e.message ?: "")
                },
            )
        }
        if (value is F && value.v.isNaN()) throw CalcError("Result is not a number")
        if (bits(value) > MAX_INT_BITS) throw CalcError("Result is too large")
        return value
    }

    private fun friendly(text: String): String = when {
        "math domain error" in text -> "Outside the function's domain"
        "argument" in text || "expected" in text -> "Wrong number of values for that function"
        else -> text.take(1).uppercase() + text.drop(1)
    }

    // -- numbers ---------------------------------------------------------------------------

    /** A Python number: an int or Fraction ([Q], normalized; an int when d == 1) or a float ([F]). */
    internal sealed class Num {
        abstract fun float(): Double
        abstract val typeName: String
    }

    internal class Q private constructor(val n: BigInteger, val d: BigInteger) : Num() {
        val isInt get() = d == BigInteger.ONE
        override val typeName get() = if (isInt) "int" else "Fraction"
        override fun float() = ratToDouble(n, d)

        companion object {
            fun of(n: BigInteger, d: BigInteger = BigInteger.ONE): Q {
                if (d.signum() == 0) throw PyErr(PyErr.Kind.ZERO)
                if (d == BigInteger.ONE) return Q(n, d)
                var g = n.gcd(d)
                if (d.signum() < 0) g = g.negate()
                return Q(n.divide(g), d.divide(g))
            }

            fun int(v: Long) = Q(BigInteger.valueOf(v), BigInteger.ONE)

            /** The exact value of a finite double (Fraction.from_float). */
            fun exact(v: Double): Q {
                val bd = BigDecimal(v)
                return decimal(bd)
            }

            fun decimal(bd: BigDecimal): Q = if (bd.scale() > 0) {
                of(bd.unscaledValue(), BigInteger.TEN.pow(bd.scale()))
            } else {
                of(bd.unscaledValue().multiply(BigInteger.TEN.pow(-bd.scale())))
            }
        }
    }

    internal class F(val v: Double) : Num() {
        override val typeName get() = "float"
        override fun float() = v
    }

    /** A Python exception inside an evaluation, mapped to the desktop's messages by [computeExact]. */
    internal class PyErr(val kind: Kind, message: String = kind.name) : Exception(message) {
        enum class Kind { ZERO, OVERFLOW, VALUE, TYPE, SYNTAX }
    }

    private fun zeroDiv(): Nothing = throw PyErr(PyErr.Kind.ZERO)
    private fun overflow(): Nothing = throw PyErr(PyErr.Kind.OVERFLOW)
    private fun valueError(msg: String): Nothing = throw PyErr(PyErr.Kind.VALUE, msg)
    private fun typeError(msg: String): Nothing = throw PyErr(PyErr.Kind.TYPE, msg)
    private fun domainError(): Nothing = valueError("math domain error")
    private fun wrongCount(): Nothing = typeError("wrong number of arguments")

    /** a / b correctly rounded, like Python's int true division; OverflowError past the double range. */
    internal fun ratToDouble(n: BigInteger, d: BigInteger): Double {
        if (n.signum() == 0) return 0.0
        val a = n.abs()
        val shift = 55 - (a.bitLength() - d.bitLength())
        val qr = if (shift >= 0) a.shiftLeft(shift).divideAndRemainder(d) else a.divideAndRemainder(d.shiftLeft(-shift))
        var q = qr[0].toLong() // 55 or 56 bits
        if (qr[1].signum() != 0) q = q or 1L // sticky bit, so the rounding below is correct
        val r = Math.scalb(q.toDouble(), -shift)
        if (r.isInfinite()) overflow()
        return if (n.signum() < 0) -r else r
    }

    private fun bits(x: Num): Int = if (x is Q) maxOf(x.n.abs().bitLength(), x.d.bitLength()) else 0

    private fun floorDiv(a: BigInteger, b: BigInteger): BigInteger {
        val qr = a.divideAndRemainder(b)
        return if (qr[1].signum() != 0 && qr[1].signum() != b.signum()) qr[0].subtract(BigInteger.ONE) else qr[0]
    }

    private fun floorMod(a: BigInteger, b: BigInteger): BigInteger {
        val r = a.rem(b)
        return if (r.signum() != 0 && r.signum() != b.signum()) r.add(b) else r
    }

    private fun add(a: Num, b: Num): Num =
        if (a is Q && b is Q) Q.of(a.n * b.d + b.n * a.d, a.d * b.d) else F(a.float() + b.float())

    private fun sub(a: Num, b: Num): Num =
        if (a is Q && b is Q) Q.of(a.n * b.d - b.n * a.d, a.d * b.d) else F(a.float() - b.float())

    private fun mul(a: Num, b: Num): Num =
        if (a is Q && b is Q) Q.of(a.n * b.n, a.d * b.d) else F(a.float() * b.float())

    private fun div(a: Num, b: Num): Num {
        if (a is Q && b is Q) {
            if (b.n.signum() == 0) zeroDiv()
            return Q.of(a.n * b.d, a.d * b.n)
        }
        val x = a.float()
        val y = b.float()
        if (y == 0.0) zeroDiv()
        return F(x / y)
    }

    private fun floordiv(a: Num, b: Num): Num {
        if (a is Q && b is Q) {
            if (b.n.signum() == 0) zeroDiv()
            return Q.of(floorDiv(a.n * b.d, a.d * b.n))
        }
        val x = a.float()
        val y = b.float()
        if (y == 0.0) zeroDiv()
        return F(floatDivMod(x, y).first)
    }

    private fun mod(a: Num, b: Num): Num {
        if (a is Q && b is Q) {
            if (b.n.signum() == 0) zeroDiv()
            return Q.of(floorMod(a.n * b.d, b.n * a.d), a.d * b.d)
        }
        val x = a.float()
        val y = b.float()
        if (y == 0.0) zeroDiv()
        var m = x % y // C fmod
        if (m != 0.0) {
            if ((y < 0) != (m < 0)) m += y
        } else {
            m = Math.copySign(0.0, y)
        }
        return F(m)
    }

    /** CPython's _float_div_mod: (floor quotient, modulus). */
    private fun floatDivMod(vx: Double, wx: Double): Pair<Double, Double> {
        var m = vx % wx
        var d = (vx - m) / wx
        if (m != 0.0 || m.isNaN()) {
            if ((wx < 0) != (m < 0)) {
                m += wx
                d -= 1.0
            }
        } else {
            m = Math.copySign(0.0, wx)
        }
        val fd: Double
        if (d != 0.0 || d.isNaN()) {
            var f = floor(d)
            if (d - f > 0.5) f += 1.0
            fd = f
        } else {
            fd = Math.copySign(0.0, vx / wx)
        }
        return fd to m
    }

    /** calculator._pow: exact for rationals to whole powers (within MAX_INT_BITS), floats otherwise. */
    private fun pow(a: Num, b: Num): Num {
        if (a is Q && b is Q && b.isInt) {
            val e = b.n
            val unit = a.isInt && a.n.abs() <= BigInteger.ONE
            if (e.abs() > BigInteger.ONE && !unit &&
                BigInteger.valueOf(bits(a).toLong()) * e.abs() > BigInteger.valueOf(MAX_INT_BITS.toLong())
            ) {
                throw CalcError("Result is too large")
            }
            return when {
                e.signum() >= 0 -> Q.of(bigPow(a.n, e), bigPow(a.d, e))
                a.n.signum() > 0 -> Q.of(bigPow(a.d, e.negate()), bigPow(a.n, e.negate()))
                a.n.signum() == 0 -> zeroDiv()
                else -> Q.of(bigPow(a.d.negate(), e.negate()), bigPow(a.n.negate(), e.negate()))
            }
        }
        return F(floatPow(a.float(), b.float()))
    }

    private fun bigPow(base: BigInteger, e: BigInteger): BigInteger = when {
        e.signum() == 0 -> BigInteger.ONE
        base.signum() == 0 || base == BigInteger.ONE -> base
        base == BigInteger.ONE.negate() -> if (e.testBit(0)) base else BigInteger.ONE
        else -> base.pow(e.intValueExact())
    }

    private fun isOddInteger(x: Double) = abs(x) % 2.0 == 1.0

    /** CPython's float_pow, with its complex results reported as the desktop does. */
    private fun floatPow(iv0: Double, iw: Double): Double {
        var iv = iv0
        if (iw == 0.0) return 1.0
        if (iv.isNaN()) return iv
        if (iw.isNaN()) return if (iv == 1.0) 1.0 else iw
        if (iw.isInfinite()) {
            iv = abs(iv)
            return when {
                iv == 1.0 -> 1.0
                (iw > 0.0) == (iv > 1.0) -> abs(iw)
                else -> 0.0
            }
        }
        if (iv.isInfinite()) {
            val odd = isOddInteger(iw)
            return if (iw > 0.0) (if (odd) iv else abs(iv)) else (if (odd) Math.copySign(0.0, iv) else 0.0)
        }
        if (iv == 0.0) {
            val odd = isOddInteger(iw)
            if (iw < 0.0) zeroDiv()
            return if (odd) iv else 0.0
        }
        var negate = false
        if (iv < 0.0) {
            if (iw != floor(iw)) {
                // Python answers with complex pow, which overflows when the magnitude does
                if (Math.pow(-iv, iw).isInfinite()) overflow()
                throw CalcError("Result is not a real number")
            }
            iv = -iv
            negate = isOddInteger(iw)
        }
        if (iv == 1.0) return if (negate) -1.0 else 1.0
        var ix = Math.pow(iv, iw)
        if (negate) ix = -ix
        if (ix.isInfinite()) overflow()
        return ix
    }

    private fun neg(x: Num): Num = if (x is Q) Q.of(x.n.negate(), x.d) else F(-(x as F).v)

    /** Python's a < b across ints, Fractions and floats: exact, and false with a NaN. */
    private fun lt(a: Num, b: Num): Boolean {
        val x = (a as? F)?.v
        val y = (b as? F)?.v
        if (x != null && x.isNaN() || y != null && y.isNaN()) return false
        if (x != null && x.isInfinite() || y != null && y.isInfinite()) {
            return when {
                x != null && y != null -> x < y
                x != null -> x < 0 // -inf < finite
                else -> y!! > 0
            }
        }
        val p = if (a is Q) a else Q.exact(x!!)
        val q = if (b is Q) b else Q.exact(y!!)
        return (p.n * q.d) < (q.n * p.d)
    }

    // -- evaluation ------------------------------------------------------------------------

    private fun eval(node: Node, depth: Int): Num {
        if (depth > MAX_EVAL_DEPTH) throw CalcError("Couldn't read that expression")
        return when (node) {
            is ConstN -> node.v
            is NameN -> lookup(node.id)
            is BinN -> {
                val l = eval(node.l, depth + 1)
                val r = eval(node.r, depth + 1)
                when (node.op) {
                    "+" -> add(l, r)
                    "-" -> sub(l, r)
                    "*" -> mul(l, r)
                    "/" -> div(l, r)
                    "//" -> floordiv(l, r)
                    "%" -> mod(l, r)
                    else -> pow(l, r)
                }
            }
            is UnaryN -> {
                val x = eval(node.x, depth + 1)
                if (node.negate) neg(x) else x
            }
            is CallN -> call(node.name, node.args.map { eval(it, depth + 1) })
            else -> throw CalcError("Couldn't read that expression")
        }
    }

    private val CONSTANTS = mapOf("pi" to Math.PI, "e" to Math.E, "tau" to 2 * Math.PI, "inf" to Double.POSITIVE_INFINITY)
    private val FUNCTIONS = setOf(
        "sqrt", "cbrt", "exp", "ln", "log", "log10", "log2", "abs", "floor", "ceil", "round", "trunc",
        "factorial", "gcd", "lcm", "comb", "perm", "hypot", "min", "max", "sinh", "cosh", "tanh",
        "asinh", "acosh", "atanh", "deg", "rad",
    )
    private val TRIG = setOf("sin", "cos", "tan")
    private val INVERSE_TRIG = setOf("asin", "acos", "atan", "atan2")

    private fun lookup(name: String): Num {
        if (name == "ans") throw CalcError("No previous answer yet")
        CONSTANTS[name]?.let { return F(it) }
        if (name.length > 1 && name.all { it == 'e' }) { // "ee" is e times e, as written in math
            var p: Num = Q.int(1)
            repeat(name.length) { p = mul(p, F(Math.E)) }
            return p
        }
        if (name in FUNCTIONS || name in TRIG || name in INVERSE_TRIG) {
            throw CalcError("'$name' is a function — try $name(…)")
        }
        throw CalcError("Unknown name '$name'")
    }

    private fun one(args: List<Num>): Num {
        if (args.size != 1) wrongCount()
        return args[0]
    }

    /** CPython's math_1: the domain and range checks around a libm function. */
    private fun math1(x: Num, canOverflow: Boolean, f: (Double) -> Double): Num {
        val d = x.float()
        val r = f(d)
        if (r.isNaN() && !d.isNaN()) domainError()
        if (r.isInfinite() && d.isFinite()) if (canOverflow) overflow() else domainError()
        return F(r)
    }

    private fun index(x: Num): BigInteger {
        if (x is Q && x.isInt) return x.n
        typeError("'${x.typeName}' object cannot be interpreted as an integer")
    }

    private fun call(name: String, args: List<Num>): Num {
        if (name in TRIG) {
            if (args.size != 1) throw CalcError("$name takes one value")
            val v = math1(args[0], false) {
                when (name) {
                    "sin" -> Math.sin(it)
                    "cos" -> Math.cos(it)
                    else -> Math.tan(it)
                }
            } as F
            return if (abs(v.v) < 1e-15) F(0.0) else v // sin(pi) is 1.2e-16, not a meaningful answer
        }
        return when (name) {
            "asin" -> math1(one(args), false, Math::asin)
            "acos" -> math1(one(args), false, Math::acos)
            "atan" -> math1(one(args), false, Math::atan)
            "atan2" -> {
                if (args.size != 2) wrongCount()
                F(Math.atan2(args[0].float(), args[1].float()))
            }
            "sqrt" -> math1(one(args), false, Math::sqrt)
            "cbrt" -> math1(one(args), false, ::cbrt)
            "exp" -> math1(one(args), true, Math::exp)
            "ln", "log" -> {
                if (args.isEmpty() || args.size > 2) wrongCount()
                val num = logOf(args[0], LogKind.LN)
                if (args.size == 1) return F(num)
                val den = logOf(args[1], LogKind.LN)
                if (den == 0.0) zeroDiv()
                F(num / den)
            }
            "log10" -> F(logOf(one(args), LogKind.LOG10))
            "log2" -> F(logOf(one(args), LogKind.LOG2))
            "abs" -> one(args).let { if (it is Q) Q.of(it.n.abs(), it.d) else F(abs((it as F).v)) }
            "floor" -> one(args).let { if (it is Q) Q.of(floorDiv(it.n, it.d)) else Q.of(toInt(floor((it as F).v))) }
            "ceil" -> one(args).let {
                if (it is Q) Q.of(floorDiv(it.n.negate(), it.d).negate()) else Q.of(toInt(Math.ceil((it as F).v)))
            }
            "trunc" -> one(args).let {
                if (it is Q) Q.of(it.n.divide(it.d)) else Q.of(toInt((it as F).v.let { v -> if (v < 0) Math.ceil(v) else floor(v) }))
            }
            "round" -> round(args)
            "factorial" -> factorial(one(args))
            "gcd" -> {
                var g = BigInteger.ZERO
                for (a in args) g = g.gcd(index(a))
                Q.of(g)
            }
            "lcm" -> {
                var l = BigInteger.ONE
                for (a in args) {
                    val x = index(a)
                    l = if (l.signum() == 0 || x.signum() == 0) BigInteger.ZERO else (l * x).abs().divide(l.gcd(x))
                }
                Q.of(l)
            }
            "comb" -> {
                if (args.size != 2) wrongCount()
                val n = index(args[0])
                val k = index(args[1])
                if (n.signum() < 0) valueError("n must be a non-negative integer")
                if (k.signum() < 0) valueError("k must be a non-negative integer")
                if (k > n) return Q.int(0)
                val m = k.min(n - k)
                if (m.bitLength() > 31 || m.toLong() * n.bitLength() > MAX_FACTORS * 64) overflow()
                var r = BigInteger.ONE
                for (i in 0 until m.toInt()) {
                    r = r * (n - BigInteger.valueOf(i.toLong())) / BigInteger.valueOf(i + 1L)
                }
                Q.of(r)
            }
            "perm" -> {
                if (args.isEmpty() || args.size > 2) wrongCount()
                val n = index(args[0])
                if (args.size == 1) {
                    if (n.signum() < 0) valueError("factorial() not defined for negative values")
                    if (n > BigInteger.valueOf(MAX_FACTORS / 4)) overflow()
                    return Q.of(product(n, n.toInt()))
                }
                val k = index(args[1])
                if (n.signum() < 0) valueError("n must be a non-negative integer")
                if (k.signum() < 0) valueError("k must be a non-negative integer")
                if (k > n) return Q.int(0)
                if (k.bitLength() > 31 || k.toLong() * n.bitLength() > MAX_FACTORS * 64) overflow()
                Q.of(product(n, k.toInt()))
            }
            "hypot" -> {
                val xs = args.map { it.float() }
                F(hypot(xs))
            }
            "min", "max" -> {
                if (args.isEmpty()) wrongCount()
                if (args.size == 1) typeError("'${args[0].typeName}' object is not iterable")
                var best = args[0]
                for (x in args.drop(1)) {
                    if (if (name == "min") lt(x, best) else lt(best, x)) best = x
                }
                best
            }
            "sinh" -> math1(one(args), true, Math::sinh)
            "cosh" -> math1(one(args), true, Math::cosh)
            "tanh" -> math1(one(args), false, Math::tanh)
            "asinh" -> math1(one(args), false, ::asinh)
            "acosh" -> math1(one(args), false, ::acosh)
            "atanh" -> math1(one(args), false, ::atanh)
            "deg" -> F(one(args).float() * (180.0 / Math.PI))
            "rad" -> F(one(args).float() * (Math.PI / 180.0))
            else -> throw CalcError("Unknown function '$name'")
        }
    }

    /** n (n-1) ... (n-k+1) */
    private fun product(n: BigInteger, k: Int): BigInteger {
        var r = BigInteger.ONE
        for (i in 0 until k) r *= n - BigInteger.valueOf(i.toLong())
        return r
    }

    /** PyLong_FromDouble: the integer value of a whole double. */
    private fun toInt(v: Double): BigInteger {
        if (v.isNaN()) valueError("cannot convert float NaN to integer")
        if (v.isInfinite()) throw PyErr(PyErr.Kind.OVERFLOW, "cannot convert float infinity to integer")
        return BigDecimal(v).toBigInteger()
    }

    private fun factorial(n0: Num): Num {
        var n = n0
        if (n is F) {
            val whole = toInt(if (n.v < 0) Math.ceil(n.v) else floor(n.v)) // int(n), raising for inf and NaN
            if (n.v == floor(n.v)) n = Q.of(whole)
        }
        if (n !is Q || !n.isInt || n.n.signum() < 0) throw CalcError("factorial needs a non-negative whole number")
        if (n.n > BigInteger.valueOf(MAX_FACTORIAL.toLong())) throw CalcError("Result is too large")
        return Q.of(product(n.n, n.n.toInt()))
    }

    private enum class LogKind { LN, LOG10, LOG2 }

    private fun log2(x: Double): Double {
        val bitsOf = java.lang.Double.doubleToRawLongBits(x)
        if (x > 0 && x.isFinite() && (bitsOf and 0x000f_ffff_ffff_ffffL) == 0L && Math.getExponent(x) >= -1022) {
            return Math.getExponent(x).toDouble() // exact for powers of two
        }
        if (!(x > 0) || x.isInfinite() || Math.getExponent(x) < java.lang.Double.MIN_EXPONENT) return Math.log(x) / LN2
        // x = m 2^e with m in [sqrt(1/2), sqrt(2)): e + log2(m) rounds once where e dominates, as glibc's log2 does
        var e = Math.getExponent(x)
        var m = Math.scalb(x, -e)
        if (m > Math.sqrt(2.0)) {
            m /= 2
            e += 1
        }
        return e + Math.log(m) / LN2
    }

    private fun libLog(kind: LogKind, x: Double) = when (kind) {
        LogKind.LN -> Math.log(x)
        LogKind.LOG10 -> Math.log10(x)
        LogKind.LOG2 -> log2(x)
    }

    /** CPython's loghelper: ints of any size, others through math_1. */
    private fun logOf(x: Num, kind: LogKind): Double {
        if (x is Q && x.isInt) {
            if (x.n.signum() <= 0) domainError()
            val d = try {
                x.float()
            } catch (e: PyErr) {
                null
            }
            if (d != null) return libLog(kind, d)
            val e = x.n.bitLength()
            val m = ratToDouble(x.n, BigInteger.ONE.shiftLeft(e)) // in [0.5, 1]
            return libLog(kind, m) + libLog(kind, 2.0) * e
        }
        val d = x.float()
        val r = when {
            d.isFinite() && d > 0 -> libLog(kind, d)
            d.isFinite() && d == 0.0 -> Double.NEGATIVE_INFINITY
            d.isFinite() -> Double.NaN
            d.isNaN() -> d
            d > 0 -> d
            else -> Double.NaN
        }
        if (r.isNaN() && !d.isNaN()) domainError()
        if (r.isInfinite() && d.isFinite()) domainError()
        return r
    }

    /** math.hypot, correctly rounded as CPython's almost always is (from the exact sum of squares). */
    private fun hypot(xs: List<Double>): Double {
        if (xs.isEmpty()) return 0.0
        if (xs.any { it.isInfinite() }) return Double.POSITIVE_INFINITY
        if (xs.any { it.isNaN() }) return Double.NaN
        if (xs.size == 1) return abs(xs[0])
        // each |x| = m 2^e
        val parts = xs.filter { it != 0.0 }.map {
            val raw = java.lang.Double.doubleToRawLongBits(abs(it))
            val exp = ((raw ushr 52) and 0x7ff).toInt()
            val mant = raw and 0x000f_ffff_ffff_ffffL
            if (exp == 0) mant to -1074 else (mant or (1L shl 52)) to exp - 1075
        }
        if (parts.isEmpty()) return 0.0
        val e0 = parts.minOf { it.second }
        var sum = BigInteger.ZERO
        for ((m, e) in parts) sum += BigInteger.valueOf(m).shiftLeft(e - e0).pow(2)
        val k = maxOf(0, (124 - sum.bitLength()) / 2 + 1) // so the root has 62 or more bits
        val t = sum.shiftLeft(2 * k)
        var r = isqrt(t)
        var sticky = r * r != t
        var scale = e0 - k
        val extra = r.bitLength() - 62
        if (extra > 0) {
            if (r.and(BigInteger.ONE.shiftLeft(extra) - BigInteger.ONE).signum() != 0) sticky = true
            r = r.shiftRight(extra)
            scale += extra
        }
        var q = r.toLong()
        if (sticky) q = q or 1L
        return Math.scalb(q.toDouble(), scale)
    }

    private fun isqrt(n: BigInteger): BigInteger {
        if (n.signum() == 0) return n
        var x = BigInteger.ONE.shiftLeft((n.bitLength() + 1) / 2)
        while (true) {
            val y = (x + n / x).shiftRight(1)
            if (y >= x) return x
            x = y
        }
    }

    /** glibc's cbrt (sysdeps/ieee754/dbl-64/s_cbrt.c), which Python's math.cbrt calls on the desktop. */
    private fun cbrt(x: Double): Double {
        if (x == 0.0 || !x.isFinite()) return x + x
        var a = abs(x)
        var shift = 0
        if (Math.getExponent(a) < java.lang.Double.MIN_EXPONENT) { // subnormal: frexp after scaling up
            a *= TWO54
            shift = 54
        }
        val xe = Math.getExponent(a) + 1 - shift
        val xm = Math.scalb(a, -(Math.getExponent(a) + 1)) // in [0.5, 1)
        val u = 0.354895765043919860 + ((1.50819193781584896 + ((-2.11499494167371287 + ((2.44693122563534430 +
            ((-1.83469277483613086 + (0.784932344976639262 - 0.145263899385486377 * xm) * xm) * xm)) * xm)) * xm)) * xm)
        val t2 = u * u * u
        val ym = u * (t2 + 2.0 * xm) / (2.0 * t2 + xm) * CBRT_FACTOR[2 + xe % 3]
        return Math.scalb(if (x > 0.0) ym else -ym, xe / 3)
    }

    private const val TWO54 = 18014398509481984.0
    private const val CBRT2 = 1.2599210498948731648
    private const val SQR_CBRT2 = 1.5874010519681994748
    private val CBRT_FACTOR = doubleArrayOf(1.0 / SQR_CBRT2, 1.0 / CBRT2, 1.0, CBRT2, SQR_CBRT2)

    // fdlibm's algorithms, as C libraries use (Java has no asinh/acosh/atanh).
    private const val LN2 = 6.93147180559945286227e-01
    private const val TWO28 = 268435456.0

    private fun asinh(x: Double): Double {
        if (x.isNaN() || x.isInfinite()) return x
        val a = abs(x)
        if (a < 1.0 / TWO28) return x
        val w = when {
            a > TWO28 -> Math.log(a) + LN2
            a > 2.0 -> Math.log(2.0 * a + 1.0 / (Math.sqrt(x * x + 1.0) + a))
            else -> Math.log1p(a + x * x / (1.0 + Math.sqrt(1.0 + x * x)))
        }
        return Math.copySign(w, x)
    }

    private fun acosh(x: Double): Double = when {
        x.isNaN() -> x
        x < 1.0 -> Double.NaN
        x >= TWO28 -> if (x.isInfinite()) x else Math.log(x) + LN2
        x == 1.0 -> 0.0
        x > 2.0 -> Math.log(2.0 * x - 1.0 / (x + Math.sqrt(x * x - 1.0)))
        else -> {
            val t = x - 1.0
            Math.log1p(t + Math.sqrt(2.0 * t + t * t))
        }
    }

    private fun atanh(x: Double): Double {
        if (x.isNaN()) return x
        val a = abs(x)
        if (a > 1.0) return Double.NaN
        if (a == 1.0) return Math.copySign(Double.POSITIVE_INFINITY, x)
        if (a < 1.0 / TWO28) return x
        val t = if (a < 0.5) {
            val t2 = a + a
            0.5 * Math.log1p(t2 + t2 * a / (1.0 - a))
        } else {
            0.5 * Math.log1p((a + a) / (1.0 - a))
        }
        return Math.copySign(t, x)
    }

    /** Python's round(x) and round(x, n) for ints, Fractions and floats. */
    private fun round(args: List<Num>): Num {
        if (args.isEmpty() || args.size > 2) wrongCount()
        val x = args[0]
        if (args.size == 1) {
            return when (x) {
                is Q -> if (x.isInt) x else Q.of(roundHalfEven(x.n, x.d))
                is F -> Q.of(toInt(Math.rint(x.v)))
            }
        }
        val nd = args[1]
        when {
            x is F -> {
                val digits = index(nd)
                val v = x.v
                if (!v.isFinite()) return x
                if (digits > BigInteger.valueOf(323)) return x
                if (digits < BigInteger.valueOf(-308)) return F(0.0 * v)
                val r = BigDecimal(v).setScale(digits.toInt(), RoundingMode.HALF_EVEN).toString().toDouble()
                if (r.isInfinite()) overflow()
                return F(if (r == 0.0) Math.copySign(0.0, v) else r)
            }
            x is Q && x.isInt -> {
                val digits = index(nd)
                if (digits.signum() >= 0) return x
                val m = digits.negate()
                // 10^m beyond twice |x| rounds everything to 0
                if (m > BigInteger.valueOf(x.n.abs().toString().length + 1L)) return Q.int(0)
                val p = BigInteger.TEN.pow(m.toInt())
                return Q.of(roundHalfEven(x.n, p) * p)
            }
            x is Q -> return roundFraction(x, nd)
        }
        throw IllegalStateException()
    }

    /** Fraction.__round__(ndigits) */
    private fun roundFraction(x: Q, nd: Num): Num {
        if (nd is Q && nd.isInt) {
            val digits = nd.n
            if (digits.abs() > BigInteger.valueOf(MAX_ROUND_DIGITS.toLong())) {
                if (digits.signum() < 0) return Q.int(0)
                throw CalcError("Result is too large")
            }
            val shift = BigInteger.TEN.pow(digits.abs().toInt())
            return if (digits.signum() > 0) {
                Q.of(roundHalfEven(x.n * shift, x.d), shift)
            } else {
                Q.of(roundHalfEven(x.n, x.d * shift) * shift)
            }
        }
        // A non-integer ndigits: shift = 10 ** abs(ndigits) is a float.
        val ndv = nd.float()
        val shift = floatPow(10.0, abs(ndv))
        if (ndv > 0) typeError("both arguments should be Rational instances")
        val q = x.float() / shift
        val rounded = toInt(Math.rint(q)) // round(Fraction / float) is round(float)
        val v = ratToDouble(rounded, BigInteger.ONE) * shift
        if (v.isInfinite()) throw PyErr(PyErr.Kind.OVERFLOW, "cannot convert Infinity to integer ratio")
        if (v.isNaN()) valueError("cannot convert NaN to integer ratio")
        return Q.exact(v)
    }

    private fun roundHalfEven(n: BigInteger, d: BigInteger): BigInteger {
        val fl = floorDiv(n, d)
        val rem = n - fl * d
        val twice = rem.shiftLeft(1)
        return when {
            twice < d -> fl
            twice > d -> fl + BigInteger.ONE
            !fl.testBit(0) -> fl
            else -> fl + BigInteger.ONE
        }
    }

    // -- parsing (Python's expression grammar) --------------------------------------------------

    internal sealed class Node
    private class ConstN(val v: Num) : Node()
    private class NameN(val id: String) : Node()
    private class BinN(val op: String, val l: Node, val r: Node) : Node()
    private class UnaryN(val x: Node, val negate: Boolean) : Node()
    private class CallN(val name: String, val args: List<Node>) : Node()

    /** Valid Python the calculator doesn't evaluate (comparisons, strings, tuples…); raises when reached. */
    private object BadN : Node()

    /** A starred item (`*a`): unevaluable too, and not allowed alone. */
    private object StarN : Node()

    private enum class T { NAME, NUMBER, STRING, OP, NEWLINE, END }

    private class Tok(val t: T, val s: String, val num: Node? = null, val kw: Boolean = false, val bytes: Boolean = false)

    private fun syntax(): Nothing = throw PyErr(PyErr.Kind.SYNTAX)

    private val KEYWORDS = setOf(
        "False", "None", "True", "and", "as", "assert", "async", "await", "break", "class", "continue", "def",
        "del", "elif", "else", "except", "finally", "for", "from", "global", "if", "import", "in", "is", "lambda",
        "nonlocal", "not", "or", "pass", "raise", "return", "try", "while", "with", "yield",
    )
    private val STRING_PREFIXES = setOf("r", "u", "b", "br", "rb", "f", "fr", "rf")
    private val OPS3 = setOf("**=", "//=", ">>=", "<<=", "...")
    private val OPS2 = setOf(
        "**", "//", "<<", ">>", "<=", ">=", "==", "!=", "->", ":=", "+=", "-=", "*=", "/=", "%=", "&=", "|=", "^=", "@=",
    )
    private const val OPS1 = "+-*/%@<>=|&^~()[]{},:.;"
    private const val MAX_PAREN_LEVEL = 200

    /** Python's tokenizer, as far as expressions need it. */
    private class PyLexer(src: String) {
        val cps: IntArray = src.codePoints().toArray()
        var i = 0
        val toks = ArrayList<Tok>()
        val brackets = ArrayList<Int>()

        fun at(k: Int = 0): Int = if (i + k < cps.size) cps[i + k] else -1

        fun isDigit(c: Int) = c in '0'.code..'9'.code
        fun isAsciiLetter(c: Int) = c in 'a'.code..'z'.code || c in 'A'.code..'Z'.code
        fun isIdentStart(c: Int) = isAsciiLetter(c) || c == '_'.code || c >= 128
        fun isIdentChar(c: Int) = isIdentStart(c) || isDigit(c)

        fun lex(): List<Tok> {
            while (true) {
                val c = at()
                when {
                    c == -1 -> break
                    c == ' '.code || c == '\t'.code || c == 0x0c -> i++
                    c == '#'.code -> while (at() != -1 && at() != '\n'.code && at() != '\r'.code) i++
                    c == '\\'.code -> when (at(1)) {
                        '\n'.code -> i += 2
                        '\r'.code -> {
                            i += 2
                            if (at() == '\n'.code) i++
                        }
                        else -> syntax()
                    }
                    c == '\n'.code || c == '\r'.code -> {
                        i++
                        if (c == '\r'.code && at() == '\n'.code) i++
                        if (brackets.isEmpty() && toks.isNotEmpty() && toks.last().t != T.NEWLINE) toks.add(Tok(T.NEWLINE, ""))
                    }
                    c == 0 -> syntax()
                    isDigit(c) || (c == '.'.code && isDigit(at(1))) -> number()
                    isIdentStart(c) -> nameOrString()
                    c == '"'.code || c == '\''.code -> string(false)
                    else -> op()
                }
            }
            if (brackets.isNotEmpty()) syntax()
            toks.add(Tok(T.END, ""))
            return toks
        }

        fun lookahead(s: String): Boolean = s.indices.all { at(1 + it) == s[it].code }

        /** verify_end_of_number: a literal can't run into a name, except a keyword like `1if`. */
        fun verifyEnd() {
            val c = at()
            val keyword = when (c) {
                'a'.code -> lookahead("nd")
                'e'.code -> lookahead("lse")
                'f'.code -> lookahead("or")
                'i'.code -> at(1) == 'f'.code || at(1) == 'n'.code || at(1) == 's'.code
                'o'.code -> lookahead("r")
                'n'.code -> lookahead("ot")
                else -> false
            }
            if (!keyword && c in 0..127 && isIdentChar(c)) syntax()
        }

        fun decimalTail() {
            while (isDigit(at())) i++
            while (at() == '_'.code) {
                i++
                if (!isDigit(at())) syntax()
                while (isDigit(at())) i++
            }
        }

        fun number() {
            val start = i
            val radixChar = at(1) or 0x20
            if (at() == '0'.code && (radixChar == 'x'.code || radixChar == 'o'.code || radixChar == 'b'.code)) {
                val radix = when (radixChar) {
                    'x'.code -> 16
                    'o'.code -> 8
                    else -> 2
                }
                i += 2
                do {
                    if (at() == '_'.code) i++
                    if (Character.digit(at(), radix) < 0 || at() >= 128) syntax()
                    while (at() in 0..127 && Character.digit(at(), radix) >= 0) i++
                } while (at() == '_'.code)
                verifyEnd()
                val digits = String(cps, start + 2, i - start - 2).replace("_", "")
                toks.add(Tok(T.NUMBER, "", ConstN(Q.of(BigInteger(digits, radix)))))
                return
            }
            var isFloat = false
            if (at() == '.'.code) {
                i++
                isFloat = true
                decimalTail()
            } else if (at() == '0'.code) {
                while (true) {
                    if (at() == '_'.code) {
                        i++
                        if (!isDigit(at())) syntax()
                    }
                    if (at() != '0'.code) break
                    i++
                }
                var nonzero = false
                if (isDigit(at())) {
                    nonzero = true
                    decimalTail()
                }
                val c = at()
                if (c == '.'.code) {
                    i++
                    isFloat = true
                    if (isDigit(at())) decimalTail()
                } else if (nonzero && c != 'e'.code && c != 'E'.code && c != 'j'.code && c != 'J'.code) {
                    syntax() // leading zeros in a decimal integer
                }
            } else {
                decimalTail()
                if (at() == '.'.code) {
                    i++
                    isFloat = true
                    if (isDigit(at())) decimalTail()
                }
            }
            if (at() == 'e'.code || at() == 'E'.code) {
                val ePos = i
                i++
                if (at() == '+'.code || at() == '-'.code) {
                    i++
                    if (!isDigit(at())) syntax()
                    decimalTail()
                    isFloat = true
                } else if (isDigit(at())) {
                    decimalTail()
                    isFloat = true
                } else {
                    i = ePos
                    verifyEnd()
                    emitNumber(start, isFloat, false)
                    return
                }
            }
            if (at() == 'j'.code || at() == 'J'.code) {
                i++
                verifyEnd()
                emitNumber(start, isFloat, true)
                return
            }
            verifyEnd()
            emitNumber(start, isFloat, false)
        }

        fun emitNumber(start: Int, isFloat: Boolean, imaginary: Boolean) {
            val text = String(cps, start, i - start).replace("_", "")
            val node: Node = when {
                imaginary -> BadN
                isFloat -> {
                    val v = text.toDouble()
                    // the decimal as written: 0.1 is exactly 1/10 (Fraction(repr(v)))
                    if (v.isFinite()) ConstN(Q.decimal(PyText.shortest(v))) else ConstN(F(v))
                }
                else -> {
                    if (text.length > 4300) syntax() // Python's int string conversion limit
                    ConstN(Q.of(BigInteger(text)))
                }
            }
            toks.add(Tok(T.NUMBER, text, node))
        }

        fun nameOrString() {
            val start = i
            while (isIdentChar(at())) i++
            val raw = String(cps, start, i - start)
            if ((at() == '"'.code || at() == '\''.code) && raw.lowercase() in STRING_PREFIXES) {
                string('b' in raw.lowercase())
                return
            }
            var id = raw
            if (raw.any { it.code >= 128 }) {
                // checked as written (x² is not a name), then normalized (ｘ is x)
                if (!isIdentifier(raw)) syntax()
                id = Normalizer.normalize(raw, Normalizer.Form.NFKC)
            }
            toks.add(Tok(T.NAME, id, kw = raw in KEYWORDS))
        }

        fun isIdentifier(s: String): Boolean {
            if (s.isEmpty()) return false
            val cs = s.codePoints().toArray()
            if (!(cs[0] == '_'.code || Character.isUnicodeIdentifierStart(cs[0]))) return false
            return cs.drop(1).all { Character.isUnicodeIdentifierPart(it) && !Character.isIdentifierIgnorable(it) }
        }

        fun string(bytes: Boolean) {
            val q = at()
            val triple = at(1) == q && at(2) == q
            i += if (triple) 3 else 1
            while (true) {
                val c = at()
                if (c == -1) syntax()
                if (c == '\\'.code) {
                    if (at(1) == -1) syntax()
                    i += 2
                    continue
                }
                if (!triple && (c == '\n'.code || c == '\r'.code)) syntax()
                if (c == q && (!triple || (at(1) == q && at(2) == q))) {
                    i += if (triple) 3 else 1
                    break
                }
                if (bytes && c >= 128) syntax()
                i++
            }
            toks.add(Tok(T.STRING, "", bytes = bytes))
        }

        fun op() {
            fun text(n: Int): String {
                val sb = StringBuilder()
                for (k in 0 until n) {
                    val c = at(k)
                    if (c == -1) return ""
                    sb.appendCodePoint(c)
                }
                return sb.toString()
            }
            val s = when {
                text(3) in OPS3 -> text(3)
                text(2) in OPS2 -> text(2)
                at() in 0..127 && OPS1.indexOf(at().toChar()) >= 0 -> text(1)
                else -> syntax()
            }
            when (s) {
                "(", "[", "{" -> {
                    if (brackets.size >= MAX_PAREN_LEVEL) syntax()
                    brackets.add(at())
                }
                ")", "]", "}" -> {
                    if (brackets.isEmpty()) syntax()
                    val open = brackets.removeAt(brackets.size - 1)
                    val expected = when (open) {
                        '('.code -> ")"
                        '['.code -> "]"
                        else -> "}"
                    }
                    if (s != expected) syntax()
                }
            }
            i += s.length
            toks.add(Tok(T.OP, s))
        }
    }

    private class Args(val ok: Boolean, val args: List<Node>)

    /** Python's expression grammar (ast.parse(mode="eval")), keeping only what the calculator evaluates. */
    private class PyParser(val toks: List<Tok>) {
        var p = 0
        var depth = 0

        fun peek(k: Int = 0) = toks[minOf(p + k, toks.size - 1)]
        fun isOp(s: String, k: Int = 0) = peek(k).let { it.t == T.OP && it.s == s }
        fun isKw(s: String, k: Int = 0) = peek(k).let { it.t == T.NAME && it.kw && it.s == s }
        fun isName(k: Int = 0) = peek(k).let { it.t == T.NAME && !it.kw }
        fun expectOp(s: String) {
            if (!isOp(s)) syntax()
            p++
        }

        fun expectKw(s: String) {
            if (!isKw(s)) syntax()
            p++
        }

        inline fun <R> nested(f: () -> R): R {
            if (++depth > MAX_PARSE_DEPTH) syntax()
            try {
                return f()
            } finally {
                depth--
            }
        }

        fun parse(): Node {
            val node = expressions()
            while (peek().t == T.NEWLINE) p++
            if (peek().t != T.END) syntax()
            return node
        }

        fun atEnd() = peek().t == T.END || peek().t == T.NEWLINE

        fun expressions(): Node {
            val first = starOrExpression()
            if (!isOp(",")) {
                if (first === StarN) syntax()
                return first
            }
            while (isOp(",")) {
                p++
                if (atEnd()) break
                starOrExpression()
            }
            return BadN
        }

        fun starOrExpression(): Node {
            if (isOp("*")) {
                p++
                bitOr()
                return StarN
            }
            return expression()
        }

        fun starOrNamed(): Node {
            if (isOp("*")) {
                p++
                bitOr()
                return StarN
            }
            return namedExpression()
        }

        fun expression(): Node = nested {
            if (isKw("lambda")) {
                lambdef()
                return@nested BadN
            }
            val d = disjunction()
            if (isKw("if")) {
                p++
                disjunction()
                expectKw("else")
                expression()
                return@nested BadN
            }
            d
        }

        fun namedExpression(): Node {
            if (isName() && isOp(":=", 1)) {
                p += 2
                expression()
                return BadN
            }
            return expression()
        }

        fun lambdef() {
            p++
            var level = 0
            while (true) {
                val t = peek()
                if (t.t == T.END) syntax()
                if (t.t == T.OP) {
                    when (t.s) {
                        "(", "[", "{" -> level++
                        ")", "]", "}" -> level--
                        ":" -> if (level == 0) break
                    }
                }
                p++
            }
            p++
            expression()
        }

        fun disjunction(): Node {
            val a = conjunction()
            if (!isKw("or")) return a
            while (isKw("or")) {
                p++
                conjunction()
            }
            return BadN
        }

        fun conjunction(): Node {
            val a = inversion()
            if (!isKw("and")) return a
            while (isKw("and")) {
                p++
                inversion()
            }
            return BadN
        }

        fun inversion(): Node = nested {
            if (isKw("not")) {
                p++
                inversion()
                BadN
            } else {
                comparison()
            }
        }

        fun comparison(): Node {
            val a = bitOr()
            var compared = false
            while (true) {
                val t = peek()
                when {
                    t.t == T.OP && t.s in setOf("<", ">", "==", ">=", "<=", "!=") -> p++
                    isKw("in") -> p++
                    isKw("not") && isKw("in", 1) -> p += 2
                    isKw("is") -> {
                        p++
                        if (isKw("not")) p++
                    }
                    else -> break
                }
                bitOr()
                compared = true
            }
            return if (compared) BadN else a
        }

        fun binaryChain(ops: Set<String>, next: () -> Node): Node {
            val a = next()
            if (!(peek().t == T.OP && peek().s in ops)) return a
            while (peek().t == T.OP && peek().s in ops) {
                p++
                next()
            }
            return BadN
        }

        fun bitOr(): Node = binaryChain(setOf("|")) { bitXor() }
        fun bitXor(): Node = binaryChain(setOf("^")) { bitAnd() }
        fun bitAnd(): Node = binaryChain(setOf("&")) { shift() }
        fun shift(): Node = binaryChain(setOf("<<", ">>")) { sum() }

        fun sum(): Node {
            var a = term()
            while (isOp("+") || isOp("-")) {
                val op = peek().s
                p++
                a = BinN(op, a, term())
            }
            return a
        }

        fun term(): Node {
            var a = factor()
            while (peek().t == T.OP && peek().s in setOf("*", "/", "//", "%", "@")) {
                val op = peek().s
                p++
                val b = factor()
                a = if (op == "@") BadN else BinN(op, a, b)
            }
            return a
        }

        fun factor(): Node = nested {
            when {
                isOp("+") || isOp("-") -> {
                    val negate = isOp("-")
                    p++
                    UnaryN(factor(), negate)
                }
                isOp("~") -> {
                    p++
                    factor()
                    BadN
                }
                else -> power()
            }
        }

        fun power(): Node {
            val a = awaitPrimary()
            if (isOp("**")) {
                p++
                return BinN("**", a, factor())
            }
            return a
        }

        fun awaitPrimary(): Node {
            if (isKw("await")) {
                p++
                primary()
                return BadN
            }
            return primary()
        }

        fun primary(): Node {
            var a = atom()
            while (true) {
                when {
                    isOp(".") -> {
                        p++
                        if (!isName()) syntax()
                        p++
                        a = BadN
                    }
                    isOp("(") -> {
                        p++
                        val call = arguments()
                        expectOp(")")
                        val f = a
                        a = if (f is NameN && call.ok) CallN(f.id, call.args) else BadN
                    }
                    isOp("[") -> {
                        p++
                        slices()
                        expectOp("]")
                        a = BadN
                    }
                    else -> return a
                }
            }
        }

        fun atom(): Node {
            val t = peek()
            return when (t.t) {
                T.NAME -> {
                    if (!t.kw) {
                        p++
                        NameN(t.s)
                    } else if (t.s == "True" || t.s == "False" || t.s == "None") {
                        p++
                        BadN
                    } else {
                        syntax()
                    }
                }
                T.NUMBER -> {
                    p++
                    t.num!!
                }
                T.STRING -> {
                    val bytes = t.bytes
                    while (peek().t == T.STRING) {
                        if (peek().bytes != bytes) syntax() // can't mix bytes and nonbytes literals
                        p++
                    }
                    BadN
                }
                T.OP -> when (t.s) {
                    "..." -> {
                        p++
                        BadN
                    }
                    "(" -> paren()
                    "[" -> list()
                    "{" -> dict()
                    else -> syntax()
                }
                else -> syntax()
            }
        }

        fun isComprehension() = isKw("for") || isKw("async")

        fun paren(): Node {
            p++
            if (isOp(")")) {
                p++
                return BadN
            }
            if (isKw("yield")) {
                yieldExpr()
                expectOp(")")
                return BadN
            }
            val first = starOrNamed()
            if (isComprehension()) {
                comprehension()
                expectOp(")")
                return BadN
            }
            if (isOp(",")) {
                while (isOp(",")) {
                    p++
                    if (isOp(")")) break
                    starOrNamed()
                }
                expectOp(")")
                return BadN
            }
            expectOp(")")
            if (first === StarN) syntax()
            return first
        }

        fun list(): Node {
            p++
            if (isOp("]")) {
                p++
                return BadN
            }
            starOrNamed()
            if (isComprehension()) {
                comprehension()
            } else {
                while (isOp(",")) {
                    p++
                    if (isOp("]")) break
                    starOrNamed()
                }
            }
            expectOp("]")
            return BadN
        }

        fun dict(): Node {
            p++
            if (isOp("}")) {
                p++
                return BadN
            }
            if (isOp("**")) {
                p++
                bitOr()
                dictRest()
                return BadN
            }
            val first = starOrNamed()
            if (isOp(":") && first !== StarN) {
                p++
                expression()
                if (isComprehension()) {
                    comprehension()
                    expectOp("}")
                } else {
                    dictRest()
                }
                return BadN
            }
            if (isComprehension()) {
                comprehension()
            } else {
                while (isOp(",")) {
                    p++
                    if (isOp("}")) break
                    starOrNamed()
                }
            }
            expectOp("}")
            return BadN
        }

        fun dictRest() {
            while (isOp(",")) {
                p++
                if (isOp("}")) break
                if (isOp("**")) {
                    p++
                    bitOr()
                } else {
                    expression()
                    expectOp(":")
                    expression()
                }
            }
            expectOp("}")
        }

        fun arguments(): Args {
            val args = ArrayList<Node>()
            if (isOp(")")) return Args(true, args)
            var keywords = false
            var seenKeyword = false
            var seenDoubleStar = false
            var generator = false
            var count = 0
            while (true) {
                count++
                when {
                    isOp("*") -> {
                        if (seenDoubleStar) syntax()
                        p++
                        expression()
                        args.add(StarN)
                    }
                    isOp("**") -> {
                        p++
                        expression()
                        keywords = true
                        seenDoubleStar = true
                    }
                    isName() && isOp("=", 1) -> {
                        p += 2
                        expression()
                        keywords = true
                        seenKeyword = true
                    }
                    else -> {
                        val e = namedExpression()
                        if (isComprehension()) {
                            comprehension()
                            generator = true
                            args.add(BadN)
                        } else {
                            args.add(e)
                        }
                        if (seenKeyword || seenDoubleStar) syntax() // positional argument follows keyword argument
                    }
                }
                if (!isOp(",")) break
                p++
                if (isOp(")")) break
            }
            if (generator && count > 1) syntax()
            return Args(!keywords, args)
        }

        fun slices() {
            sliceItem()
            while (isOp(",")) {
                p++
                if (isOp("]")) break
                sliceItem()
            }
        }

        fun sliceItem() {
            if (isOp("*")) {
                p++
                bitOr()
                return
            }
            if (!isOp(":")) namedExpression()
            if (isOp(":")) {
                p++
                if (!isOp(":") && !isOp(",") && !isOp("]")) expression()
                if (isOp(":")) {
                    p++
                    if (!isOp(",") && !isOp("]")) expression()
                }
            }
        }

        fun comprehension() {
            do {
                if (isKw("async")) p++
                expectKw("for")
                if (isOp("*")) p++
                bitOr()
                while (isOp(",")) {
                    p++
                    if (isKw("in")) break
                    if (isOp("*")) p++
                    bitOr()
                }
                expectKw("in")
                disjunction()
                while (isKw("if")) {
                    p++
                    disjunction()
                }
            } while (isComprehension())
        }

        fun yieldExpr() {
            p++
            if (isKw("from")) {
                p++
                expression()
            } else if (!isOp(")")) {
                starOrExpression()
                while (isOp(",")) {
                    p++
                    if (isOp(")")) break
                    starOrExpression()
                }
            }
        }
    }
}

/** Python's view of text, where the desktop's behavior depends on it: str.isspace, isdigit, repr… */
internal object PyText {
    fun isSpace(cp: Int): Boolean = when (cp) {
        in 0x09..0x0d, in 0x1c..0x20, 0x85, 0xa0, 0x1680, in 0x2000..0x200a, 0x2028, 0x2029, 0x202f, 0x205f, 0x3000 -> true
        else -> false
    }

    fun strip(s: String): String {
        var start = 0
        var end = s.length
        while (start < end && isSpace(s.codePointAt(start))) start += Character.charCount(s.codePointAt(start))
        while (end > start && isSpace(s.codePointBefore(end))) end -= Character.charCount(s.codePointBefore(end))
        return s.substring(start, end)
    }

    fun isAlpha(cp: Int) = Character.isLetter(cp)

    /** str.isdigit: decimal digits and digits like superscripts (Numeric_Type Digit). */
    fun isDigit(cp: Int): Boolean {
        val type = Character.getType(cp)
        return type == Character.DECIMAL_DIGIT_NUMBER.toInt() ||
            (type == Character.OTHER_NUMBER.toInt() && Character.getNumericValue(cp) in 0..9)
    }

    fun isAlnum(cp: Int): Boolean {
        if (isAlpha(cp)) return true
        val type = Character.getType(cp)
        return type == Character.DECIMAL_DIGIT_NUMBER.toInt() || type == Character.LETTER_NUMBER.toInt() ||
            type == Character.OTHER_NUMBER.toInt()
    }

    /** Python's \w */
    fun isWord(cp: Int) = cp == '_'.code || isAlnum(cp)

    private fun isPrintable(cp: Int): Boolean = when (Character.getType(cp).toByte()) {
        Character.CONTROL, Character.FORMAT, Character.SURROGATE, Character.PRIVATE_USE, Character.UNASSIGNED,
        Character.LINE_SEPARATOR, Character.PARAGRAPH_SEPARATOR, Character.SPACE_SEPARATOR,
        -> cp == ' '.code
        else -> true
    }

    /** repr() of a str */
    fun repr(s: String): String {
        val quote = if ('\'' in s && '"' !in s) '"' else '\''
        val sb = StringBuilder().append(quote)
        s.codePoints().forEach { cp ->
            when {
                cp == quote.code || cp == '\\'.code -> sb.append('\\').appendCodePoint(cp)
                cp == '\t'.code -> sb.append("\\t")
                cp == '\n'.code -> sb.append("\\n")
                cp == '\r'.code -> sb.append("\\r")
                cp < 0x20 || cp == 0x7f -> sb.append("\\x%02x".format(cp))
                cp < 0x7f || isPrintable(cp) -> sb.appendCodePoint(cp)
                cp <= 0xff -> sb.append("\\x%02x".format(cp))
                cp <= 0xffff -> sb.append("\\u%04x".format(cp))
                else -> sb.append("\\U%08x".format(cp))
            }
        }
        return sb.append(quote).toString()
    }

    /** The shortest decimal that reads back as v (the digits of Python's repr), for finite v. */
    fun shortest(v: Double): BigDecimal {
        if (v == 0.0) return BigDecimal.ZERO
        val exact = BigDecimal(v)
        for (precision in 1..17) {
            val bd = exact.round(MathContext(precision, RoundingMode.HALF_EVEN))
            if (bd.toString().toDouble() == v) return bd
        }
        return exact.round(MathContext(17, RoundingMode.HALF_EVEN))
    }

    /** repr() of a float */
    fun floatRepr(v: Double): String {
        if (v.isNaN()) return "nan"
        if (v.isInfinite()) return if (v > 0) "inf" else "-inf"
        if (v == 0.0) return if (1.0 / v < 0) "-0.0" else "0.0"
        val bd = shortest(abs(v)).stripTrailingZeros()
        val digits = bd.unscaledValue().toString()
        val decpt = digits.length - bd.scale()
        val body = when {
            decpt <= -4 || decpt > 16 -> {
                val mantissa = if (digits.length == 1) digits else digits[0] + "." + digits.substring(1)
                val e = decpt - 1
                mantissa + "e" + (if (e < 0) "-" else "+") + abs(e).toString().padStart(2, '0')
            }
            decpt <= 0 -> "0." + "0".repeat(-decpt) + digits
            decpt >= digits.length -> digits + "0".repeat(decpt - digits.length) + ".0"
            else -> digits.substring(0, decpt) + "." + digits.substring(decpt)
        }
        return (if (v < 0) "-" else "") + body
    }
}
