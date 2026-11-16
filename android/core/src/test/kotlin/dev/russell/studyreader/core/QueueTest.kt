package dev.russell.studyreader.core

import kotlinx.serialization.json.Json
import kotlinx.serialization.json.JsonArray
import kotlinx.serialization.json.JsonNull
import kotlinx.serialization.json.JsonObject
import kotlinx.serialization.json.JsonPrimitive
import kotlinx.serialization.json.booleanOrNull
import kotlinx.serialization.json.doubleOrNull
import kotlinx.serialization.json.intOrNull
import kotlinx.serialization.json.jsonArray
import kotlinx.serialization.json.jsonObject
import kotlinx.serialization.json.jsonPrimitive
import kotlinx.serialization.json.long
import kotlinx.serialization.json.longOrNull
import org.junit.After
import org.junit.Assert.assertEquals
import org.junit.Assert.assertNotNull
import org.junit.Assert.assertThrows
import org.junit.Assert.assertTrue
import org.junit.Before
import org.junit.Test
import java.io.File
import java.time.ZoneId

/**
 * The queue, counts, sections, previews and rewards against what the desktop's practice.py and
 * gamify.py say about the same questions (scripts/queue_fixtures.py), over a snapshot the desktop's
 * phone_sync.export_snapshot wrote.
 */
class QueueTest {
    private val cases = Json.parseToJsonElement(javaClass.getResource("/queue_cases.json")!!.readText()).jsonObject
    private val now = cases["now"]!!.jsonPrimitive.doubleOrNull!!
    private lateinit var file: File
    private lateinit var db: JdbcSql
    private lateinit var practice: Practice

    @Before
    fun open() {
        file = File.createTempFile("snapshot", ".db")
        javaClass.getResourceAsStream("/queue_snapshot.db")!!.use { input -> file.outputStream().use { input.copyTo(it) } }
        db = JdbcSql(file.path)
        SyncStore.prepare(db)
        practice = Practice(db, ZoneId.of("UTC"), fuzz = false)
    }

    @After
    fun close() { db.close(); file.delete() }

    private fun long(o: JsonObject, k: String) = (o[k] as? JsonPrimitive)?.longOrNull
    private fun int(o: JsonObject, k: String) = o[k]!!.jsonPrimitive.intOrNull!!

    @Test
    fun countsAndNextQuestionMatchTheDesktop() {
        assertEquals(int(cases, "new_left_today"), practice.newLeftToday(now))
        for (e in cases["scopes"]!!.jsonArray) {
            val sc = e.jsonObject
            val book = long(sc, "book"); val deck = long(sc, "deck"); val tag = (sc["tag"] as? JsonPrimitive)?.takeIf { it !is JsonNull }?.content
            val label = "scope $sc"
            val want = sc["counts"]!!.jsonObject
            val got = practice.counts(book, now, deck, tag)
            for (k in listOf("due", "new", "new_total", "total", "suspended")) assertEquals("$label $k", int(want, k), got[k])
            val first = practice.nextQuestion(book, now, emptyList(), deck, tag)
            assertEquals(label, long(sc, "next"), first?.long("id"))
            val second = practice.nextQuestion(book, now, listOfNotNull(first?.long("id")), deck, tag)
            assertEquals(label, long(sc, "next_skipping"), second?.long("id"))
            // Excluded (skipped this session): never shown, even with nothing else left.
            val excluding = practice.nextQuestion(book, now, emptyList(), deck, tag, listOfNotNull(first?.long("id")))
            val wantExcluding = if (first == null || long(sc, "next_skipping") == first.long("id")) null else long(sc, "next_skipping")
            assertEquals(label, wantExcluding, excluding?.long("id"))
            assertEquals(label, int(want, "due") + int(want, "new"), practice.leftToPractice(book, now, deck, tag))
            if (first != null) {
                val was = if (first.long("stage") == 0L) "new" else "due"
                val capped = was == "new" && int(want, "new") < int(want, "new_total")
                assertEquals(label, int(want, "due") + int(want, "new") - (if (capped) 0 else 1),
                    practice.leftToPractice(book, now, deck, tag, listOf(first.long("id"))))
            }
            assertEquals(label, (sc["next_due"] as? JsonPrimitive)?.doubleOrNull, practice.nextDue(book, now, deck, tag))
        }
    }

    @Test
    fun picksPracticeAnyOfSeveral() {
        // One pick narrows as the scope it names does; several practice the questions in any of them, once each.
        val picks = mutableListOf<Practice.Pick>()
        for (e in cases["scopes"]!!.jsonArray) {
            val sc = e.jsonObject
            val book = long(sc, "book"); val deck = long(sc, "deck"); val tag = (sc["tag"] as? JsonPrimitive)?.takeIf { it !is JsonNull }?.content
            if ((book == null) == (deck == null)) continue
            val pick = if (deck != null) Practice.Pick(deck = deck) else Practice.Pick(book = book, section = tag)
            assertEquals("scope $sc", practice.counts(book, now, deck, tag), practice.counts(null, now, picks = listOf(pick, pick)))
            picks += pick
        }
        assertTrue(picks.size >= 2)
        val ids = picks.flatMap { p -> practice.questions(p.book, deckId = p.deck, tag = p.section).map { it.long("id") } }.toSet()
        assertEquals(ids.size, practice.counts(null, now, picks = picks)["total"])

        val calls = mutableListOf<String>()
        val c = PracticeController(db, { calls += it }, now = { now })
        val (a, b) = picks.take(2)
        fun json(p: Practice.Pick) = if (p.deck != null) "{\"deck\": ${p.deck}}" else "{\"book\": ${p.book}, \"section\": ${p.section?.let { "\"$it\"" } ?: "null"}}"
        c.choose("[${json(a)}, ${json(b)}, ${json(a)}, {\"deck\": 0}]")
        assertEquals(listOf(a, b), c.picks)
        assertTrue(calls.last(), calls.last().startsWith("showOverview(") && calls.last().contains("\"picks\":[{"))
        c.choose("[]")
        assertEquals(emptyList<Practice.Pick>(), c.picks)
    }

    @Test
    fun deckCountsMatch() {
        val got = practice.deckCounts(now)
        for ((id, want) in cases["deck_counts"]!!.jsonObject) {
            for (k in listOf("due", "new", "total")) assertEquals("deck $id $k", int(want.jsonObject, k), got[id.toLong()]!![k])
        }
    }

    @Test
    fun sectionsMatch() {
        val got = practice.sections(null, now)
        val want = cases["sections"]!!.jsonObject
        assertEquals(want.keys.map { it.toLong() }.toSet(), got.keys)
        for ((book, list) in want) {
            val mine = got[book.toLong()]!!
            assertEquals(list.jsonArray.map { it.jsonObject["tag"]!!.jsonPrimitive.content }, mine.map { it.tag })
            list.jsonArray.zip(mine).forEach { (w, m) ->
                val o = w.jsonObject
                assertEquals(o["name"]!!.jsonPrimitive.content, m.name)
                assertEquals((o["parent"] as? JsonPrimitive)?.takeIf { it !is JsonNull }?.content, m.parent)
                assertEquals(int(o, "due"), m.due); assertEquals(int(o, "new"), m.new); assertEquals(int(o, "total"), m.total)
                assertEquals(long(o, "page"), m.page)
            }
        }
    }

    @Test
    fun previewsAndIntervalsMatch() {
        for ((id, want) in cases["previews"]!!.jsonObject) {
            val got = practice.previews(practice.question(id.toLong())!!, now)
            for ((k, v) in want.jsonObject) assertEquals("question $id rating $k", v.jsonPrimitive.content, got[k.toInt()])
        }
        for ((s, v) in cases["format_interval"]!!.jsonObject) assertEquals(v.jsonPrimitive.content, Practice.formatInterval(s.toDouble()))
    }

    @Test
    fun rewardsStatusMatches() {
        val want = cases["gamify_status"]!!.jsonObject
        val got = Gamify(practice).status(now)
        for (k in listOf("level", "title", "xp", "into", "need", "streak", "freezes", "at_risk", "lapsed", "today", "goal", "goal_met"))
            assertEquals(k, want[k], got[k])
        assertEquals(want["heat"], got["heat"])
        assertEquals(want["achievements"], got["achievements"])
    }

    // -- the practice controller and the sync queue ----------------------------------------------
    @Test
    fun practicingQueuesEventsThatReplayOnTheNextSnapshot() {
        val calls = mutableListOf<String>()
        var t = now
        var mono = 1000.0
        val c = PracticeController(db, { calls += it }, now = { t }, mono = { mono })
        c.refresh()
        assertTrue(calls.last().startsWith("showOverview("))
        c.start("")
        val first = c.questionId!!
        val origReps = practice.question(first)!!.long("reps")
        assertEquals(cases["scopes"]!!.jsonArray[0].jsonObject["next"]!!.jsonPrimitive.long, first)
        val q = practice.content(practice.question(first)!!)
        val type = q["type"]!!.jsonPrimitive.content
        c.check(first, if (type == "choice") "{\"choice\": 1}" else if (type == "truefalse") "{\"value\": false}" else "{}")
        assertTrue(calls.last().contains("\"previews\":{"))
        mono += 7; t += 7
        c.rate(first, 3)
        assertTrue(calls.any { it.startsWith("celebrate(") })
        val second = c.questionId!!
        c.act(second, "flag:4")
        assertEquals(4L, practice.question(second)!!.long("flag"))
        c.act(second, "bury")
        val third = c.questionId!!
        assertTrue(third != second)
        c.act(third, "suspend")
        c.stop()
        assertEquals(1L, practice.question(third)!!.long("suspended"))

        val events = db.query("SELECT json FROM pending ORDER BY seq").map { Json.parseToJsonElement(it.str("json")).jsonObject }
        val types = events.map { it["type"]!!.jsonPrimitive.content }
        assertTrue(types.toString(), types.containsAll(listOf("attempt", "xp", "flag", "bury", "suspend")))
        val attempt = events.first { it["type"]!!.jsonPrimitive.content == "attempt" }
        assertEquals(first, attempt["question"]!!.jsonPrimitive.long)
        assertEquals(7000L, attempt["duration"]!!.jsonPrimitive.long)
        assertTrue(events.none { "card" in it })
        assertEquals("practice", db.one("SELECT source FROM attempts ORDER BY id DESC LIMIT 1")!!.str("source"))

        // The batch file the desktop reads.
        val (name, text) = SyncStore.takeBatch(db, 1234)!!
        val batch = Json.parseToJsonElement(text).jsonObject
        assertEquals(2, batch["format"]!!.jsonPrimitive.intOrNull)
        assertEquals(events.size, batch["events"]!!.jsonArray.size)
        assertEquals(name.removeSuffix(".json"), batch["batch"]!!.jsonPrimitive.content)
        assertEquals(null, SyncStore.takeBatch(db))

        // The same snapshot again, not acknowledging the batch: the phone's changes are replayed on it.
        val attempted = practice.question(first)!!
        val fresh = File.createTempFile("next", ".db")
        javaClass.getResourceAsStream("/queue_snapshot.db")!!.use { input -> fresh.outputStream().use { input.copyTo(it) } }
        JdbcSql(fresh.path).use { next ->
            assertEquals(emptyList<String>(), SyncStore.adopt(next, db))
            val again = Practice(next, ZoneId.of("UTC")).question(first)!!
            assertEquals(attempted.long("reps"), again.long("reps"))
            assertEquals(attempted.long("stage"), again.long("stage"))
            assertEquals(attempted.double("stability"), again.double("stability"), 1e-9)
            assertEquals(4L, Practice(next).question(second)!!.long("flag"))
            assertEquals(1L, next.long("SELECT COUNT(*) FROM buried WHERE question_id=?", second))
            assertEquals(Gamify.State.fromJson(practice.setting(Gamify.STATE_SETTING)).xp,
                Gamify.State.fromJson(Practice(next).setting(Gamify.STATE_SETTING)).xp)
            // Once a snapshot acknowledges the batch, its events are dropped rather than replayed.
            next.exec("INSERT INTO acks VALUES (?,?)", SyncStore.device(db), name.removeSuffix(".json"))
            val fresh2 = File.createTempFile("next2", ".db")
            javaClass.getResourceAsStream("/queue_snapshot.db")!!.use { input -> fresh2.outputStream().use { input.copyTo(it) } }
            JdbcSql(fresh2.path).use { third2 ->
                third2.exec("CREATE TABLE IF NOT EXISTS acks (device TEXT NOT NULL, batch TEXT NOT NULL, PRIMARY KEY (device, batch))")
                third2.exec("INSERT INTO acks VALUES (?,?)", SyncStore.device(db), name.removeSuffix(".json"))
                assertEquals(listOf(name), SyncStore.adopt(third2, next))
                assertEquals(0L, third2.long("SELECT COUNT(*) FROM pending"))
                assertEquals(origReps, Practice(third2).question(first)!!.long("reps"))
            }
            fresh2.delete()
        }
        fresh.delete()
    }

    @Test
    fun answersAreGradedAndBadInputStays() {
        val calls = mutableListOf<String>()
        val c = PracticeController(db, { calls += it }, now = { now })
        // A calculation question, if the fixture made one that's up.
        val calculation = db.query("SELECT id, data FROM questions WHERE data LIKE '%\"calculation\"%' LIMIT 1").firstOrNull() ?: return
        c.questionId = calculation.long("id")
        c.refresh()
        c.check(calculation.long("id"), "{\"latex\": \"\\\\frac{\"}")
        assertTrue(calls.last(), calls.last().startsWith("inputError(") || calls.last().contains("\"verdict\""))
        c.check(calculation.long("id"), "{\"latex\": \"41\"}")
        assertTrue(calls.last().contains("\"verdict\":\"correct\""))
        assertNotNull(calls.last())
    }

    @Test
    fun olderKindNamesReadAsTodays() {
        // Snapshots and variants from older desktops name kinds as they were (and older still, "free"):
        // such a question is self-rated like a short answer.
        val id = db.query("SELECT id FROM questions WHERE data LIKE '%\"short_answer\"%' LIMIT 1").first().long("id")
        for (old in listOf("written", "free")) {
            db.exec("UPDATE questions SET data=replace(data, '\"short_answer\"', ?), variant='' WHERE id=?", "\"$old\"", id)
            assertEquals("short_answer", practice.content(practice.question(id)!!)["type"]!!.jsonPrimitive.content)
            db.exec("UPDATE questions SET data=replace(data, ?, '\"short_answer\"') WHERE id=?", "\"$old\"", id)
        }
        db.exec("UPDATE questions SET data=replace(data, '\"short_answer\"', '\"free\"') WHERE id=?", id)
        val calls = mutableListOf<String>()
        val c = PracticeController(db, { calls += it }, now = { now })
        c.questionId = id
        c.refresh()
        c.check(id, "{\"text\": \"Tea.\"}")
        assertTrue(calls.last(), calls.last().startsWith("showQuestion(") && calls.last().contains("\"previews\""))
        // A variant written before the rename.
        val flash = db.query("SELECT id, data FROM questions WHERE data LIKE '%\"self_check\"%' LIMIT 1").first()
        db.exec("UPDATE questions SET variant=? WHERE id=?", flash.str("data").replace("\"self_check\"", "\"flashcard\""), flash.long("id"))
        assertEquals("self_check", practice.content(practice.question(flash.long("id"))!!)["type"]!!.jsonPrimitive.content)
    }

    // -- older builds and older desktops -----------------------------------------------------------
    @Test
    fun anOlderBuildsLocalTablesAndQueuedEventsAreUpgraded() {
        JdbcSql().use { old ->
            // What a build from before the rename left behind: buried by card_id, format 1 events.
            old.exec("CREATE TABLE pending (seq INTEGER PRIMARY KEY AUTOINCREMENT, id TEXT UNIQUE NOT NULL, batch TEXT, json TEXT NOT NULL)")
            old.exec("CREATE TABLE buried (card_id INTEGER PRIMARY KEY, until REAL NOT NULL)")
            old.exec("CREATE TABLE local (key TEXT PRIMARY KEY, value TEXT NOT NULL)")
            old.exec("INSERT INTO local VALUES ('device', 'phone1')")
            val first = cases["scopes"]!!.jsonArray[0].jsonObject["next"]!!.jsonPrimitive.long
            old.exec("INSERT INTO buried VALUES (?, ?)", 7, now + 3600)
            old.exec("INSERT INTO pending (id, batch, json) VALUES (?,?,?)", "a", "0000000000001-sent",
                """{"id":"a","type":"flag","card":$first,"at":${now - 60},"flag":3}""")
            old.exec("INSERT INTO pending (id, batch, json) VALUES (?,?,?)", "b", null,
                """{"id":"b","type":"review","card":$first,"at":${now - 30},"rating":3,"duration":2000,"response":null,"verdict":null,"new":true}""")
            old.exec("INSERT INTO pending (id, batch, json) VALUES (?,?,?)", "c", null, """{"id":"c","type":"xp","at":${now - 30},"amount":10}""")
            SyncStore.prepare(old)
            SyncStore.prepare(old)  // twice changes nothing
            assertEquals(listOf("question_id", "until"), old.query("PRAGMA table_info(buried)").map { it.str("name") })
            assertEquals(1L, old.long("SELECT COUNT(*) FROM buried WHERE question_id=7"))
            val events = old.query("SELECT json FROM pending ORDER BY seq").map { Json.parseToJsonElement(it.str("json")).jsonObject }
            assertEquals(listOf("flag", "attempt", "xp"), events.map { it["type"]!!.jsonPrimitive.content })
            assertTrue(events.none { "card" in it })
            assertEquals(first, events[1]["question"]!!.jsonPrimitive.long)
            assertEquals(listOf("a", "b", "c"), events.map { it["id"]!!.jsonPrimitive.content })

            // Only the unsent ones go out, in one format 2 batch.
            val (_, text) = SyncStore.takeBatch(old, 1234)!!
            val batch = Json.parseToJsonElement(text).jsonObject
            assertEquals(2, batch["format"]!!.jsonPrimitive.intOrNull)
            assertEquals(listOf("b", "c"), batch["events"]!!.jsonArray.map { it.jsonObject["id"]!!.jsonPrimitive.content })

            // And they replay on the next snapshot.
            val before = practice.question(first)!!
            assertEquals(emptyList<String>(), SyncStore.adopt(db, old))
            val after = practice.question(first)!!
            assertEquals(before.long("reps") + 1, after.long("reps"))
            assertEquals(3L, after.long("flag"))
            assertEquals(1L, db.long("SELECT COUNT(*) FROM buried WHERE question_id=7"))
        }
    }

    @Test
    fun aSnapshotFromAnOlderDesktopIsRefused() {
        assertEquals(null, SyncStore.refusal(db))
        db.exec("DELETE FROM meta WHERE key='schema'")
        assertEquals(SyncStore.TOO_OLD, SyncStore.refusal(db))
        val e = assertThrows(IllegalStateException::class.java) { SyncStore.adopt(db, null) }
        assertEquals(SyncStore.TOO_OLD, e.message)
    }

    @Test
    fun achievementsUnderTheirOldKeysCount() {
        val gamify = Gamify(practice)
        // A state saved before the rename says reviews_100; the next save says attempts_100.
        practice.setSetting(Gamify.STATE_SETTING, practice.setting(Gamify.STATE_SETTING).replace("\"attempts_100\"", "\"reviews_100\""))
        assertTrue("reviews_100" in practice.setting(Gamify.STATE_SETTING))
        assertEquals(cases["gamify_status"]!!.jsonObject["achievements"], gamify.status(now)["achievements"])
        val state = gamify.load(now)
        assertTrue("attempts_100" in state.achievements && "reviews_100" !in state.achievements)
        gamify.applyReward(Json.parseToJsonElement("""{"type":"achievement","key":"reviews_1000","at":$now}""").jsonObject, now)
        assertTrue("attempts_1000" in gamify.load(now).achievements)
        assertTrue("reviews_" !in practice.setting(Gamify.STATE_SETTING))
    }
}
