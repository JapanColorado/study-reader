package dev.russell.studyreader.core

import java.math.BigDecimal
import java.math.MathContext
import java.math.RoundingMode
import java.time.LocalDate
import java.time.LocalDateTime
import java.time.OffsetDateTime
import java.time.ZoneOffset
import java.time.format.DateTimeParseException
import java.util.Locale
import kotlinx.serialization.json.JsonElement
import kotlinx.serialization.json.JsonNull
import kotlinx.serialization.json.JsonObject
import kotlinx.serialization.json.JsonPrimitive
import kotlinx.serialization.json.booleanOrNull
import kotlinx.serialization.json.jsonObject
import kotlinx.serialization.json.Json

// A port of py-fsrs 6.3.2 (the desktop's scheduler): Scheduler.review_card and
// get_card_retrievability, and Card's JSON, which the desktop stores in cards.fsrs. Cards keep
// times as epoch microseconds (UTC), exact as py's datetimes are: a Double of epoch seconds stops
// holding microseconds after 2106, within reach of long intervals. FsrsTest checks the port
// against py-fsrs's own results (android/scripts/fsrs_fixtures.py).

private const val SECOND_US = 1_000_000L
private const val DAY_US = 86_400L * SECOND_US

/** A card's learning state, as py-fsrs's State (a new card is a Learning card at step 0). */
enum class FsrsState(val value: Int) {
    Learning(1), Review(2), Relearning(3);

    companion object {
        fun of(value: Int): FsrsState =
            entries.firstOrNull { it.value == value } ?: throw IllegalArgumentException("Unknown card state: $value")
    }
}

/**
 * py-fsrs's Card. Times are epoch microseconds, exact as py's datetimes are ([due] and
 * [lastReview] give them in epoch seconds); [step] is the (re)learning step (null in Review);
 * [stability] and [difficulty] are null until the first review.
 */
data class FsrsCard(
    val cardId: Long,
    val state: FsrsState,
    val step: Int?,
    val stability: Double?,
    val difficulty: Double?,
    val dueMicros: Long,
    val lastReviewMicros: Long?,
) {
    /** When the card is due, in epoch seconds. */
    val due: Double get() = seconds(dueMicros)

    /** When the card was last reviewed, in epoch seconds. */
    val lastReview: Double? get() = lastReviewMicros?.let(::seconds)

    /** The same text py-fsrs's Card.to_json() writes. */
    fun toJson(): String = buildString {
        append("{\"card_id\": ").append(cardId)
        append(", \"state\": ").append(state.value)
        append(", \"step\": ").append(step?.toString() ?: "null")
        append(", \"stability\": ").append(stability?.let(::pyFloat) ?: "null")
        append(", \"difficulty\": ").append(difficulty?.let(::pyFloat) ?: "null")
        append(", \"due\": \"").append(isoformat(dueMicros)).append('"')
        append(", \"last_review\": ").append(lastReviewMicros?.let { "\"" + isoformat(it) + "\"" } ?: "null")
        append('}')
    }

    companion object {
        /** Reads Card.to_json() text the way py's Card.from_json does (a zero stability or difficulty reads as null). */
        fun fromJson(json: String): FsrsCard {
            val o = Json.parseToJsonElement(json).jsonObject
            val state = FsrsState.of(intOf(o.field("state")))
            val step = o["step"]?.takeUnless { it is JsonNull }?.let(::intOf)
            return FsrsCard(
                cardId = longOf(o.field("card_id")),
                state = state,
                step = if (state == FsrsState.Learning && step == null) 0 else step,
                stability = floatOrNull(o["stability"]),
                difficulty = floatOrNull(o["difficulty"]),
                dueMicros = parseIso(stringOf(o.field("due"))),
                lastReviewMicros = stringOf(o["last_review"]).takeIf { it.isNotEmpty() }?.let(::parseIso),
            )
        }

        /** A new card due at [due] (epoch seconds), as py's Card(due=...): Learning at step 0. py's default id is the creation time in ms. */
        fun new(due: Double, cardId: Long = System.currentTimeMillis()): FsrsCard =
            FsrsCard(cardId, FsrsState.Learning, 0, null, null, micros(due), null)
    }
}

/**
 * py-fsrs's Scheduler. Ratings are 1 (Again), 2 (Hard), 3 (Good), 4 (Easy); steps are in
 * seconds; [random] stands in for Python's random.random(), drawn once per fuzzed interval.
 */
class FsrsScheduler(
    parameters: List<Double> = DEFAULT_PARAMETERS,
    val desiredRetention: Double = 0.9,
    val learningSteps: List<Long> = listOf(60, 600),
    val relearningSteps: List<Long> = listOf(600),
    val maximumInterval: Int = 36500,
    val enableFuzzing: Boolean = true,
    private val random: () -> Double = { kotlin.random.Random.nextDouble() },
) {
    val parameters: List<Double> = parameters.toList()
    private val w = DoubleArray(this.parameters.size) { this.parameters[it] }
    private val decay = -w.getOrElse(20) { 0.0 }
    private val factor = Math.pow(0.9, 1 / decay) - 1

    init {
        validateParameters(this.parameters)
    }

    /** The probability of recalling the card at [now] (epoch seconds; 0 before its first review). */
    fun retrievability(card: FsrsCard, now: Double): Double = retrievabilityMicros(card, micros(now))

    /** [retrievability] at [nowMicros], epoch microseconds. */
    fun retrievabilityMicros(card: FsrsCard, nowMicros: Long): Double {
        val last = card.lastReviewMicros ?: return 0.0
        val stability = card.stability ?: return 0.0
        val elapsedDays = maxOf(0L, Math.floorDiv(nowMicros - last, DAY_US))
        return Math.pow(1 + factor * elapsedDays / stability, decay)
    }

    /** The card after a review rated [rating] at [reviewTime] (epoch seconds). */
    fun reviewCard(card: FsrsCard, rating: Int, reviewTime: Double): FsrsCard = reviewCardMicros(card, rating, micros(reviewTime))

    /** [reviewCard] at [now], epoch microseconds. */
    fun reviewCardMicros(card: FsrsCard, rating: Int, now: Long): FsrsCard {
        require(rating in 1..4) { "Unknown rating: $rating" }
        val daysSince = card.lastReviewMicros?.let { Math.floorDiv(now - it, DAY_US) }
        val sameDay = daysSince != null && daysSince < 1
        var state = card.state
        var step = card.step
        val stability: Double
        val difficulty: Double
        var interval: Long // microseconds

        fun graduate(s: Double): Long {
            state = FsrsState.Review
            step = null
            return nextInterval(s) * DAY_US
        }

        // Learning and relearning steps; a step past the last one (left by a scheduler with more
        // steps) graduates on Hard, Good or Easy.
        fun stepInterval(steps: List<Long>, current: Int, s: Double): Long {
            if (steps.isEmpty() || (current >= steps.size && rating >= 2)) return graduate(s)
            return when (rating) {
                1 -> { step = 0; steps[0] * SECOND_US }
                2 -> when {
                    current == 0 && steps.size == 1 -> steps[0] * SECOND_US * 3 / 2
                    current == 0 -> (steps[0] + steps[1]) * SECOND_US / 2
                    else -> steps[current] * SECOND_US
                }
                3 -> if (current + 1 == steps.size) graduate(s) else { step = current + 1; steps[current + 1] * SECOND_US }
                else -> graduate(s)
            }
        }

        when (card.state) {
            FsrsState.Learning -> {
                val current = requireNotNull(card.step) { "A learning card needs a step" }
                val s = card.stability
                val d = card.difficulty
                if (s == null || d == null) {
                    stability = initialStability(rating)
                    difficulty = initialDifficulty(rating, clamp = true)
                } else {
                    stability = if (sameDay) shortTermStability(s, rating)
                    else nextStability(d, s, retrievabilityMicros(card, now), rating)
                    difficulty = nextDifficulty(d, rating)
                }
                interval = stepInterval(learningSteps, current, stability)
            }
            FsrsState.Review, FsrsState.Relearning -> {
                val s = requireNotNull(card.stability) { "A reviewed card needs a stability" }
                val d = requireNotNull(card.difficulty) { "A reviewed card needs a difficulty" }
                stability = if (sameDay) shortTermStability(s, rating)
                else nextStability(d, s, retrievabilityMicros(card, now), rating)
                difficulty = nextDifficulty(d, rating)
                interval = if (card.state == FsrsState.Relearning) {
                    stepInterval(relearningSteps, requireNotNull(card.step) { "A relearning card needs a step" }, stability)
                } else if (rating == 1 && relearningSteps.isNotEmpty()) {
                    state = FsrsState.Relearning
                    step = 0
                    relearningSteps[0] * SECOND_US
                } else {
                    nextInterval(stability) * DAY_US
                }
            }
        }

        if (enableFuzzing && state == FsrsState.Review) interval = fuzzedInterval(interval)
        return FsrsCard(card.cardId, state, step, stability, difficulty, now + interval, now)
    }

    private fun clampDifficulty(d: Double) = minOf(maxOf(d, MIN_DIFFICULTY), MAX_DIFFICULTY)

    private fun clampStability(s: Double) = maxOf(s, STABILITY_MIN)

    private fun initialStability(rating: Int) = clampStability(w[rating - 1])

    private fun initialDifficulty(rating: Int, clamp: Boolean): Double {
        val d = w[4] - Math.pow(Math.E, w[5] * (rating - 1)) + 1
        return if (clamp) clampDifficulty(d) else d
    }

    /** Whole days, at least 1 and at most [maximumInterval] (py's round: half to even). */
    private fun nextInterval(stability: Double): Long {
        val days = (stability / factor) * (Math.pow(desiredRetention, 1 / decay) - 1)
        return minOf(maxOf(Math.rint(days), 1.0), maximumInterval.toDouble()).toLong()
    }

    private fun shortTermStability(stability: Double, rating: Int): Double {
        var increase = Math.pow(Math.E, w[17] * (rating - 3 + w[18])) * Math.pow(stability, -w[19])
        if (rating >= 2) increase = maxOf(increase, 1.0)
        return clampStability(stability * increase)
    }

    private fun nextDifficulty(difficulty: Double, rating: Int): Double {
        val arg1 = initialDifficulty(4, clamp = false)
        val delta = -(w[6] * (rating - 3))
        val arg2 = difficulty + (10.0 - difficulty) * delta / 9.0
        return clampDifficulty(w[7] * arg1 + (1 - w[7]) * arg2)
    }

    private fun nextStability(difficulty: Double, stability: Double, retrievability: Double, rating: Int): Double {
        val next = if (rating == 1) {
            val longTerm = w[11] * Math.pow(difficulty, -w[12]) * (Math.pow(stability + 1, w[13]) - 1) *
                Math.pow(Math.E, (1 - retrievability) * w[14])
            val shortTerm = stability / Math.pow(Math.E, w[17] * w[18])
            minOf(longTerm, shortTerm)
        } else {
            val hardPenalty = if (rating == 2) w[15] else 1.0
            val easyBonus = if (rating == 4) w[16] else 1.0
            stability * (1 + Math.pow(Math.E, w[8]) * (11 - difficulty) * Math.pow(stability, -w[9]) *
                (Math.pow(Math.E, (1 - retrievability) * w[10]) - 1) * hardPenalty * easyBonus)
        }
        return clampStability(next)
    }

    /** py's _get_fuzzed_interval: a random whole number of days around the interval (from 3 days up). */
    private fun fuzzedInterval(interval: Long): Long {
        val days = Math.floorDiv(interval, DAY_US)
        if (days < 2.5) return interval
        var delta = 1.0
        for ((start, end, f) in FUZZ_RANGES) delta += f * maxOf(minOf(days.toDouble(), end) - start, 0.0)
        var minIvl = Math.rint(days - delta).toLong()
        var maxIvl = Math.rint(days + delta).toLong()
        minIvl = maxOf(2L, minIvl)
        maxIvl = minOf(maxIvl, maximumInterval.toLong())
        minIvl = minOf(minIvl, maxIvl)
        val fuzzed = random() * (maxIvl - minIvl + 1) + minIvl
        return minOf(Math.rint(fuzzed).toLong(), maximumInterval.toLong()) * DAY_US
    }

    companion object {
        /** py-fsrs's default FSRS-6 weights. */
        val DEFAULT_PARAMETERS: List<Double> = listOf(
            0.212, 1.2931, 2.3065, 8.2956, 6.4133, 0.8334, 3.0194, 0.001, 1.8722, 0.1666, 0.796,
            1.4835, 0.0614, 0.2629, 1.6483, 0.6014, 1.8729, 0.5425, 0.0912, 0.0658, 0.1542,
        )
        const val STABILITY_MIN = 0.001
        const val MIN_DIFFICULTY = 1.0
        const val MAX_DIFFICULTY = 10.0
        val LOWER_BOUNDS: List<Double> = listOf(
            STABILITY_MIN, STABILITY_MIN, STABILITY_MIN, STABILITY_MIN, 1.0, 0.001, 0.001, 0.001, 0.0, 0.0, 0.001,
            0.001, 0.001, 0.001, 0.0, 0.0, 1.0, 0.0, 0.0, 0.0, 0.1,
        )
        val UPPER_BOUNDS: List<Double> = listOf(
            100.0, 100.0, 100.0, 100.0, 10.0, 4.0, 4.0, 0.75, 4.5, 0.8, 3.5, 5.0, 0.25, 0.9, 4.0, 1.0, 6.0, 2.0,
            2.0, 0.8, 0.8,
        )
        private val FUZZ_RANGES = listOf(
            Triple(2.5, 7.0, 0.15), Triple(7.0, 20.0, 0.1), Triple(20.0, Double.POSITIVE_INFINITY, 0.05),
        )

        /** Throws IllegalArgumentException unless there are 21 weights, each within py-fsrs's bounds. */
        fun validateParameters(parameters: List<Double>) {
            require(parameters.size == LOWER_BOUNDS.size) { "Expected ${LOWER_BOUNDS.size} parameters, got ${parameters.size}." }
            val errors = parameters.indices.filterNot { LOWER_BOUNDS[it] <= parameters[it] && parameters[it] <= UPPER_BOUNDS[it] }
                .map { "parameters[$it] = ${parameters[it]} is out of bounds: (${LOWER_BOUNDS[it]}, ${UPPER_BOUNDS[it]})" }
            require(errors.isEmpty()) { "One or more parameters are out of bounds:\n" + errors.joinToString("\n") }
        }
    }
}

/**
 * FSRS-6's 21 weights from 21, or from FSRS-5's 19 (extended with w19 = 0 and the old fixed
 * decay w20 = 0.5), as the desktop's srs.fsrs6_parameters. Throws IllegalArgumentException for
 * another count or weights out of bounds.
 */
fun fsrs6Parameters(weights: List<Double>): List<Double> {
    val w = if (weights.size == 19) weights + listOf(0.0, 0.5) else weights.toList()
    require(w.size == 21) { "expected 19 or 21 FSRS weights, got ${weights.size}" }
    FsrsScheduler.validateParameters(w)
    return w
}

// -- time ------------------------------------------------------------------------------------
private fun micros(seconds: Double): Long = Math.round(seconds * 1e6)

private fun seconds(micros: Long): Double = micros / 1e6

/** Python's datetime.isoformat() of a UTC time: microseconds only when there are any. */
private fun isoformat(micros: Long): String {
    val t = LocalDateTime.ofEpochSecond(Math.floorDiv(micros, SECOND_US), 0, ZoneOffset.UTC)
    val us = Math.floorMod(micros, SECOND_US)
    val date = String.format(Locale.ROOT, "%04d-%02d-%02dT%02d:%02d:%02d", t.year, t.monthValue, t.dayOfMonth, t.hour, t.minute, t.second)
    return date + (if (us != 0L) String.format(Locale.ROOT, ".%06d", us) else "") + "+00:00"
}

/** Epoch microseconds of an ISO time as py's datetime.fromisoformat reads it (no offset: UTC). */
private fun parseIso(text: String): Long {
    val s = if (text.length > 10 && text[10] != 'T') text.substring(0, 10) + "T" + text.substring(11) else text
    val instant = try {
        OffsetDateTime.parse(s).toInstant()
    } catch (_: DateTimeParseException) {
        try {
            LocalDateTime.parse(s).toInstant(ZoneOffset.UTC)
        } catch (_: DateTimeParseException) {
            LocalDate.parse(s).atStartOfDay().toInstant(ZoneOffset.UTC)
        }
    }
    return instant.epochSecond * SECOND_US + instant.nano / 1000
}

// -- JSON fields, read as py's int()/float() would -------------------------------------------
private fun JsonObject.field(key: String): JsonElement = this[key] ?: throw IllegalArgumentException("No $key in card JSON")

private fun stringOf(e: JsonElement?): String = (e as? JsonPrimitive)?.takeUnless { it is JsonNull }?.content ?: ""

private fun longOf(e: JsonElement): Long = stringOf(e).trim().let { it.toLongOrNull() ?: it.toDouble().toLong() }

private fun intOf(e: JsonElement): Int = longOf(e).toInt()

private fun floatOrNull(e: JsonElement?): Double? {
    val p = e as? JsonPrimitive ?: return null
    if (p is JsonNull) return null
    if (p.isString) return p.content.takeIf { it.isNotEmpty() }?.trim()?.toDouble()
    p.booleanOrNull?.let { return if (it) 1.0 else null }
    return p.content.toDouble().takeIf { it != 0.0 }
}

// -- Python's float repr ---------------------------------------------------------------------
/** repr() of a Double as Python writes it: the shortest digits that read back the same. */
private fun pyFloat(x: Double): String {
    if (x.isNaN()) return "NaN"
    if (x.isInfinite()) return if (x > 0) "Infinity" else "-Infinity"
    if (x == 0.0) return if (1 / x < 0) "-0.0" else "0.0"
    val sign = if (x < 0) "-" else ""
    val (digits, decpt) = shortestDigits(Math.abs(x))
    val n = digits.length
    val body = if (decpt <= -4 || decpt > 16) {
        val exp = decpt - 1
        val mantissa = if (n == 1) digits else digits[0] + "." + digits.substring(1)
        mantissa + "e" + (if (exp < 0) "-" else "+") + String.format(Locale.ROOT, "%02d", Math.abs(exp))
    } else if (decpt <= 0) {
        "0." + "0".repeat(-decpt) + digits
    } else if (decpt >= n) {
        digits + "0".repeat(decpt - n) + ".0"
    } else {
        digits.substring(0, decpt) + "." + digits.substring(decpt)
    }
    return sign + body
}

/** The shortest decimal digits that read back as [x] (> 0), nearest to it if several, and the
 * point's position: x = 0.DIGITS × 10^decpt. */
private fun shortestDigits(x: Double): Pair<String, Int> {
    val exact = BigDecimal(x)
    for (precision in 1..17) {
        val best = listOf(RoundingMode.HALF_EVEN, RoundingMode.FLOOR, RoundingMode.CEILING)
            .map { exact.round(MathContext(precision, it)) }
            .filter { java.lang.Double.parseDouble(it.toString()) == x }
            .minByOrNull { it.subtract(exact).abs() } ?: continue
        val stripped = best.stripTrailingZeros()
        val digits = stripped.unscaledValue().toString()
        return digits to (digits.length - stripped.scale())
    }
    error("no repr for $x")
}
