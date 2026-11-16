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
 * The phone's practice panel: the desktop's PracticePanel (practice_view.py), driving the same practice.html.
 * The page calls the bridge methods (start, stop, check, rate, act, choose, refresh, open); this answers with JavaScript to run (showOverview, showQuestion,
 * celebrate, inputError) through [js]. Every change for the desktop is
 * queued with SyncStore.record.
 *
 * [now] is wall-clock epoch seconds; [mono] a monotonic clock in seconds (attempt durations).
 */
class PracticeController(
    val db: Sql,
    val js: (String) -> Unit,
    val now: () -> Double = { System.currentTimeMillis() / 1000.0 },
    val mono: () -> Double = ::monotonic,
    val onOpen: (String) -> Unit = {},
    val onChanged: () -> Unit = {},
) {
    companion object {
        const val MAX_ATTEMPT_SECONDS = 120.0
        /** Kinds the user rates themselves (short answers too, in practice: Claude grades them only in the chat). */
        val SELF_RATED = setOf("self_check", "both_ways", "fill_in", "image_labels", "short_answer")
        val QUESTION_ACTIONS = listOf(
            listOf("skip", "Skip", "Show another question first"),
            listOf("flag", "Flag", "Flag this question"),
            listOf("bury", "Bury", "Leave this question until tomorrow"),
            listOf("suspend", "Suspend", "Leave this question out of practice"),
        )
    }

    val practice = Practice(db)
    val gamify = Gamify(practice) { SyncStore.record(db, it) }

    /** What to practice: any of these decks, books and sections of books; none for every question. */
    var picks: List<Practice.Pick> = emptyList()
    var questionId: Long? = null
    private var shownAt = 0.0
    /** The shown question's content (its variant, if any). */
    private var content: JsonObject? = null
    private var shownData = ""
    private var answer: JsonObject? = null
    /** The question just answered, not to be shown again right away. */
    private var just = listOf<Long>()
    /** Questions skipped this session, not to be shown again until the next one. */
    private val skipped = mutableListOf<Long>()
    private var session: Gamify.Session? = null
    private var summary: JsonObject? = null
    /** A line about syncing for the overview, set by the app. */
    var status: String = ""
    /** What the overview says with no questions at all, set by the app. */
    var emptyMessage: String = "No questions yet."

    private fun call(function: String, vararg args: JsonElement) = js("$function(${args.joinToString(", ")})")

    // -- scope ----------------------------------------------------------------------------
    /** Practice the questions in any of picks, as the page sends them: JSON [{deck} or {book, section}]. */
    fun choose(picksJson: String) {
        val list = try { Json.parseToJsonElement(picksJson) as? JsonArray } catch (_: Exception) { null }
        choose(list.orEmpty().mapNotNull { e ->
            val o = e as? JsonObject ?: return@mapNotNull null
            fun long(k: String) = (o[k] as? JsonPrimitive)?.contentOrNull?.toDoubleOrNull()?.toLong()?.takeIf { it != 0L }
            val deck = long("deck")
            val book = long("book")
            when {
                deck != null -> Practice.Pick(deck = deck)
                book != null -> Practice.Pick(book = book, section = (o["section"] as? JsonPrimitive)?.contentOrNull?.ifEmpty { null })
                else -> null
            }
        })
    }

    fun choose(picks: List<Practice.Pick>) { this.picks = picks.distinct(); stop() }

    /** pick, or (a section whose questions are gone or retagged) its book, or null if it is gone. */
    private fun stillThere(p: Practice.Pick): Practice.Pick? = when {
        p.deck != null -> p.takeIf { db.one("SELECT id FROM decks WHERE id=?", p.deck) != null }
        db.one("SELECT id FROM books WHERE id=?", p.book) == null -> null
        p.section != null && practice.countQuestions(p.book, tag = p.section) == 0 -> Practice.Pick(book = p.book)
        else -> p
    }

    private fun pickJson(p: Practice.Pick) = buildJsonObject {
        if (p.deck != null) put("deck", p.deck) else { put("book", p.book); put("section", p.section) }
    }

    fun refresh() {
        val id = questionId
        if (id != null && practice.question(id) != null) showQuestion() else { questionId = null; showOverview() }
    }

    private fun counts(t: Double) = practice.counts(null, t, picks = picks)

    fun showOverview() {
        val t = now()
        practice.unbury(t)
        picks = picks.mapNotNull(::stillThere).distinct()
        val c = counts(t)
        val upcoming = practice.nextDue(null, t, picks = picks)
        val o = buildJsonObject {
            c.forEach { (k, v) -> put(k, v) }
            put("all", true)
            put("title", scopeTitle())
            put("picks", JsonArray(picks.map(::pickJson)))
            put("ready", practice.nextQuestion(null, t, picks = picks) != null)
            put("next", if (upcoming != null) "in ${Practice.formatInterval(upcoming - t)}" else "")
            put("attempted", summary?.get("attempted") ?: JsonPrimitive(0))
            put("summary", summary ?: JsonNull)
            put("new_per_day", practice.newPerDay())
            put("game", if (gamify.enabled()) gamify.status(t) else JsonNull)
            put("quick", buildJsonObject { put("questions", Gamify.QUICK_QUESTIONS); put("minutes", Gamify.QUICK_SECONDS / 60) })
            put("links", JsonArray(emptyList()))
            put("status", status)
            put("empty", emptyMessage)
            decksOverview(t, if (picks.isEmpty()) c else null).forEach { (k, v) -> put(k, v) }
            booksOverview(t).forEach { (k, v) -> put(k, v) }
        }
        call("showOverview", o)
    }

    private fun scopeTitle(): String {
        var names = picks.map(::pickTitle)
        if (names.size > 3) names = names.take(2) + "${names.size - 2} more"
        return if (names.size < 2) names.firstOrNull() ?: "" else names.dropLast(1).joinToString(", ") + " and " + names.last()
    }

    private fun pickTitle(p: Practice.Pick): String {
        if (p.deck != null) return db.one("SELECT name FROM decks WHERE id=?", p.deck)?.str("name")?.replace("::", " › ") ?: ""
        val sec = p.section?.split("::")?.drop(1)?.joinToString(" › ") { it.replace("_", " ") } ?: ""
        val book = db.one("SELECT title FROM books WHERE id=?", p.book)?.str("title") ?: ""
        return listOf(book, sec).filter { it.isNotEmpty() }.joinToString(" › ")
    }

    private fun sectionsJson(list: List<Practice.Section>, left: Int) = buildJsonArray {
        list.forEach { n ->
            add(buildJsonObject {
                put("tag", n.tag); put("name", n.name); put("parent", n.parent); put("due", n.due)
                put("new", min(n.new, left)); put("total", n.total); put("page", n.page)
            })
        }
    }

    private fun booksOverview(t: Double): Map<String, JsonElement> {
        val left = practice.newLeftToday(t)
        val rows = db.query(
            "SELECT q.book_id, b.title, SUM(${Practice.dueSql("q.")}) AS due, SUM(q.suspended=0 AND q.stage=0) AS new " +
                "FROM questions q JOIN books b ON b.id=q.book_id WHERE q.id NOT IN (SELECT question_id FROM buried) " +
                "GROUP BY q.book_id ORDER BY b.title COLLATE NOCASE", practice.nextDay(t), t,
        )
        val sections = practice.sections(null, t)
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
        )
    }

    private fun decksOverview(t: Double, everything: Map<String, Int>?): Map<String, JsonElement> {
        val counts = practice.deckCounts(t)
        val bookDecks = db.query("SELECT deck_id FROM books WHERE deck_id IS NOT NULL").map { it.long("deck_id") }.toSet()
        val all = practice.decks().filter { (counts[it.long("id")]?.get("total") ?: 0) > 0 }
        val shown = all.filter { it.long("id") !in bookDecks }.map { it.str("name") }
        val keep = shown.flatMap { n -> val p = n.split("::"); (1..p.size).map { p.take(it).joinToString("::") } }.toSet()
        val decks = all.filter { it.str("name") in keep }
        val byName = decks.associate { it.str("name") to it.long("id") }
        val left = practice.newLeftToday(t)
        val e = everything ?: practice.counts(null, t)
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
            "all_due" to JsonPrimitive(e["due"]), "all_new" to JsonPrimitive(e["new"]),
        )
    }

    // -- the session ------------------------------------------------------------------------
    fun start(limit: String) {
        session = Gamify.Session(if (limit == Gamify.Session.QUESTIONS || limit == Gamify.Session.TIME) limit else "", mono())
        summary = null
        just = emptyList(); skipped.clear()
        next()
    }

    fun stop() {
        session?.let { if (it.attempted > 0) summary = it.summary(false, mono()) }
        session = null
        questionId = null; answer = null
        showOverview()
    }

    private fun next() {
        val s = session
        if (s != null && s.over(mono())) { finish(false); return }
        val t = now()
        practice.unbury(t)
        val question = practice.nextQuestion(null, t, just, exclude = skipped, picks = picks)
        questionId = question?.long("id")
        answer = null
        shownAt = mono()
        shownData = ""
        // Only skipped questions left (if any): not caught up while they are still to practice.
        if (question == null) finish(skipped.isEmpty() || practice.nextQuestion(null, t, picks = picks) == null)
        else showQuestion()
    }

    private fun finish(caughtUp: Boolean) {
        val s = session
        session = null
        questionId = null; answer = null
        if (s == null || s.attempted == 0) { showOverview(); return }
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

    fun showQuestion() {
        val question = practice.question(questionId ?: return) ?: return
        if (question.str("data") != shownData) { content = practice.content(question); shownData = question.str("data") }
        val t = now()
        val a = answer
        val c = counts(t)
        // The session's progress leaves out questions skipped in it, which won't come back in it.
        val left = if (skipped.isEmpty()) c["due"]!! + c["new"]!! else practice.leftToPractice(null, t, exclude = skipped, picks = picks)
        val book = question.longOrNull("book_id")?.let { db.one("SELECT title FROM books WHERE id=?", it) }
        val deck = db.one("SELECT name FROM decks WHERE id=?", question.long("deck_id"))
        val q = content!!
        call("showQuestion", buildJsonObject {
            put("id", question.long("id"))
            put("question", q)
            put("varied", q != practice.ownContent(question))
            put("title", question.str("title"))
            put("deck", deck?.str("name") ?: "")
            put("book", book?.str("title") ?: "")
            put("page", JsonNull)
            put("chat", false)
            put("new", practice.isNew(question))
            put("flag", question.long("flag"))
            put("answer", a ?: JsonNull)
            put("previews", if (a != null) buildJsonObject { practice.previews(question, t).forEach { (k, v) -> put("$k", v) } } else JsonNull)
            put("suggested", a?.let { (it["verdict"] as? JsonPrimitive)?.contentOrNull }?.let { Practice.VERDICT_RATINGS[it] })
            put("remaining", buildJsonObject { put("due", c["due"]); put("new", c["new"]) })
            put("session", session?.progress(left, mono()) ?: JsonNull)
            put("rewards", gamify.enabled())
            put("actions", buildJsonArray { QUESTION_ACTIONS.forEach { add(buildJsonArray { it.forEach { s -> add(JsonPrimitive(s)) } }) } })
        })
    }

    /** An answer, or a request to see the answer (self-rated questions and short answers). */
    fun check(id: Long, responseJson: String) {
        if (id != questionId || answer != null || practice.question(id) == null) return
        val q = content ?: return
        val response = try { Json.parseToJsonElement(responseJson).jsonObject } catch (_: Exception) { JsonObject(emptyMap()) }
        val type = Practice.kind((q["type"] as? JsonPrimitive)?.content)
        var verdict: String? = null
        if (type !in SELF_RATED) {
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
        showQuestion()
    }

    fun rate(id: Long, rating: Int) {
        if (id != questionId || rating !in 1..4) return
        val a = answer
        val seconds = mono() - shownAt
        val duration = (min(seconds, MAX_ATTEMPT_SECONDS) * 1000).toLong()
        val before = practice.question(id) ?: return
        val t = now()
        val response = a?.get("response") as? JsonObject
        val verdict = (a?.get("verdict") as? JsonPrimitive)?.contentOrNull
        val wasNew = before.long("stage") == 0L
        val events = db.transaction {
            val after = practice.attempt(id, rating, t, response, verdict, duration) ?: return@transaction emptyList()
            SyncStore.record(db, SyncStore.attemptEvent(id, t, rating, duration, response, verdict, wasNew))
            val s = session ?: Gamify.Session(started = mono()).also { session = it }
            val sessionEvents = s.record(before, after, rating, seconds)
            val all = gamify.attempted(practice.isNew(before), s.multiplier, t, s.best, s.attempted) + sessionEvents
            s.xp += all.filter { (it["kind"] as JsonPrimitive).content == "xp" }.sumOf { (it["xp"] as JsonPrimitive).content.toInt() }
            all
        }
        just = listOf(id)
        onChanged()
        next()
        celebrate(events)
    }

    fun act(id: Long, action: String) {
        val question = practice.question(id) ?: return
        val t = now()
        when {
            action == "skip" -> { skipped.add(id); next() }
            action == "suspend" -> {
                db.transaction {
                    practice.update(id, mapOf("suspended" to 1L))
                    SyncStore.record(db, SyncStore.questionEvent("suspend", id, t) { put("suspended", 1) })
                }
                onChanged(); next()
            }
            action == "bury" -> {
                db.transaction {
                    practice.bury(id, t)
                    SyncStore.record(db, SyncStore.questionEvent("bury", id, t))
                }
                onChanged(); next()
            }
            action.startsWith("flag:") -> {
                val flag = action.removePrefix("flag:").toIntOrNull()?.coerceIn(0, 7) ?: return
                // The same flag again takes it off.
                val value = if (flag != 0 && question.long("flag") == flag.toLong()) 0 else flag
                db.transaction {
                    practice.update(id, mapOf("flag" to value.toLong()))
                    SyncStore.record(db, SyncStore.questionEvent("flag", id, t) { put("flag", value) })
                }
                onChanged()
                if (id == questionId) showQuestion()
            }
        }
    }

    fun open(what: String) = onOpen(what)
}
