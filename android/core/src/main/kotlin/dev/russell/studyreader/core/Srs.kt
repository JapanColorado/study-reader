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
 * The review queue over the phone's copy of the cards, as the desktop's srs.py runs it: what is
 * due, new cards within the daily allowance, sections, and scheduling a review with FSRS.
 * Times are epoch seconds. Cards the phone buried (the `buried` table) stay out until it empties.
 */
class Srs(val db: Sql, val zone: ZoneId = ZoneId.systemDefault(), val fuzz: Boolean = true) {
    companion object {
        const val NEW_PER_DAY_SETTING = "srs_new_per_day"
        const val DEFAULT_NEW_PER_DAY = 20
        const val RETENTION_SETTING = "srs_desired_retention"
        const val PARAMETERS_SETTING = "srs_parameters"
        const val DEFAULT_RETENTION = 0.9
        const val DAY_STARTS_AT_HOUR = 4
        const val LEARN_AHEAD = 20 * 60.0
        val VERDICT_RATINGS = mapOf("correct" to 3, "partial" to 2, "incorrect" to 1)

        // Due cards; args (nextDay(now), now).
        fun dueSql(alias: String = "") =
            "${alias}suspended=0 AND ((${alias}stage=2 AND ${alias}due<?) OR (${alias}stage IN (1,3) AND ${alias}due<=?))"
        val DUE = dueSql()
        const val NEW = "suspended=0 AND stage=0"

        /** 'in 5m'-style intervals, as srs.format_interval. */
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

    /** The review day a moment falls on. */
    fun dayOf(t: Double): LocalDate = ZonedDateTime.ofInstant(instant(dayStart(t)), zone).toLocalDate()

    private fun instant(t: Double) = Instant.ofEpochMilli((t * 1000).roundToLong())

    // -- scope ------------------------------------------------------------------------------
    private fun like(s: String) = s.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")

    /** One of several things to review together (review.py's ReviewPanel.picks): a deck (with its
     *  subdecks), a book, or a section (tag) of a book. */
    data class Pick(val deck: Long? = null, val book: Long? = null, val section: String? = null)

    /** SQL condition (ending in AND) and its arguments for a book, deck (with its subdecks) or tag, and
     *  within any of picks (none: every card). */
    fun scope(bookId: Long?, deckId: Long?, tag: String? = null, picks: List<Pick> = emptyList()): Pair<String, List<Any?>> {
        val sql = StringBuilder("id NOT IN (SELECT card_id FROM buried) AND ")
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
            sql.append("note_id IN (SELECT id FROM card_notes WHERE tags LIKE ? ESCAPE '\\' OR tags LIKE ? ESCAPE '\\') AND ")
            args += "% ${like(tag)} %"; args += "% ${like(tag)}::%"
        }
        return sql.toString() to args
    }

    fun cards(bookId: Long? = null, where: String = "1", args: List<Any?> = emptyList(), order: String = "id",
              limit: Int = -1, deckId: Long? = null, tag: String? = null, picks: List<Pick> = emptyList()): List<Row> {
        val (s, a) = scope(bookId, deckId, tag, picks)
        return db.query("SELECT * FROM cards WHERE $s($where) ORDER BY $order LIMIT ?", *(a + args + limit).toTypedArray())
    }

    fun countCards(bookId: Long? = null, where: String = "1", args: List<Any?> = emptyList(),
                   deckId: Long? = null, tag: String? = null, picks: List<Pick> = emptyList()): Int {
        val (s, a) = scope(bookId, deckId, tag, picks)
        return db.long("SELECT COUNT(*) FROM cards WHERE $s($where)", *(a + args).toTypedArray()).toInt()
    }

    fun card(id: Long): Row? = db.one("SELECT * FROM cards WHERE id=?", id)

    /** Lets buried cards back in once their day is over. */
    fun unbury(now: Double) = db.exec("DELETE FROM buried WHERE until<=?", now)

    fun newCardsReviewedSince(since: Double): Int =
        db.long("SELECT COUNT(*) FROM reviews WHERE source='review' AND was_new=1 AND reviewed_at>=?", since).toInt()

    fun newLeftToday(now: Double): Int = max(newPerDay() - newCardsReviewedSince(dayStart(now)), 0)

    // -- counts -----------------------------------------------------------------------------
    fun counts(bookId: Long?, now: Double, deck: Long? = null, tag: String? = null,
               picks: List<Pick> = emptyList()): Map<String, Int> {
        val new = countCards(bookId, NEW, deckId = deck, tag = tag, picks = picks)
        return mapOf(
            "due" to countCards(bookId, DUE, listOf(nextDay(now), now), deck, tag, picks),
            "new" to min(new, newLeftToday(now)),
            "new_total" to new,
            "total" to countCards(bookId, deckId = deck, tag = tag, picks = picks),
            "suspended" to countCards(bookId, "suspended=1", deckId = deck, tag = tag, picks = picks),
        )
    }

    /** How many cards are left to review today (due, and new ones today's allowance allows),
     *  leaving out exclude (cards skipped this session). */
    fun leftToReview(bookId: Long?, now: Double, deck: Long? = null, tag: String? = null,
                     exclude: List<Long> = emptyList(), picks: List<Pick> = emptyList()): Int {
        val (dueWhere, dueArgs) = notIn(DUE, listOf(nextDay(now), now), exclude)
        val (newWhere, newArgs) = notIn(NEW, emptyList(), exclude)
        return countCards(bookId, dueWhere, dueArgs, deck, tag, picks) +
            min(countCards(bookId, newWhere, newArgs, deck, tag, picks), newLeftToday(now))
    }

    /** where, also leaving out the cards with the ids in exclude. */
    private fun notIn(where: String, args: List<Any?>, exclude: List<Long>): Pair<String, List<Any?>> =
        if (exclude.isEmpty()) where to args
        else "($where) AND id NOT IN (${exclude.joinToString(",") { "?" }})" to args + exclude

    fun decks(): List<Row> = db.query("SELECT * FROM decks ORDER BY name COLLATE NOCASE")

    /** For every deck, its cards and those of the decks under it: {due, new (uncapped), total}. */
    fun deckCounts(now: Double): Map<Long, MutableMap<String, Int>> {
        val own = db.query(
            "SELECT deck_id, SUM($DUE) AS due, SUM($NEW) AS new, COUNT(*) AS total FROM cards " +
                "WHERE id NOT IN (SELECT card_id FROM buried) GROUP BY deck_id", nextDay(now), now,
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

    /** Port of srs.sections: per book, the section tags of its cards as a tree in reading order. */
    fun sections(bookId: Long? = null, now: Double): Map<Long, List<Section>> {
        val roots = db.query("SELECT id, root FROM books").associate { it.long("id") to it.str("root") }
        val scopeSql = if (bookId != null) "AND c.book_id=?" else ""
        val args = mutableListOf<Any?>(nextDay(now), now)
        if (bookId != null) args += bookId
        val rows = db.query(
            "SELECT c.book_id, c.page, n.tags, ${dueSql("c.")} AS due, c.suspended=0 AND c.stage=0 AS new " +
                "FROM cards c JOIN card_notes n ON n.id=c.note_id WHERE c.book_id IS NOT NULL AND n.tags!='' " +
                "AND c.id NOT IN (SELECT card_id FROM buried) $scopeSql", *args.toTypedArray(),
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
    /** skip: cards not to show again yet (the one just answered), unless nothing else is left;
     *  exclude: cards never to show (skipped this session). */
    fun nextCard(bookId: Long?, now: Double, skip: List<Long> = emptyList(), deck: Long? = null, tag: String? = null,
                 exclude: List<Long> = emptyList(), picks: List<Pick> = emptyList()): Row? {
        val tiers = mutableListOf(Triple(DUE, listOf<Any?>(nextDay(now), now), "due, id"))
        if (newLeftToday(now) > 0) tiers += Triple(NEW, emptyList(), "created_at, id")
        tiers += Triple("suspended=0 AND stage IN (1,3) AND due<=?", listOf(now + LEARN_AHEAD), "due, id")
        var fallback: Row? = null
        for ((tierWhere, tierArgs, order) in tiers) {
            val (where, args) = notIn(tierWhere, tierArgs, exclude)
            for (card in cards(bookId, where, args, order, skip.size + 1, deck, tag, picks)) {
                if (card.long("id") !in skip) return card
                if (fallback == null) fallback = card
            }
        }
        return fallback
    }

    fun nextDue(bookId: Long?, now: Double, deck: Long? = null, tag: String? = null, picks: List<Pick> = emptyList()): Double? =
        cards(bookId, "suspended=0 AND stage>0 AND due>?", listOf(now), "due", 1, deck, tag, picks).firstOrNull()?.double("due")

    // -- cards ------------------------------------------------------------------------------
    fun isNew(card: Row) = card.long("reps") == 0L

    /** The question review shows: the latest version with new numbers or new choices (variants.py), else the card's own. */
    fun question(card: Row): JsonObject {
        val variant = card.str("variant")
        if (variant.isNotBlank()) {
            val v = Json.parseToJsonElement(variant).jsonObject
            if (v["skip"]?.toString() != "true") return current(v)
        }
        return ownQuestion(card)
    }

    fun ownQuestion(card: Row): JsonObject = current(Json.parseToJsonElement(card.str("data")).jsonObject)

    /** A question in today's terms: a snapshot from an older desktop still calls written answers "free". */
    private fun current(q: JsonObject): JsonObject =
        if ((q["type"] as? JsonPrimitive)?.content == "free") JsonObject(q + ("type" to JsonPrimitive("written"))) else q

    private fun fsrsCard(card: Row, now: Double): FsrsCard {
        val raw = card.str("fsrs")
        return if (raw.isNotBlank()) FsrsCard.fromJson(raw) else FsrsCard.new(due = now, cardId = card.long("id"))
    }

    /** What each rating would schedule: {1: "1m", 3: "10m", ...}. */
    fun previews(card: Row, now: Double): Map<Int, String> {
        val s = scheduler()
        return (1..4).associateWith { r -> formatInterval(s.reviewCard(fsrsCard(card, now), r, now).due - now) }
    }

    fun scheduleColumns(c: FsrsCard): Map<String, Any?> = mapOf(
        "fsrs" to c.toJson(), "stage" to c.state.value.toLong(), "due" to c.due,
        "stability" to c.stability, "difficulty" to c.difficulty, "last_review" to c.lastReview,
    )

    /** Schedules a card after it was rated, and logs the review (as srs.review). */
    fun review(cardId: Long, rating: Int, now: Double, response: JsonElement? = null, verdict: String? = null,
               duration: Long = 0, wasNew: Boolean? = null): Row? {
        val card = card(cardId) ?: return null
        val after = scheduler().reviewCard(fsrsCard(card, now), rating, now)
        val lapse = rating == 1 && card.long("stage") == 2L
        val cols = scheduleColumns(after) + mapOf("reps" to card.long("reps") + 1, "lapses" to card.long("lapses") + if (lapse) 1 else 0)
        db.transaction {
            db.exec("INSERT INTO reviews (card_id, rating, source, reviewed_at, duration, was_new) VALUES (?,?,?,?,?,?)",
                cardId, rating, "review", now, duration, if (wasNew ?: (card.long("stage") == 0L)) 1 else 0)
            update(cardId, cols)
        }
        return card(cardId)
    }

    fun update(cardId: Long, fields: Map<String, Any?>) {
        if (fields.isEmpty()) return
        db.exec("UPDATE cards SET ${fields.keys.joinToString(", ") { "$it=?" }} WHERE id=?", *(fields.values + cardId).toTypedArray())
    }

    /** Hides a card until the next review day. */
    fun bury(cardId: Long, now: Double) =
        db.exec("INSERT OR REPLACE INTO buried (card_id, until) VALUES (?,?)", cardId, nextDay(now))
}
