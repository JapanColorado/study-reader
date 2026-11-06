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
import org.junit.Assert.assertTrue
import org.junit.Before
import org.junit.Test
import java.io.File
import java.time.ZoneId

/**
 * The queue, counts, sections, previews and rewards against what the desktop's srs.py and
 * gamify.py say about the same cards (scripts/queue_fixtures.py), over a snapshot the desktop's
 * phone_sync.export_snapshot wrote.
 */
class QueueTest {
    private val cases = Json.parseToJsonElement(javaClass.getResource("/queue_cases.json")!!.readText()).jsonObject
    private val now = cases["now"]!!.jsonPrimitive.doubleOrNull!!
    private lateinit var file: File
    private lateinit var db: JdbcSql
    private lateinit var srs: Srs

    @Before
    fun open() {
        file = File.createTempFile("snapshot", ".db")
        javaClass.getResourceAsStream("/queue_snapshot.db")!!.use { input -> file.outputStream().use { input.copyTo(it) } }
        db = JdbcSql(file.path)
        SyncStore.prepare(db)
        srs = Srs(db, ZoneId.of("UTC"), fuzz = false)
    }

    @After
    fun close() { db.close(); file.delete() }

    private fun long(o: JsonObject, k: String) = (o[k] as? JsonPrimitive)?.longOrNull
    private fun int(o: JsonObject, k: String) = o[k]!!.jsonPrimitive.intOrNull!!

    @Test
    fun countsAndNextCardMatchTheDesktop() {
        assertEquals(int(cases, "new_left_today"), srs.newLeftToday(now))
        for (e in cases["scopes"]!!.jsonArray) {
            val sc = e.jsonObject
            val book = long(sc, "book"); val deck = long(sc, "deck"); val tag = (sc["tag"] as? JsonPrimitive)?.takeIf { it !is JsonNull }?.content
            val label = "scope $sc"
            val want = sc["counts"]!!.jsonObject
            val got = srs.counts(book, now, deck, tag)
            for (k in listOf("due", "new", "new_total", "total", "suspended")) assertEquals("$label $k", int(want, k), got[k])
            val first = srs.nextCard(book, now, emptyList(), deck, tag)
            assertEquals(label, long(sc, "next"), first?.long("id"))
            val second = srs.nextCard(book, now, listOfNotNull(first?.long("id")), deck, tag)
            assertEquals(label, long(sc, "next_skipping"), second?.long("id"))
            // Excluded (skipped this session): never shown, even with nothing else left.
            val excluding = srs.nextCard(book, now, emptyList(), deck, tag, listOfNotNull(first?.long("id")))
            val wantExcluding = if (first == null || long(sc, "next_skipping") == first.long("id")) null else long(sc, "next_skipping")
            assertEquals(label, wantExcluding, excluding?.long("id"))
            assertEquals(label, int(want, "due") + int(want, "new"), srs.leftToReview(book, now, deck, tag))
            if (first != null) {
                val was = if (first.long("stage") == 0L) "new" else "due"
                val capped = was == "new" && int(want, "new") < int(want, "new_total")
                assertEquals(label, int(want, "due") + int(want, "new") - (if (capped) 0 else 1),
                    srs.leftToReview(book, now, deck, tag, listOf(first.long("id"))))
            }
            assertEquals(label, (sc["next_due"] as? JsonPrimitive)?.doubleOrNull, srs.nextDue(book, now, deck, tag))
        }
    }

    @Test
    fun deckCountsMatch() {
        val got = srs.deckCounts(now)
        for ((id, want) in cases["deck_counts"]!!.jsonObject) {
            for (k in listOf("due", "new", "total")) assertEquals("deck $id $k", int(want.jsonObject, k), got[id.toLong()]!![k])
        }
    }

    @Test
    fun sectionsMatch() {
        val got = srs.sections(null, now)
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
            val got = srs.previews(srs.card(id.toLong())!!, now)
            for ((k, v) in want.jsonObject) assertEquals("card $id rating $k", v.jsonPrimitive.content, got[k.toInt()])
        }
        for ((s, v) in cases["format_interval"]!!.jsonObject) assertEquals(v.jsonPrimitive.content, Srs.formatInterval(s.toDouble()))
    }

    @Test
    fun rewardsStatusMatches() {
        val want = cases["gamify_status"]!!.jsonObject
        val got = Gamify(srs).status(now)
        for (k in listOf("level", "title", "xp", "into", "need", "streak", "freezes", "at_risk", "lapsed", "today", "goal", "goal_met"))
            assertEquals(k, want[k], got[k])
        assertEquals(want["heat"], got["heat"])
        assertEquals(want["achievements"], got["achievements"])
    }

    // -- the review controller and the sync queue ------------------------------------------------
    @Test
    fun reviewingQueuesEventsThatReplayOnTheNextSnapshot() {
        val calls = mutableListOf<String>()
        var t = now
        var mono = 1000.0
        val c = ReviewController(db, { calls += it }, now = { t }, mono = { mono })
        c.refresh()
        assertTrue(calls.last().startsWith("showOverview("))
        c.start("")
        val first = c.cardId!!
        val origReps = srs.card(first)!!.long("reps")
        assertEquals(cases["scopes"]!!.jsonArray[0].jsonObject["next"]!!.jsonPrimitive.long, first)
        val q = srs.question(srs.card(first)!!)
        val type = q["type"]!!.jsonPrimitive.content
        c.check(first, if (type == "choice") "{\"choice\": 1}" else if (type == "truefalse") "{\"value\": false}" else "{}")
        assertTrue(calls.last().contains("\"previews\":{"))
        mono += 7; t += 7
        c.rate(first, 3)
        assertTrue(calls.any { it.startsWith("celebrate(") })
        val second = c.cardId!!
        c.act(second, "flag:4")
        assertEquals(4L, srs.card(second)!!.long("flag"))
        c.act(second, "bury")
        val third = c.cardId!!
        assertTrue(third != second)
        c.act(third, "suspend")
        c.stop()
        assertEquals(1L, srs.card(third)!!.long("suspended"))

        val events = db.query("SELECT json FROM pending ORDER BY seq").map { Json.parseToJsonElement(it.str("json")).jsonObject }
        val types = events.map { it["type"]!!.jsonPrimitive.content }
        assertTrue(types.toString(), types.containsAll(listOf("review", "xp", "flag", "bury", "suspend")))
        val review = events.first { it["type"]!!.jsonPrimitive.content == "review" }
        assertEquals(first, review["card"]!!.jsonPrimitive.long)
        assertEquals(7000L, review["duration"]!!.jsonPrimitive.long)

        // The batch file the desktop reads.
        val (name, text) = SyncStore.takeBatch(db, 1234)!!
        val batch = Json.parseToJsonElement(text).jsonObject
        assertEquals(1, batch["format"]!!.jsonPrimitive.intOrNull)
        assertEquals(events.size, batch["events"]!!.jsonArray.size)
        assertEquals(name.removeSuffix(".json"), batch["batch"]!!.jsonPrimitive.content)
        assertEquals(null, SyncStore.takeBatch(db))

        // The same snapshot again, not acknowledging the batch: the phone's changes are replayed on it.
        val reviewed = srs.card(first)!!
        val fresh = File.createTempFile("next", ".db")
        javaClass.getResourceAsStream("/queue_snapshot.db")!!.use { input -> fresh.outputStream().use { input.copyTo(it) } }
        JdbcSql(fresh.path).use { next ->
            assertEquals(emptyList<String>(), SyncStore.adopt(next, db))
            val again = Srs(next, ZoneId.of("UTC")).card(first)!!
            assertEquals(reviewed.long("reps"), again.long("reps"))
            assertEquals(reviewed.long("stage"), again.long("stage"))
            assertEquals(reviewed.double("stability"), again.double("stability"), 1e-9)
            assertEquals(4L, Srs(next).card(second)!!.long("flag"))
            assertEquals(1L, next.long("SELECT COUNT(*) FROM buried WHERE card_id=?", second))
            assertEquals(Gamify.State.fromJson(srs.setting(Gamify.STATE_SETTING)).xp,
                Gamify.State.fromJson(Srs(next).setting(Gamify.STATE_SETTING)).xp)
            // Once a snapshot acknowledges the batch, its events are dropped rather than replayed.
            next.exec("INSERT INTO acks VALUES (?,?)", SyncStore.device(db), name.removeSuffix(".json"))
            val fresh2 = File.createTempFile("next2", ".db")
            javaClass.getResourceAsStream("/queue_snapshot.db")!!.use { input -> fresh2.outputStream().use { input.copyTo(it) } }
            JdbcSql(fresh2.path).use { third2 ->
                third2.exec("CREATE TABLE IF NOT EXISTS acks (device TEXT NOT NULL, batch TEXT NOT NULL, PRIMARY KEY (device, batch))")
                third2.exec("INSERT INTO acks VALUES (?,?)", SyncStore.device(db), name.removeSuffix(".json"))
                assertEquals(listOf(name), SyncStore.adopt(third2, next))
                assertEquals(0L, third2.long("SELECT COUNT(*) FROM pending"))
                assertEquals(origReps, Srs(third2).card(first)!!.long("reps"))
            }
            fresh2.delete()
        }
        fresh.delete()
    }

    @Test
    fun answersAreGradedAndBadInputStays() {
        val calls = mutableListOf<String>()
        val c = ReviewController(db, { calls += it }, now = { now })
        // A numeric card, if the fixture made one that's up.
        val numeric = db.query("SELECT id, data FROM cards WHERE data LIKE '%\"numeric\"%' LIMIT 1").firstOrNull() ?: return
        c.cardId = numeric.long("id")
        c.refresh()
        c.check(numeric.long("id"), "{\"latex\": \"\\\\frac{\"}")
        assertTrue(calls.last(), calls.last().startsWith("inputError(") || calls.last().contains("\"verdict\""))
        c.check(numeric.long("id"), "{\"latex\": \"41\"}")
        assertTrue(calls.last().contains("\"verdict\":\"correct\""))
        assertNotNull(calls.last())
    }
}
