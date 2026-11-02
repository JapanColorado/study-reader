package dev.russell.studyreader.core

import kotlinx.serialization.json.Json
import kotlinx.serialization.json.JsonElement
import kotlinx.serialization.json.JsonObject
import kotlinx.serialization.json.JsonPrimitive
import kotlinx.serialization.json.jsonArray
import kotlinx.serialization.json.jsonObject
import kotlinx.serialization.json.jsonPrimitive
import org.junit.Assert.assertEquals
import org.junit.Assert.assertThrows
import org.junit.Assert.assertTrue
import org.junit.Assert.fail
import org.junit.Test
import kotlin.math.abs
import kotlin.math.max

/**
 * Checks the Kotlin grading against the desktop's own answers in grading_cases.json, written by
 * android/scripts/grading_fixtures.py from quiz.py, calc_latex.py and calculator.py.
 *
 * Where the desktop crashes instead of answering (Python's float() overflowing on a huge exact result,
 * RecursionError on very deep expressions), the port answers with an error instead; see [crashOk].
 */
class GradingTest {
    private val cases: JsonObject by lazy {
        val text = javaClass.classLoader.getResource("grading_cases.json")!!.readText()
        Json.parseToJsonElement(text).jsonObject
    }

    private fun list(key: String) = cases.getValue(key).jsonArray.map { it.jsonObject }

    private fun JsonObject.str(key: String) = this[key]?.jsonPrimitive?.content

    private fun number(e: JsonElement): Double {
        val p = e.jsonPrimitive
        if (!p.isString) return p.content.toDouble()
        return when (p.content) {
            "inf" -> Double.POSITIVE_INFINITY
            "-inf" -> Double.NEGATIVE_INFINITY
            else -> Double.NaN
        }
    }

    private fun close(a: Double, b: Double): Boolean =
        if (!a.isFinite() || !b.isFinite()) a == b else a == b || abs(a - b) <= 1e-9 * max(abs(a), abs(b))

    private fun check(name: String, failures: List<String>, total: Int) {
        if (failures.isNotEmpty()) {
            fail("$name: ${failures.size} of $total cases differ from the desktop:\n" + failures.take(40).joinToString("\n"))
        }
    }

    private fun show(s: String) = PyText.repr(s)

    /** What compute gives for an expression, as the fixture records it. */
    private sealed class Outcome {
        data class Value(val v: Double) : Outcome()
        data class Error(val message: String) : Outcome()
    }

    private fun compute(expr: String): Outcome = try {
        Outcome.Value(Calc.compute(expr))
    } catch (e: CalcError) {
        Outcome.Error(e.message ?: "")
    }

    /** Compares compute against a fixture case with "value" | "error" | "crash". */
    private fun computeMismatch(case: JsonObject, expr: String): String? {
        val got = compute(expr)
        val crash = case.str("crash")
        when {
            crash == "OverflowError" -> // float() of a huge exact result: the desktop never catches it
                if (got != Outcome.Error("Result is too large")) return "${show(expr)}: Python OverflowError, got $got"
            crash != null -> {} // RecursionError: Python gives up on deep nesting; any answer is fine
            case.containsKey("error") ->
                if (got != Outcome.Error(case.str("error")!!)) return "${show(expr)}: expected error ${case.str("error")}, got $got"
            else -> {
                val want = number(case.getValue("value"))
                if (got !is Outcome.Value || !close(got.v, want)) return "${show(expr)}: expected $want, got $got"
            }
        }
        return null
    }

    @Test
    fun latexMatchesTheDesktop() {
        val all = list("latex")
        val failures = ArrayList<String>()
        for (case in all) {
            val latex = case.str("latex")!!
            val expr = try {
                CalcLatex.latexToExpr(latex)
            } catch (e: LatexError) {
                if (case.str("latex_error") != e.message) {
                    failures += "${show(latex)}: expected ${case.str("latex_error") ?: case.str("expr")}, got LatexError ${e.message}"
                }
                continue
            }
            if (case.str("expr") != expr) {
                failures += "${show(latex)}: expected ${case.str("expr") ?: "LatexError " + case.str("latex_error")}, got ${show(expr)}"
                continue
            }
            computeMismatch(case, expr)?.let { failures += "${show(latex)} -> $it" }
        }
        check("latex", failures, all.size)
        assertTrue(all.size > 1500)
    }

    @Test
    fun computeMatchesTheDesktop() {
        val all = list("compute")
        val failures = all.mapNotNull { computeMismatch(it, it.str("expr")!!) }
        check("compute", failures, all.size)
        assertTrue(all.size > 1000)
    }

    @Test
    fun exprToLatexMatchesTheDesktop() {
        val all = list("expr_to_latex")
        val failures = all.mapNotNull {
            val got = CalcLatex.exprToLatex(it.str("expr")!!)
            if (got != it.str("latex")) "${show(it.str("expr")!!)}: expected ${show(it.str("latex")!!)}, got ${show(got)}" else null
        }
        check("expr_to_latex", failures, all.size)
    }

    /** The outcome of a grading call as the fixture records it: a result, or an error kind and message. */
    private fun outcome(f: () -> Any): Pair<String, String> = try {
        val r = f()
        "result" to (if (r is Double) r.toString() else r.toString())
    } catch (e: AnswerError) {
        "AnswerError" to (e.message ?: "")
    } catch (e: IllegalArgumentException) {
        "IllegalArgumentException" to (e.message ?: "")
    } catch (e: ArithmeticException) {
        "ArithmeticException" to (e.message ?: "")
    }

    private fun gradingMismatch(case: JsonObject, label: String, f: () -> Any): String? {
        val (kind, message) = outcome(f)
        val error = case.str("error")
        return when (error) {
            null -> {
                val want = case.getValue("result")
                val ok = kind == "result" && if (want is JsonPrimitive && !want.isString || want.jsonPrimitive.content in setOf("inf", "-inf")) {
                    close(message.toDouble(), number(want))
                } else {
                    message == want.jsonPrimitive.content
                }
                if (ok) null else "$label: expected ${want.jsonPrimitive.content}, got $kind $message"
            }
            "AnswerError" -> if (kind == "AnswerError" && message == case.str("message")) null else
                "$label: expected AnswerError ${case.str("message")}, got $kind $message"
            "ValueError", "KeyError", "TypeError" ->
                if (kind == "IllegalArgumentException" && message == case.str("message")) null else
                    "$label: expected $error ${case.str("message")}, got $kind $message"
            // Python's float() overflowing on a huge exact value, which the desktop doesn't catch
            "OverflowError" -> if (kind != "result") null else "$label: expected an error (Python OverflowError), got $message"
            else -> "$label: unexpected Python error $error"
        }
    }

    @Test
    fun gradeMatchesTheDesktop() {
        val all = list("grade")
        val failures = all.mapNotNull { case ->
            val q = case.getValue("question").jsonObject
            val r = case.getValue("response").jsonObject
            gradingMismatch(case, "grade($q, $r)") { Grading.grade(q, r) }
        }
        check("grade", failures, all.size)
        assertTrue(all.size > 1000)
    }

    @Test
    fun expectedValueMatchesTheDesktop() {
        val all = list("expected_value")
        val failures = all.mapNotNull { case ->
            val a = case.getValue("answer")
            gradingMismatch(case, "expected_value($a)") { Grading.expectedValue(a) }
        }
        check("expected_value", failures, all.size)
    }

    // -- a few readable examples of the API --------------------------------------------------------

    private fun json(s: String) = Json.parseToJsonElement(s).jsonObject

    @Test
    fun gradesEachKind() {
        val numeric = json("""{"type": "numeric", "answer": "41", "unit": "s"}""")
        assertEquals("correct", Grading.grade(numeric, json("""{"latex": "\\frac{82}{2}"}""")))
        assertEquals("correct", Grading.grade(numeric, json("""{"latex": "40.7"}"""))) // within 1%
        assertEquals("incorrect", Grading.grade(numeric, json("""{"latex": "40"}""")))
        assertEquals("correct", Grading.grade(json("""{"type": "numeric", "answer": 41, "tolerance": 1.5}"""), json("""{"latex": "40"}""")))
        assertEquals("correct", Grading.grade(json("""{"type": "numeric", "answer": "sqrt(2)/2"}"""), json("""{"latex": "0.7071"}""")))
        assertEquals("correct", Grading.grade(json("""{"type": "choice", "choices": ["a", "b"], "answer": 1}"""), json("""{"choice": 1}""")))
        assertEquals("incorrect", Grading.grade(json("""{"type": "truefalse", "answer": false}"""), json("""{"value": true}""")))
        assertEquals("partial", Grading.grade(json("""{"type": "flashcard", "answer": "Back"}"""), json("""{"rating": 2}""")))
        assertEquals("Enter a number", assertThrows(AnswerError::class.java) { Grading.grade(numeric, json("""{"latex": " "}""")) }.message)
        assertEquals("Fill in the empty box first", assertThrows(AnswerError::class.java) {
            Grading.grade(numeric, json("""{"latex": "\\frac{1}{\\placeholder{}}"}"""))
        }.message)
        assertThrows(IllegalArgumentException::class.java) { Grading.grade(json("""{"type": "free"}"""), json("""{"text": "x"}""")) }
    }

    @Test
    fun readsMathFieldLatex() {
        assertEquals("(1)/(2)*sqrt(x)+sin(pi)", CalcLatex.latexToExpr("\\frac{1}{2}\\sqrt{x}+\\sin\\left(\\pi\\right)"))
        assertEquals("3*(x+1)", CalcLatex.latexToExpr("3\\left(x+1\\right)"))
        assertEquals(0.0015, Calc.compute(CalcLatex.latexToExpr("1.5\\times10^{-3}")), 0.0)
        assertEquals(0.3, Calc.compute("0.1 + 0.2"), 0.0) // exact, as on the desktop
        assertEquals("Division by zero", assertThrows(CalcError::class.java) { Calc.compute("1/0") }.message)
        assertEquals("Unknown name 'x'", assertThrows(CalcError::class.java) { Calc.compute("x") }.message)
    }
}
