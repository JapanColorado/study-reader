package dev.russell.studyreader.core

import kotlinx.serialization.json.Json
import kotlinx.serialization.json.JsonArray
import kotlinx.serialization.json.JsonElement
import kotlinx.serialization.json.JsonNull
import kotlinx.serialization.json.JsonObject
import kotlinx.serialization.json.JsonPrimitive
import kotlinx.serialization.json.buildJsonArray
import kotlinx.serialization.json.buildJsonObject
import kotlinx.serialization.json.contentOrNull
import kotlinx.serialization.json.jsonObject
import kotlinx.serialization.json.put
import kotlin.math.min

/**
 * The phone's review panel: the desktop's ReviewPanel (review.py) in its all-books form, driving
 * the same review.html. The page calls the bridge methods (start, stop, check, rate, act,
 * chooseDeck, chooseBook, chooseSection, refresh, open); this answers with JavaScript to run
 * (showOverview, showCard, celebrate, inputError) through [js]. Every change for the desktop is
 * queued with SyncStore.record.
 *
 * [now] is wall-clock epoch seconds; [mono] a monotonic clock in seconds (review durations).
 */
class ReviewController(
    val db: Sql,
    val js: (String) -> Unit,
    val now: () -> Double = { System.currentTimeMillis() / 1000.0 },
    val mono: () -> Double = ::monotonic,
    val onOpen: (String) -> Unit = {},
    val onChanged: () -> Unit = {},
) {
    companion object {
        const val MAX_REVIEW_SECONDS = 120.0
        val SELF_RATED = setOf("flashcard", "cloze", "occlusion")
        val CARD_ACTIONS = listOf(
            listOf("skip", "Skip", "Show another card first"),
            listOf("flag", "Flag", "Flag this card (as in Anki)"),
            listOf("bury", "Bury", "Leave this card until tomorrow"),
            listOf("suspend", "Suspend", "Leave this card out of review"),
        )
    }

    val srs = Srs(db)
    val gamify = Gamify(srs) { SyncStore.record(db, it) }

    var deckId: Long? = null
    var pickedBook: Long? = null
    var section: String? = null
    var cardId: Long? = null
    private var shownAt = 0.0
    private var question: JsonObject? = null
    private var shownData = ""
    private var answer: JsonObject? = null
    /** The card just answered, not to be shown again right away. */
    private var just = listOf<Long>()
    /** Cards skipped this session, not to be shown again until the next one. */
    private val skipped = mutableListOf<Long>()
    private var session: Gamify.Session? = null
    private var summary: JsonObject? = null
    /** A line about syncing for the overview, set by the app. */
    var status: String = ""
    /** What the overview says with no cards at all, set by the app. */
    var emptyMessage: String = "No review cards yet."

    private fun call(function: String, vararg args: JsonElement) = js("$function(${args.joinToString(", ")})")

    // -- scope ----------------------------------------------------------------------------
    fun chooseDeck(id: Long?) { deckId = id; pickedBook = null; section = null; stop() }
    fun chooseBook(id: Long?) { deckId = null; pickedBook = id; section = null; stop() }
    fun chooseSection(bookId: Long?, tag: String?) { deckId = null; pickedBook = bookId; section = tag; stop() }

    fun refresh() {
        val id = cardId
        if (id != null && srs.card(id) != null) showCard() else { cardId = null; showOverview() }
    }

    private fun counts(t: Double) = srs.counts(pickedBook, t, deckId, section)

    fun showOverview() {
        val t = now()
        srs.unbury(t)
        if (deckId != null && db.one("SELECT id FROM decks WHERE id=?", deckId) == null) deckId = null
        if (pickedBook != null && db.one("SELECT id FROM books WHERE id=?", pickedBook) == null) { pickedBook = null; section = null }
        if (section != null && srs.countCards(pickedBook, tag = section) == 0) section = null
        val c = counts(t)
        val upcoming = srs.nextDue(pickedBook, t, deckId, section)
        val o = buildJsonObject {
            c.forEach { (k, v) -> put(k, v) }
            put("all", true)
            put("title", scopeTitle())
            put("section", section)
            put("ready", srs.nextCard(pickedBook, t, deck = deckId, tag = section) != null)
            put("next", if (upcoming != null) "in ${Srs.formatInterval(upcoming - t)}" else "")
            put("reviewed", summary?.get("reviewed") ?: JsonPrimitive(0))
            put("summary", summary ?: JsonNull)
            put("new_per_day", srs.newPerDay())
            put("game", if (gamify.enabled()) gamify.status(t) else JsonNull)
            put("quick", buildJsonObject { put("cards", Gamify.QUICK_CARDS); put("minutes", Gamify.QUICK_SECONDS / 60) })
            put("links", JsonArray(emptyList()))
            put("status", status)
            put("empty", emptyMessage)
            decksOverview(t, if (pickedBook == null && deckId == null) c else null).forEach { (k, v) -> put(k, v) }
            booksOverview(t).forEach { (k, v) -> put(k, v) }
        }
        call("showOverview", o)
    }

    private fun scopeTitle(): String {
        val sec = section?.split("::")?.drop(1)?.joinToString(" › ") { it.replace("_", " ") } ?: ""
        if (pickedBook != null) {
            val book = db.one("SELECT title FROM books WHERE id=?", pickedBook)?.str("title") ?: ""
            return listOf(book, sec).filter { it.isNotEmpty() }.joinToString(" › ")
        }
        val deck = deckId?.let { db.one("SELECT name FROM decks WHERE id=?", it) }
        return deck?.str("name")?.replace("::", " › ") ?: ""
    }

    private fun sectionsJson(list: List<Srs.Section>, left: Int) = buildJsonArray {
        list.forEach { n ->
            add(buildJsonObject {
                put("tag", n.tag); put("name", n.name); put("parent", n.parent); put("due", n.due)
                put("new", min(n.new, left)); put("total", n.total); put("page", n.page)
            })
        }
    }

    private fun booksOverview(t: Double): Map<String, JsonElement> {
        val left = srs.newLeftToday(t)
        val rows = db.query(
            "SELECT c.book_id, b.title, SUM(${Srs.dueSql("c.")}) AS due, SUM(c.suspended=0 AND c.stage=0) AS new " +
                "FROM cards c JOIN books b ON b.id=c.book_id WHERE c.id NOT IN (SELECT card_id FROM buried) " +
                "GROUP BY c.book_id ORDER BY b.title COLLATE NOCASE", srs.nextDay(t), t,
        )
        val sections = srs.sections(null, t)
        return mapOf(
            "books" to buildJsonArray {
                rows.forEach { r ->
                    add(buildJsonObject {
                        put("id", r.long("book_id")); put("title", r.str("title"))
                        put("due", (r["due"] as? Number)?.toInt() ?: 0)
                        put("new", min((r["new"] as? Number)?.toInt() ?: 0, left))
                        put("sections", sectionsJson(sections[r.long("book_id")] ?: emptyList(), left))
                    })
                }
            },
            "book" to JsonPrimitive(pickedBook),
        )
    }

    private fun decksOverview(t: Double, everything: Map<String, Int>?): Map<String, JsonElement> {
        val counts = srs.deckCounts(t)
        val bookDecks = db.query("SELECT deck_id FROM books WHERE deck_id IS NOT NULL").map { it.long("deck_id") }.toSet()
        val all = srs.decks().filter { (counts[it.long("id")]?.get("total") ?: 0) > 0 }
        val shown = all.filter { it.long("id") !in bookDecks }.map { it.str("name") }
        val keep = shown.flatMap { n -> val p = n.split("::"); (1..p.size).map { p.take(it).joinToString("::") } }.toSet()
        val decks = all.filter { it.str("name") in keep }
        val byName = decks.associate { it.str("name") to it.long("id") }
        val left = srs.newLeftToday(t)
        val e = everything ?: srs.counts(null, t)
        return mapOf(
            "decks" to buildJsonArray {
                decks.forEach { d ->
                    val name = d.str("name")
                    val c = counts[d.long("id")]!!
                    add(buildJsonObject {
                        put("id", d.long("id")); put("name", name.substringAfterLast("::")); put("path", name)
                        put("parent", if ("::" in name) byName[name.substringBeforeLast("::")] else null)
                        put("due", c["due"]); put("new", min(c["new"]!!, left))
                    })
                }
            },
            "deck" to JsonPrimitive(deckId),
            "all_due" to JsonPrimitive(e["due"]), "all_new" to JsonPrimitive(e["new"]),
        )
    }

    // -- the session ------------------------------------------------------------------------
    fun start(limit: String) {
        session = Gamify.Session(if (limit == "cards" || limit == "time") limit else "", mono())
        summary = null
        just = emptyList(); skipped.clear()
        next()
    }

    fun stop() {
        session?.let { if (it.reviewed > 0) summary = it.summary(false, mono()) }
        session = null
        cardId = null; answer = null
        showOverview()
    }

    private fun next() {
        val s = session
        if (s != null && s.over(mono())) { finish(false); return }
        val t = now()
        srs.unbury(t)
        val card = srs.nextCard(pickedBook, t, just, deckId, section, skipped)
        cardId = card?.long("id")
        answer = null
        shownAt = mono()
        shownData = ""
        // Only skipped cards left (if any): not caught up while they are still to review.
        if (card == null) finish(skipped.isEmpty() || srs.nextCard(pickedBook, t, deck = deckId, tag = section) == null)
        else showCard()
    }

    private fun finish(caughtUp: Boolean) {
        val s = session
        session = null
        cardId = null; answer = null
        if (s == null || s.reviewed == 0) { showOverview(); return }
        val sum = s.summary(caughtUp, mono())
        summary = sum
        val events = gamify.finished(caughtUp, (sum["quick"] as JsonPrimitive).content == "true", now())
        showOverview()
        celebrate(events + buildJsonObject { put("kind", "done"); put("caught_up", caughtUp); put("quick", sum["quick"]!!) })
        onChanged()
    }

    private fun celebrate(events: List<JsonObject>) {
        if (events.isNotEmpty() && gamify.enabled()) call("celebrate", JsonArray(events))
    }

    fun showCard() {
        val card = srs.card(cardId ?: return) ?: return
        if (card.str("data") != shownData) { question = srs.question(card); shownData = card.str("data") }
        val t = now()
        val a = answer
        val c = counts(t)
        // The session's progress leaves out cards skipped in it, which won't come back in it.
        val left = if (skipped.isEmpty()) c["due"]!! + c["new"]!! else srs.leftToReview(pickedBook, t, deckId, section, skipped)
        val book = card.longOrNull("book_id")?.let { db.one("SELECT title FROM books WHERE id=?", it) }
        val deck = db.one("SELECT name FROM decks WHERE id=?", card.long("deck_id"))
        val q = question!!
        call("showCard", buildJsonObject {
            put("id", card.long("id"))
            put("question", q)
            put("varied", q != srs.ownQuestion(card))
            put("title", card.str("title"))
            put("deck", deck?.str("name") ?: "")
            put("book", book?.str("title") ?: "")
            put("page", JsonNull)
            put("chat", false)
            put("new", srs.isNew(card))
            put("flag", card.long("flag"))
            put("answer", a ?: JsonNull)
            put("previews", if (a != null) buildJsonObject { srs.previews(card, t).forEach { (k, v) -> put("$k", v) } } else JsonNull)
            put("suggested", a?.let { (it["verdict"] as? JsonPrimitive)?.contentOrNull }?.let { Srs.VERDICT_RATINGS[it] })
            put("remaining", buildJsonObject { put("due", c["due"]); put("new", c["new"]) })
            put("session", session?.progress(left, mono()) ?: JsonNull)
            put("rewards", gamify.enabled())
            put("actions", buildJsonArray { CARD_ACTIONS.forEach { add(buildJsonArray { it.forEach { s -> add(JsonPrimitive(s)) } }) } })
        })
    }

    /** An answer, or a request to see the answer (self-rated cards and free responses). */
    fun check(id: Long, responseJson: String) {
        if (id != cardId || answer != null || srs.card(id) == null) return
        val q = question ?: return
        val response = try { Json.parseToJsonElement(responseJson).jsonObject } catch (_: Exception) { JsonObject(emptyMap()) }
        val type = (q["type"] as? JsonPrimitive)?.content
        var verdict: String? = null
        if (type !in SELF_RATED && type != "free") {
            try {
                verdict = Grading.grade(q, response)
            } catch (e: AnswerError) {
                call("inputError", JsonPrimitive(id), JsonPrimitive(e.message ?: "Can't read that"))
                return
            } catch (e: Exception) {
                call("inputError", JsonPrimitive(id), JsonPrimitive(e.message ?: e.toString()))
                return
            }
        }
        answer = buildJsonObject { put("response", response); put("status", "done"); put("verdict", verdict); put("feedback", "") }
        showCard()
    }

    fun rate(id: Long, rating: Int) {
        if (id != cardId || rating !in 1..4) return
        val a = answer
        val seconds = mono() - shownAt
        val duration = (min(seconds, MAX_REVIEW_SECONDS) * 1000).toLong()
        val before = srs.card(id) ?: return
        val t = now()
        val response = a?.get("response") as? JsonObject
        val verdict = (a?.get("verdict") as? JsonPrimitive)?.contentOrNull
        val wasNew = before.long("stage") == 0L
        val events = db.transaction {
            val after = srs.review(id, rating, t, response, verdict, duration) ?: return@transaction emptyList()
            SyncStore.record(db, SyncStore.reviewEvent(id, t, rating, duration, response, verdict, wasNew))
            val s = session ?: Gamify.Session(started = mono()).also { session = it }
            val sessionEvents = s.record(before, after, rating, seconds)
            val all = gamify.reviewed(srs.isNew(before), s.multiplier, t, s.best, s.reviewed) + sessionEvents
            s.xp += all.filter { (it["kind"] as JsonPrimitive).content == "xp" }.sumOf { (it["xp"] as JsonPrimitive).content.toInt() }
            all
        }
        just = listOf(id)
        onChanged()
        next()
        celebrate(events)
    }

    fun act(id: Long, action: String) {
        val card = srs.card(id) ?: return
        val t = now()
        when {
            action == "skip" -> { skipped.add(id); next() }
            action == "suspend" -> {
                db.transaction {
                    srs.update(id, mapOf("suspended" to 1L))
                    SyncStore.record(db, SyncStore.cardEvent("suspend", id, t) { put("suspended", 1) })
                }
                onChanged(); next()
            }
            action == "bury" -> {
                db.transaction {
                    srs.bury(id, t)
                    SyncStore.record(db, SyncStore.cardEvent("bury", id, t))
                }
                onChanged(); next()
            }
            action.startsWith("flag:") -> {
                val flag = action.removePrefix("flag:").toIntOrNull()?.coerceIn(0, 7) ?: return
                // The same flag again takes it off, as in Anki.
                val value = if (flag != 0 && card.long("flag") == flag.toLong()) 0 else flag
                db.transaction {
                    srs.update(id, mapOf("flag" to value.toLong()))
                    SyncStore.record(db, SyncStore.cardEvent("flag", id, t) { put("flag", value) })
                }
                onChanged()
                if (id == cardId) showCard()
            }
        }
    }

    fun open(what: String) = onOpen(what)
}
