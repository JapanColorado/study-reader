package dev.russell.studyreader.core

import kotlinx.serialization.json.Json
import kotlinx.serialization.json.JsonArray
import kotlinx.serialization.json.JsonObject
import kotlinx.serialization.json.boolean
import kotlinx.serialization.json.booleanOrNull
import kotlinx.serialization.json.double
import kotlinx.serialization.json.int
import kotlinx.serialization.json.jsonArray
import kotlinx.serialization.json.jsonObject
import kotlinx.serialization.json.jsonPrimitive
import kotlinx.serialization.json.long
import org.junit.Assert.assertEquals
import org.junit.Assert.assertThrows
import org.junit.Assert.assertTrue
import org.junit.Assert.fail
import org.junit.Test

/** The Kotlin FSRS against py-fsrs's own results (android/scripts/fsrs_fixtures.py). */
class FsrsTest {
    private val cases: JsonObject by lazy {
        val url = javaClass.classLoader.getResource("fsrs_cases.json") ?: error("fsrs_cases.json missing")
        Json.parseToJsonElement(url.readText()).jsonObject
    }

    private fun num(e: kotlinx.serialization.json.JsonElement): Double = e.jsonPrimitive.content.toDouble()

    private fun schedulerOf(d: JsonObject, draws: ArrayDeque<Double>) = FsrsScheduler(
        parameters = d["parameters"]!!.jsonArray.map(::num),
        desiredRetention = d["desired_retention"]!!.jsonPrimitive.double,
        learningSteps = d["learning_steps"]!!.jsonArray.map { it.jsonPrimitive.long },
        relearningSteps = d["relearning_steps"]!!.jsonArray.map { it.jsonPrimitive.long },
        maximumInterval = d["maximum_interval"]!!.jsonPrimitive.int,
        enableFuzzing = d["enable_fuzzing"]!!.jsonPrimitive.boolean,
        random = { draws.removeFirstOrNull() ?: fail("drew more random numbers than py did").let { 0.0 } },
    )

    private fun assertClose(what: String, expected: Double?, actual: Double?, relative: Double) {
        if (expected == null || actual == null) {
            assertEquals(what, expected, actual)
            return
        }
        assertTrue("$what: expected $expected, got $actual", Math.abs(expected - actual) <= relative * Math.abs(expected))
    }


    private fun checkRetrievability(where: String, scheduler: FsrsScheduler, card: FsrsCard, probes: JsonArray) {
        for (p in probes) {
            val (t, r) = p.jsonArray
            val actual = scheduler.retrievabilityMicros(card, t.jsonPrimitive.long)
            assertTrue("$where: R at ${t.jsonPrimitive.long}: expected ${num(r)}, got $actual", Math.abs(num(r) - actual) <= 1e-12)
        }
    }

    @Test
    fun reviewSequencesMatchPyFsrs() {
        var reviews = 0
        var fuzzed = 0
        var exactJson = 0
        val sequences = cases["sequences"]!!.jsonArray
        for (seq in sequences.map { it.jsonObject }) {
            val name = seq["name"]!!.jsonPrimitive.content
            val draws = ArrayDeque<Double>()
            val scheduler = schedulerOf(seq["scheduler"]!!.jsonObject, draws)
            var card = FsrsCard.fromJson(seq["card"]!!.jsonPrimitive.content)
            checkRetrievability("$name, before", scheduler, card, seq["retrievability"]!!.jsonArray)
            for ((i, stepElement) in seq["steps"]!!.jsonArray.withIndex()) {
                val step = stepElement.jsonObject
                val where = "$name, review ${i + 1}"
                val randoms = step["random"]!!.jsonArray.map(::num)
                draws.clear()
                draws.addAll(randoms)
                card = scheduler.reviewCardMicros(card, step["rating"]!!.jsonPrimitive.int, step["time"]!!.jsonPrimitive.long)
                assertTrue("$where: py drew ${randoms.size} random numbers, Kotlin fewer", draws.isEmpty())
                val pyJson = step["card"]!!.jsonPrimitive.content
                val expected = FsrsCard.fromJson(pyJson)
                assertEquals("$where: card_id", expected.cardId, card.cardId)
                assertEquals("$where: state", expected.state, card.state)
                assertEquals("$where: step", expected.step, card.step)
                assertClose("$where: stability", expected.stability, card.stability, 1e-9)
                assertClose("$where: difficulty", expected.difficulty, card.difficulty, 1e-9)
                assertEquals("$where: due", expected.dueMicros, card.dueMicros)
                assertEquals("$where: last_review", expected.lastReviewMicros, card.lastReviewMicros)
                checkRetrievability(where, scheduler, card, step["retrievability"]!!.jsonArray)
                reviews++
                fuzzed += randoms.size
                if (card.toJson() == pyJson) exactJson++
            }
        }
        println("FSRS parity: ${sequences.size} sequences, $reviews reviews ($fuzzed fuzzed), $exactJson card JSONs identical to py's")
        assertTrue(reviews > 3000)
        assertTrue(fuzzed > 500)
    }

    @Test
    fun cardJsonRoundTripsPyText() {
        val texts = cases["roundtrip"]!!.jsonArray.map { it.jsonPrimitive.content } +
            cases["sequences"]!!.jsonArray.flatMap { seq ->
                listOf(seq.jsonObject["card"]!!.jsonPrimitive.content) +
                    seq.jsonObject["steps"]!!.jsonArray.map { it.jsonObject["card"]!!.jsonPrimitive.content }
            }
        for (text in texts) assertEquals(text, FsrsCard.fromJson(text).toJson())
        assertTrue(texts.size > 3000)
    }

    @Test
    fun cardJsonReadsLikePy() {
        for (case in cases["from_json"]!!.jsonArray) {
            val (input, expected) = case.jsonArray.map { it.jsonPrimitive.content }
            assertEquals(input, expected, FsrsCard.fromJson(input).toJson())
        }
    }

    @Test
    fun fsrs6ParametersMatchesDesktop() {
        for (case in cases["fsrs6_parameters"]!!.jsonArray.map { it.jsonObject }) {
            val weights = case["weights"]!!.jsonArray.map(::num)
            if (case["error"]?.jsonPrimitive?.booleanOrNull == true) {
                assertThrows("$weights", IllegalArgumentException::class.java) { fsrs6Parameters(weights) }
            } else {
                assertEquals(case["result"]!!.jsonArray.map(::num), fsrs6Parameters(weights))
            }
        }
    }

    @Test
    fun schedulerChecksItsInputs() {
        assertThrows(IllegalArgumentException::class.java) { FsrsScheduler(FsrsScheduler.DEFAULT_PARAMETERS.take(19)) }
        assertThrows(IllegalArgumentException::class.java) {
            FsrsScheduler(FsrsScheduler.DEFAULT_PARAMETERS.dropLast(1) + 0.05)
        }
        val card = FsrsCard.new(due = 1_800_000_000.25, cardId = 42)
        assertThrows(IllegalArgumentException::class.java) { FsrsScheduler().reviewCard(card, 0, 1_800_000_000.0) }
        assertThrows(IllegalArgumentException::class.java) { FsrsScheduler().reviewCard(card, 5, 1_800_000_000.0) }
    }

    @Test
    fun newCardIsLearningAtStepZero() {
        val card = FsrsCard.new(due = 1_800_000_000.25, cardId = 42)
        assertEquals(
            """{"card_id": 42, "state": 1, "step": 0, "stability": null, "difficulty": null, "due": "2027-01-15T08:00:00.250000+00:00", "last_review": null}""",
            card.toJson(),
        )
        assertEquals(0.0, FsrsScheduler().retrievability(card, 1_800_000_000.0), 0.0)
        val after = FsrsScheduler().reviewCard(card, 3, 1_800_000_000.0)
        assertEquals(FsrsState.Learning, after.state)
        assertEquals(1, after.step)
        assertEquals(1_800_000_600.0, after.due, 0.0)
        assertEquals(1.0, FsrsScheduler().retrievability(after, 1_800_000_000.0), 0.0)
    }
}
