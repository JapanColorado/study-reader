package dev.russell.studyreader.core

import kotlinx.serialization.json.JsonArray
import kotlinx.serialization.json.JsonElement
import kotlinx.serialization.json.JsonNull
import kotlinx.serialization.json.JsonObject
import kotlinx.serialization.json.JsonPrimitive
import java.math.BigDecimal
import java.math.BigInteger
import kotlin.math.abs

/** An answer that can't be graded as entered (e.g. an unreadable number); shown under the field, nothing is saved. */
class AnswerError(message: String) : Exception(message)

/**
 * Port of the desktop's local quiz grading (quiz.grade and quiz.expected_value). Numeric answers are typed
 * as LaTeX in a MathLive field and read through [CalcLatex] and [Calc], as on the desktop.
 */
object Grading {
    /** Numeric answers within this fraction of the expected value count, unless the question gives a tolerance. */
    const val RELATIVE_TOLERANCE = 0.01

    /** How well a flashcard was recalled (Again / Hard / Good / Easy) as a verdict. */
    val RATING_VERDICTS = mapOf(1 to "incorrect", 2 to "partial", 3 to "correct", 4 to "correct")

    /**
     * Verdict ("correct" | "incorrect" | "partial") for a locally graded question: choice, truefalse,
     * flashcard (by rating) and numeric. question/response are the JSON the cards store / review.html sends:
     * {"choice": i} | {"value": bool} | {"latex": s} | {"rating": n}.
     *
     * Throws [AnswerError] for input that can't be graded as entered, and IllegalArgumentException where the
     * desktop raises ValueError (written answers, which Claude grades; unreadable expected answers), KeyError
     * or TypeError (malformed questions). An expected answer beyond the double range throws
     * ArithmeticException (the desktop's uncaught OverflowError); a typed answer beyond it is an AnswerError.
     */
    fun grade(question: JsonObject, response: JsonObject): String {
        val kind = question.required("type").let { if (it is JsonPrimitive && it.isString) it.content else null }
        when (kind) {
            "choice" -> {
                val given = response["choice"]
                return if (pyEquals(given, question.required("answer"))) "correct" else "incorrect"
            }
            "truefalse" -> {
                val given = PyValue.of(response["value"])
                val answer = PyValue.of(question.required("answer"))
                // Python's `is`: the same bool (or both None)
                val same = (given is PyValue.Bool && answer is PyValue.Bool && given.b == answer.b) ||
                    (given is PyValue.None && answer is PyValue.None)
                return if (same) "correct" else "incorrect"
            }
            "flashcard" -> {
                val rating = when (val r = PyValue.of(response["rating"])) {
                    is PyValue.Bool -> if (r.b) 1 else 0
                    is PyValue.Int -> if (r.n.bitLength() < 31) r.n.toInt() else 0
                    is PyValue.Float -> if (r.v == Math.floor(r.v) && abs(r.v) < 1e9) r.v.toInt() else 0
                    is PyValue.List -> throw IllegalArgumentException("unhashable type: 'list'")
                    is PyValue.Dict -> throw IllegalArgumentException("unhashable type: 'dict'")
                    else -> 0
                }
                return RATING_VERDICTS[rating] ?: throw AnswerError("Rate how well you knew it")
            }
            "numeric" -> {
                val latex = PyText.strip(response["latex"]?.let { PyValue.str(it) } ?: "")
                if (latex.isEmpty()) throw AnswerError("Enter a number")
                val given = try {
                    // (where the desktop crashes on an answer beyond the double range, this says so)
                    Calc.compute(CalcLatex.latexToExpr(latex))
                } catch (e: CalcError) {
                    throw AnswerError(e.message ?: "")
                } catch (e: LatexError) {
                    throw AnswerError(e.message ?: "")
                }
                val expected = expectedValue(question.required("answer"))
                val tol = when (val t = PyValue.of(question["tolerance"])) {
                    is PyValue.None -> RELATIVE_TOLERANCE * abs(expected)
                    is PyValue.Bool -> if (t.b) 1.0 else 0.0
                    is PyValue.Int -> intToFloat(t.n)
                    is PyValue.Float -> t.v
                    is PyValue.Str -> throw IllegalArgumentException("can only concatenate str (not \"float\") to str")
                    is PyValue.List -> throw IllegalArgumentException("can only concatenate list (not \"float\") to list")
                    is PyValue.Dict -> throw IllegalArgumentException("unsupported operand type(s) for +: 'dict' and 'float'")
                }
                return if (isClose(given, expected, tol + 1e-12)) "correct" else "incorrect"
            }
        }
        throw IllegalArgumentException("written answers are graded by Claude")
    }

    /**
     * The value of a numeric question's answer: a JSON number, or a string holding a plain expression
     * ("3/8", "sqrt(2)/2", "2e-3") or LaTeX. Throws IllegalArgumentException if it can't be read, and
     * ArithmeticException for a value beyond the double range (where the desktop raises OverflowError).
     */
    fun expectedValue(answer: JsonElement): Double {
        when (val a = PyValue.of(answer)) {
            is PyValue.Bool -> throw IllegalArgumentException("a numeric answer must be a number")
            is PyValue.Int -> return intToFloat(a.n)
            is PyValue.Float -> return a.v
            else -> {}
        }
        val text = PyText.strip(PyValue.str(answer))
        return try {
            Calc.computeFloat(text)
        } catch (e: CalcError) {
            try {
                Calc.computeFloat(CalcLatex.latexToExpr(text))
            } catch (e: CalcError) {
                throw IllegalArgumentException("can't read the numeric answer ${PyText.repr(text)}")
            } catch (e: LatexError) {
                throw IllegalArgumentException("can't read the numeric answer ${PyText.repr(text)}")
            }
        }
    }

    private fun JsonObject.required(key: String): JsonElement =
        this[key] ?: throw IllegalArgumentException("'$key'") // Python's KeyError

    private fun intToFloat(n: BigInteger): Double = try {
        Calc.ratToDouble(n, BigInteger.ONE)
    } catch (e: Calc.PyErr) {
        throw ArithmeticException("int too large to convert to float")
    }

    /** math.isclose(a, b, rel_tol=0, abs_tol=absTol) */
    private fun isClose(a: Double, b: Double, absTol: Double): Boolean {
        if (absTol < 0.0) throw IllegalArgumentException("tolerances must be non-negative")
        if (a == b) return true
        if (a.isInfinite() || b.isInfinite()) return false
        val diff = abs(b - a)
        return diff <= 0.0 || diff <= absTol
    }

    /** Python's == between JSON values, as json.loads gives them (True == 1 == 1.0). */
    private fun pyEquals(x: JsonElement?, y: JsonElement?): Boolean {
        val a = PyValue.of(x)
        val b = PyValue.of(y)
        val na = a.number()
        val nb = b.number()
        if (na != null || nb != null) {
            if (na == null || nb == null) return false
            if (na is Double && na.isNaN() || nb is Double && nb.isNaN()) return false
            if (na is Double && na.isInfinite() || nb is Double && nb.isInfinite()) return na.toDouble() == nb.toDouble()
            return toExact(na).compareTo(toExact(nb)) == 0
        }
        return when (a) {
            is PyValue.None -> b is PyValue.None
            is PyValue.Str -> b is PyValue.Str && a.s == b.s
            is PyValue.List -> b is PyValue.List && a.items.size == b.items.size &&
                a.items.indices.all { pyEquals(a.items[it], b.items[it]) }
            is PyValue.Dict -> b is PyValue.Dict && a.map.keys == b.map.keys && a.map.all { (k, v) -> pyEquals(v, b.map[k]) }
            else -> false
        }
    }

    private fun toExact(n: Number): BigDecimal = when (n) {
        is BigInteger -> BigDecimal(n)
        else -> BigDecimal(n.toDouble())
    }

    /** A JSON value as Python's json module would load it. */
    private sealed class PyValue {
        object None : PyValue()
        class Bool(val b: Boolean) : PyValue()
        class Int(val n: BigInteger) : PyValue()
        class Float(val v: Double) : PyValue()
        class Str(val s: String) : PyValue()
        class List(val items: kotlin.collections.List<JsonElement>) : PyValue()
        class Dict(val map: Map<String, JsonElement>) : PyValue()

        /** Its numeric value for comparisons (bools are ints in Python), or null. */
        fun number(): Number? = when (this) {
            is Bool -> if (b) BigInteger.ONE else BigInteger.ZERO
            is Int -> n
            is Float -> v
            else -> null
        }

        companion object {
            fun of(e: JsonElement?): PyValue = when (e) {
                null, is JsonNull -> None
                is JsonArray -> List(e)
                is JsonObject -> Dict(e)
                is JsonPrimitive -> when {
                    e.isString -> Str(e.content)
                    e.content == "true" -> Bool(true)
                    e.content == "false" -> Bool(false)
                    else -> e.content.toBigIntegerOrNull()?.let { Int(it) } ?: Float(e.content.toDouble())
                }
            }

            /** str() of a JSON value */
            fun str(e: JsonElement): String = when (val v = of(e)) {
                is Str -> v.s
                else -> repr(e)
            }

            /** repr() of a JSON value */
            fun repr(e: JsonElement): String = when (val v = of(e)) {
                is None -> "None"
                is Bool -> if (v.b) "True" else "False"
                is Int -> v.n.toString()
                is Float -> PyText.floatRepr(v.v)
                is Str -> PyText.repr(v.s)
                is List -> v.items.joinToString(", ", "[", "]") { repr(it) }
                is Dict -> v.map.entries.joinToString(", ", "{", "}") { (k, x) -> PyText.repr(k) + ": " + repr(x) }
            }
        }
    }
}
