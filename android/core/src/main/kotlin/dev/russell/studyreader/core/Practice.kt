package dev.russell.studyreader.core

import kotlinx.serialization.json.Json
import kotlinx.serialization.json.JsonElement
import kotlinx.serialization.json.JsonObject
import kotlinx.serialization.json.JsonPrimitive
import kotlinx.serialization.json.jsonObject
import java.time.Instant
import java.time.LocalDate
import java.time.ZoneId
import java.time.ZonedDateTime
import kotlin.math.max
import kotlin.math.min
import kotlin.math.roundToLong

/**
 * The practice queue over the phone's copy of the questions, as the desktop's practice.py runs it: what
 * is due, new questions within the daily allowance, sections, and scheduling an attempt with FSRS.
 * Times are epoch seconds. Questions the phone buried (the `buried` table) stay out until it empties.
 */
class Practice(val db: Sql, val zone: ZoneId = ZoneId.systemDefault(), val fuzz: Boolean = true) {
    companion object {
        const val NEW_PER_DAY_SETTING = "practice_new_per_day"
        const val DEFAULT_NEW_PER_DAY = 20
        const val RETENTION_SETTING = "practice_desired_retention"
        const val PARAMETERS_SETTING = "practice_parameters"
        const val DEFAULT_RETENTION = 0.9
        const val DAY_STARTS_AT_HOUR = 4
        const val LEARN_AHEAD = 20 * 60.0
        val VERDICT_RATINGS = mapOf("correct" to 3, "partial" to 2, "incorrect" to 1)

        /** Question kinds by their names before the rename (and "free", older still), which older
         *  snapshots and variants may hold. */
        val KIND_ALIASES = mapOf(
            "flashcard" to "self_check", "reversed" to "both_ways", "cloze" to "fill_in", "occlusion" to "image_labels",
            "numeric" to "calculation", "written" to "short_answer", "free" to "short_answer",
        )

        /** A kind in today's terms. */
        fun kind(name: String?): String? = KIND_ALIASES[name] ?: name

        // Due questions; args (nextDay(now), now).
        fun dueSql(alias: String = "") =
            "${alias}suspended=0 AND ((${alias}stage=2 AND ${alias}due<?) OR (${alias}stage IN (1,3) AND ${alias}due<=?))"
        val DUE = dueSql()
        const val NEW = "suspended=0 AND stage=0"

        /** 'in 5m'-style intervals, as practice.format_interval. */
        fun formatInterval(seconds: Double): String {
            val s = max(seconds, 0.0)
            for ((unit, size) in listOf("y" to 365 * 86400.0, "mo" to 30 * 86400.0, "d" to 86400.0, "h" to 3600.0, "m" to 60.0)) {
                if (s >= size || unit == "m") {
                    val n = s / size
                    if (unit == "y") return String.format(java.util.Locale.ROOT, "%.1f", n).trimEnd('0').trimEnd('.') + "y"
                    return "${max(pyRound(n), 1)}$unit"
                }
            }
            return ""
        }

        /** Python's round(): halves to even. */
        fun pyRound(x: Double): Long {
            val r = Math.rint(x)
            return r.roundToLong()
        }
    }

    // -- settings ---------------------------------------------------------------------------
    fun setting(key: String, default: String = ""): String =
        db.one("SELECT value FROM settings WHERE key=?", key)?.str("value") ?: default

    fun setSetting(key: String, value: String) =
        db.exec("INSERT INTO settings (key, value) VALUES (?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value", key, value)

    fun newPerDay(): Int = setting(NEW_PER_DAY_SETTING, "$DEFAULT_NEW_PER_DAY").toIntOrNull()?.let { max(it, 0) } ?: DEFAULT_NEW_PER_DAY

    fun desiredRetention(): Double {
        val r = setting(RETENTION_SETTING, "$DEFAULT_RETENTION").toDoubleOrNull() ?: return DEFAULT_RETENTION
        return min(max(r, 0.7), 0.99)
    }

    fun parameters(): List<Double>? {
        val raw = setting(PARAMETERS_SETTING)
        if (raw.isBlank()) return null
        return try {
            fsrs6Parameters(Json.parseToJsonElement(raw).let { e ->
                (e as kotlinx.serialization.json.JsonArray).map { (it as kotlinx.serialization.json.JsonPrimitive).content.toDouble() }
            })
        } catch (e: Exception) { null }
    }

    private var cached: Pair<Pair<List<Double>?, Double>, FsrsScheduler>? = null
    fun scheduler(): FsrsScheduler {
        val key = parameters() to desiredRetention()
        cached?.let { if (it.first == key) return it.second }
        val s = if (key.first != null) FsrsScheduler(parameters = key.first!!, desiredRetention = key.second, enableFuzzing = fuzz)
                else FsrsScheduler(desiredRetention = key.second, enableFuzzing = fuzz)
        cached = key to s
        return s
    }

    // -- days -------------------------------------------------------------------------------
    fun dayStart(now: Double): Double {
        val local = ZonedDateTime.ofInstant(instant(now), zone)
        var start = local.withHour(DAY_STARTS_AT_HOUR).withMinute(0).withSecond(0).withNano(0)
        if (local.isBefore(start)) start = start.minusDays(1)
        return start.toEpochSecond().toDouble()
    }

    fun nextDay(now: Double): Double =
        ZonedDateTime.ofInstant(instant(dayStart(now)), zone).plusDays(1).toEpochSecond().toDouble()

    /** The practice day a moment falls on (days roll over at 4 am). */
    fun dayOf(t: Double): LocalDate = ZonedDateTime.ofInstant(instant(dayStart(t)), zone).toLocalDate()

    private fun instant(t: Double) = Instant.ofEpochMilli((t * 1000).roundToLong())

    // -- scope ------------------------------------------------------------------------------
    private fun like(s: String) = s.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")

    /** One of several things to practice together (practice_view.py's PracticePanel.picks): a deck
     *  (with its subdecks), a book, or a section (tag) of a book. */
    data class Pick(val deck: Long? = null, val book: Long? = null, val section: String? = null)

    /** SQL condition (ending in AND) and its arguments for a book, deck (with its subdecks) or tag, and
     *  within any of picks (none: every question). */
    fun scope(bookId: Long?, deckId: Long?, tag: String? = null, picks: List<Pick> = emptyList()): Pair<String, List<Any?>> {
        val sql = StringBuilder("id NOT IN (SELECT question_id FROM buried) AND ")
        val args = mutableListOf<Any?>()
        if (picks.isNotEmpty()) {
            sql.append(picks.joinToString(" OR ", "(", ") AND ") { p ->
                val (s, a) = if (p.deck != null) narrow(null, p.deck) else narrow(p.book, null, p.section)
                args.addAll(a)
                "(${s.removeSuffix(" AND ")})"
            })
        }
        val (s, a) = narrow(bookId, deckId, tag)
        sql.append(s); args.addAll(a)
        return sql.toString() to args
    }

    private fun narrow(bookId: Long?, deckId: Long?, tag: String? = null): Pair<String, List<Any?>> {
        val sql = StringBuilder()
        val args = mutableListOf<Any?>()
        if (bookId != null) { sql.append("book_id=? AND "); args += bookId }
        if (deckId != null) {
            sql.append("deck_id IN (SELECT d2.id FROM decks d1 JOIN decks d2 ON d2.id=d1.id OR " +
                "substr(d2.name, 1, length(d1.name) + 2)=d1.name||'::' WHERE d1.id=?) AND ")
            args += deckId
        }
        if (tag != null) {
            sql.append("group_id IN (SELECT id FROM question_groups WHERE tags LIKE ? ESCAPE '\\' OR tags LIKE ? ESCAPE '\\') AND ")
            args += "% ${like(tag)} %"; args += "% ${like(tag)}::%"
        }
        return sql.toString() to args
    }

    fun questions(bookId: Long? = null, where: String = "1", args: List<Any?> = emptyList(), order: String = "id",
              limit: Int = -1, deckId: Long? = null, tag: String? = null, picks: List<Pick> = emptyList()): List<Row> {
        val (s, a) = scope(bookId, deckId, tag, picks)
        return db.query("SELECT * FROM questions WHERE $s($where) ORDER BY $order LIMIT ?", *(a + args + limit).toTypedArray())
    }

    fun countQuestions(bookId: Long? = null, where: String = "1", args: List<Any?> = emptyList(),
                   deckId: Long? = null, tag: String? = null, picks: List<Pick> = emptyList()): Int {
        val (s, a) = scope(bookId, deckId, tag, picks)
        return db.long("SELECT COUNT(*) FROM questions WHERE $s($where)", *(a + args).toTypedArray()).toInt()
    }

    fun question(id: Long): Row? = db.one("SELECT * FROM questions WHERE id=?", id)

    /** Lets buried questions back in once their day is over. */
    fun unbury(now: Double) = db.exec("DELETE FROM buried WHERE until<=?", now)

    fun newQuestionsAttemptedSince(since: Double): Int =
        // 'review' is what attempts made in practice were called before the rename.
        db.long("SELECT COUNT(*) FROM attempts WHERE source IN ('practice', 'review') AND was_new=1 AND attempted_at>=?", since).toInt()

    fun newLeftToday(now: Double): Int = max(newPerDay() - newQuestionsAttemptedSince(dayStart(now)), 0)

    // -- counts -----------------------------------------------------------------------------
    fun counts(bookId: Long?, now: Double, deck: Long? = null, tag: String? = null,
               picks: List<Pick> = emptyList()): Map<String, Int> {
        val new = countQuestions(bookId, NEW, deckId = deck, tag = tag, picks = picks)
        return mapOf(
            "due" to countQuestions(bookId, DUE, listOf(nextDay(now), now), deck, tag, picks),
            "new" to min(new, newLeftToday(now)),
            "new_total" to new,
            "total" to countQuestions(bookId, deckId = deck, tag = tag, picks = picks),
            "suspended" to countQuestions(bookId, "suspended=1", deckId = deck, tag = tag, picks = picks),
        )
    }

    /** How many questions are left to practice today (due, and new ones today's allowance allows),
     *  leaving out exclude (questions skipped this session). */
    fun leftToPractice(bookId: Long?, now: Double, deck: Long? = null, tag: String? = null,
                     exclude: List<Long> = emptyList(), picks: List<Pick> = emptyList()): Int {
        val (dueWhere, dueArgs) = notIn(DUE, listOf(nextDay(now), now), exclude)
        val (newWhere, newArgs) = notIn(NEW, emptyList(), exclude)
        return countQuestions(bookId, dueWhere, dueArgs, deck, tag, picks) +
            min(countQuestions(bookId, newWhere, newArgs, deck, tag, picks), newLeftToday(now))
    }

    /** where, also leaving out the questions with the ids in exclude. */
    private fun notIn(where: String, args: List<Any?>, exclude: List<Long>): Pair<String, List<Any?>> =
        if (exclude.isEmpty()) where to args
        else "($where) AND id NOT IN (${exclude.joinToString(",") { "?" }})" to args + exclude

    fun decks(): List<Row> = db.query("SELECT * FROM decks ORDER BY name COLLATE NOCASE")

    /** For every deck, its questions and those of the decks under it: {due, new (uncapped), total}. */
    fun deckCounts(now: Double): Map<Long, MutableMap<String, Int>> {
        val own = db.query(
            "SELECT deck_id, SUM($DUE) AS due, SUM($NEW) AS new, COUNT(*) AS total FROM questions " +
                "WHERE id NOT IN (SELECT question_id FROM buried) GROUP BY deck_id", nextDay(now), now,
        ).associateBy { it.long("deck_id") }
        val decks = decks()
        val out = decks.associate { it.long("id") to mutableMapOf("due" to 0, "new" to 0, "total" to 0) }
        val byName = decks.associate { it.str("name") to it.long("id") }
        for (d in decks) {
            val row = own[d.long("id")] ?: continue
            val parts = d.str("name").split("::")
            for (i in 1..parts.size) {
                val target = byName[parts.take(i).joinToString("::")] ?: continue
                for (k in listOf("due", "new", "total")) out[target]!![k] = out[target]!![k]!! + ((row[k] as? Number)?.toInt() ?: 0)
            }
        }
        return out
    }

    /** How many '::' a tag has. */
    private fun levels(tag: String) = tag.split("::").size - 1

    data class Section(val tag: String, val name: String, val parent: String?, var due: Int = 0, var new: Int = 0,
                       var total: Int = 0, var page: Long? = null)

    /** Port of practice.sections: per book, the section tags of its questions as a tree in reading order. */
    fun sections(bookId: Long? = null, now: Double): Map<Long, List<Section>> {
        val roots = db.query("SELECT id, root FROM books").associate { it.long("id") to it.str("root") }
        val scopeSql = if (bookId != null) "AND q.book_id=?" else ""
        val args = mutableListOf<Any?>(nextDay(now), now)
        if (bookId != null) args += bookId
        val rows = db.query(
            "SELECT q.book_id, q.page, g.tags, ${dueSql("q.")} AS due, q.suspended=0 AND q.stage=0 AS new " +
                "FROM questions q JOIN question_groups g ON g.id=q.group_id WHERE q.book_id IS NOT NULL AND g.tags!='' " +
                "AND q.id NOT IN (SELECT question_id FROM buried) $scopeSql", *args.toTypedArray(),
        )
        val nodes = LinkedHashMap<Long, LinkedHashMap<String, Section>>()
        for (r in rows) {
            val book = r.long("book_id")
            val root = roots[book] ?: continue
            val prefix = root.lowercase() + "::"
            val tags = LinkedHashSet<String>()
            for (t in r.str("tags").split(" ").filter { it.isNotEmpty() }) {
                if (t.lowercase().startsWith(prefix)) {
                    val parts = t.split("::")
                    for (i in 2..parts.size) tags += parts.take(i).joinToString("::")
                }
            }
            val bookNodes = nodes.getOrPut(book) { LinkedHashMap() }
            val page = r.longOrNull("page")
            for (t in tags) {
                val n = bookNodes.getOrPut(t) {
                    Section(t, t.substringAfterLast("::").replace("_", " "),
                        if (levels(t) > 1) t.substringBeforeLast("::") else null, page = page)
                }
                n.due += (r["due"] as? Number)?.toInt() ?: 0
                n.new += (r["new"] as? Number)?.toInt() ?: 0
                n.total += 1
                if (page != null && (n.page == null || page < n.page!!)) n.page = page
            }
        }
        val out = LinkedHashMap<Long, List<Section>>()
        for ((b, ns) in nodes) {
            val starts = db.query("SELECT tag, page FROM section_starts WHERE book_id=?", b)
                .associate { it.str("tag") to it.long("page") }
            for (n in ns.values) starts[n.tag.lowercase()]?.let { n.page = it }
            out[b] = ns.values.sortedWith(compareBy<Section>({ it.page == null }, { it.page ?: 0 },
                { levels(it.tag) }, { it.tag }))
        }
        return out
    }

    // -- the queue --------------------------------------------------------------------------
    /** skip: questions not to show again yet (the one just answered), unless nothing else is left;
     *  exclude: questions never to show (skipped this session). */
    fun nextQuestion(bookId: Long?, now: Double, skip: List<Long> = emptyList(), deck: Long? = null, tag: String? = null,
                 exclude: List<Long> = emptyList(), picks: List<Pick> = emptyList()): Row? {
        val tiers = mutableListOf(Triple(DUE, listOf<Any?>(nextDay(now), now), "due, id"))
        if (newLeftToday(now) > 0) tiers += Triple(NEW, emptyList(), "created_at, id")
        tiers += Triple("suspended=0 AND stage IN (1,3) AND due<=?", listOf(now + LEARN_AHEAD), "due, id")
        var fallback: Row? = null
        for ((tierWhere, tierArgs, order) in tiers) {
            val (where, args) = notIn(tierWhere, tierArgs, exclude)
            for (question in questions(bookId, where, args, order, skip.size + 1, deck, tag, picks)) {
                if (question.long("id") !in skip) return question
                if (fallback == null) fallback = question
            }
        }
        return fallback
    }

    fun nextDue(bookId: Long?, now: Double, deck: Long? = null, tag: String? = null, picks: List<Pick> = emptyList()): Double? =
        questions(bookId, "suspended=0 AND stage>0 AND due>?", listOf(now), "due", 1, deck, tag, picks).firstOrNull()?.double("due")

    // -- questions --------------------------------------------------------------------------
    fun isNew(question: Row) = question.long("reps") == 0L

    /** What practice shows of a question: the latest version with new numbers or new choices
     *  (variants.py), else the question's own. */
    fun content(question: Row): JsonObject {
        val variant = question.str("variant")
        if (variant.isNotBlank()) {
            val v = Json.parseToJsonElement(variant).jsonObject
            if (v["skip"]?.toString() != "true") return current(v)
        }
        return ownContent(question)
    }

    fun ownContent(question: Row): JsonObject = current(Json.parseToJsonElement(question.str("data")).jsonObject)

    /** Content in today's terms: older snapshots and variants may still use a kind's old name. */
    private fun current(q: JsonObject): JsonObject {
        val type = (q["type"] as? JsonPrimitive)?.takeIf { it.isString }?.content
        val now = KIND_ALIASES[type] ?: return q
        return JsonObject(q + ("type" to JsonPrimitive(now)))
    }

    private fun fsrsCard(question: Row, now: Double): FsrsCard {
        val raw = question.str("fsrs")
        return if (raw.isNotBlank()) FsrsCard.fromJson(raw) else FsrsCard.new(due = now, cardId = question.long("id"))
    }

    /** What each rating would schedule: {1: "1m", 3: "10m", ...}. */
    fun previews(question: Row, now: Double): Map<Int, String> {
        val s = scheduler()
        return (1..4).associateWith { r -> formatInterval(s.reviewCard(fsrsCard(question, now), r, now).due - now) }
    }

    fun scheduleColumns(c: FsrsCard): Map<String, Any?> = mapOf(
        "fsrs" to c.toJson(), "stage" to c.state.value.toLong(), "due" to c.due,
        "stability" to c.stability, "difficulty" to c.difficulty, "last_attempt" to c.lastReview,
    )

    /** Schedules a question after it was rated, and logs the attempt (as the desktop's practice does). */
    fun attempt(questionId: Long, rating: Int, now: Double, response: JsonElement? = null, verdict: String? = null,
                duration: Long = 0, wasNew: Boolean? = null): Row? {
        val row = question(questionId) ?: return null
        val after = scheduler().reviewCard(fsrsCard(row, now), rating, now)
        val lapse = rating == 1 && row.long("stage") == 2L
        val cols = scheduleColumns(after) + mapOf("reps" to row.long("reps") + 1, "lapses" to row.long("lapses") + if (lapse) 1 else 0)
        db.transaction {
            db.exec("INSERT INTO attempts (question_id, rating, source, attempted_at, duration, was_new) VALUES (?,?,?,?,?,?)",
                questionId, rating, "practice", now, duration, if (wasNew ?: (row.long("stage") == 0L)) 1 else 0)
            update(questionId, cols)
        }
        return question(questionId)
    }

    fun update(questionId: Long, fields: Map<String, Any?>) {
        if (fields.isEmpty()) return
        db.exec("UPDATE questions SET ${fields.keys.joinToString(", ") { "$it=?" }} WHERE id=?", *(fields.values + questionId).toTypedArray())
    }

    /** Hides a question until the next practice day. */
    fun bury(questionId: Long, now: Double) =
        db.exec("INSERT OR REPLACE INTO buried (question_id, until) VALUES (?,?)", questionId, nextDay(now))
}
