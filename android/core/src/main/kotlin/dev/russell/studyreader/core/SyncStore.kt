package dev.russell.studyreader.core

import kotlinx.serialization.json.Json
import kotlinx.serialization.json.JsonObject
import kotlinx.serialization.json.JsonPrimitive
import kotlinx.serialization.json.buildJsonObject
import kotlinx.serialization.json.contentOrNull
import kotlinx.serialization.json.doubleOrNull
import kotlinx.serialization.json.intOrNull
import kotlinx.serialization.json.jsonObject
import kotlinx.serialization.json.jsonPrimitive
import kotlinx.serialization.json.longOrNull
import kotlinx.serialization.json.put
import java.util.UUID

/**
 * The phone's side of the sync format (the desktop's phone_sync.py describes it in full).
 *
 * The phone works on a copy of the desktop's snapshot, plus local tables: `pending` (events not
 * yet acknowledged by a snapshot, with the batch file each went out in), `buried`, and `local`
 * (device id, the snapshot in use). When a new snapshot arrives, [adopt] moves the local tables
 * over, drops the events it acknowledges, and replays the rest on top of it.
 */
object SyncStore {
    const val FORMAT = 1

    /** The desktop's snapshot schema (phone_sync.SNAPSHOT_SCHEMA), for an empty start. */
    val SNAPSHOT_SCHEMA = listOf(
        "CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT NOT NULL)",
        "CREATE TABLE IF NOT EXISTS decks (id INTEGER PRIMARY KEY, name TEXT NOT NULL)",
        "CREATE TABLE IF NOT EXISTS books (id INTEGER PRIMARY KEY, title TEXT NOT NULL, root TEXT NOT NULL, deck_id INTEGER)",
        "CREATE TABLE IF NOT EXISTS section_starts (book_id INTEGER NOT NULL, tag TEXT NOT NULL, page INTEGER NOT NULL)",
        "CREATE TABLE IF NOT EXISTS card_notes (id INTEGER PRIMARY KEY, kind TEXT NOT NULL, tags TEXT NOT NULL)",
        "CREATE TABLE IF NOT EXISTS cards (id INTEGER PRIMARY KEY, note_id INTEGER NOT NULL, ord INTEGER NOT NULL, " +
            "deck_id INTEGER NOT NULL, book_id INTEGER, message_id INTEGER, quiz INTEGER, question INTEGER, title TEXT NOT NULL, " +
            "data TEXT NOT NULL, variant TEXT NOT NULL, page INTEGER, stage INTEGER NOT NULL, fsrs TEXT NOT NULL, due REAL NOT NULL, " +
            "reps INTEGER NOT NULL, lapses INTEGER NOT NULL, suspended INTEGER NOT NULL, flag INTEGER NOT NULL, stability REAL, " +
            "difficulty REAL, last_review REAL, created_at REAL NOT NULL, updated_at REAL NOT NULL)",
        "CREATE TABLE IF NOT EXISTS reviews (id INTEGER PRIMARY KEY, card_id INTEGER NOT NULL, rating INTEGER NOT NULL, " +
            "source TEXT NOT NULL, reviewed_at REAL NOT NULL, duration INTEGER NOT NULL, was_new INTEGER NOT NULL)",
        "CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT NOT NULL)",
        "CREATE TABLE IF NOT EXISTS acks (device TEXT NOT NULL, batch TEXT NOT NULL, PRIMARY KEY (device, batch))",
    )

    val LOCAL_SCHEMA = listOf(
        "CREATE TABLE IF NOT EXISTS pending (seq INTEGER PRIMARY KEY AUTOINCREMENT, id TEXT UNIQUE NOT NULL, " +
            "batch TEXT, json TEXT NOT NULL)",
        "CREATE TABLE IF NOT EXISTS buried (card_id INTEGER PRIMARY KEY, until REAL NOT NULL)",
        "CREATE TABLE IF NOT EXISTS local (key TEXT PRIMARY KEY, value TEXT NOT NULL)",
    )

    /** Makes sure a database (a fresh snapshot copy, or an empty one) has every table the app uses. */
    fun prepare(db: Sql) {
        for (s in SNAPSHOT_SCHEMA + LOCAL_SCHEMA) db.exec(s)
    }

    fun local(db: Sql, key: String): String? = db.one("SELECT value FROM local WHERE key=?", key)?.str("value")
    fun setLocal(db: Sql, key: String, value: String) =
        db.exec("INSERT INTO local (key, value) VALUES (?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value", key, value)

    fun meta(db: Sql, key: String): String? = db.one("SELECT value FROM meta WHERE key=?", key)?.str("value")

    /** This phone's id (made the first time), its folder name under phones/. */
    fun device(db: Sql): String = local(db, "device") ?: UUID.randomUUID().toString().take(12).also { setLocal(db, "device", it) }

    /** Queues an event for the desktop. */
    fun record(db: Sql, event: JsonObject) {
        val id = UUID.randomUUID().toString()
        val full = buildJsonObject { put("id", id); event.forEach { (k, v) -> put(k, v) } }
        db.exec("INSERT INTO pending (id, json) VALUES (?,?)", id, full.toString())
    }

    fun unsent(db: Sql): Int = db.long("SELECT COUNT(*) FROM pending WHERE batch IS NULL").toInt()
    fun unacknowledged(db: Sql): Int = db.long("SELECT COUNT(*) FROM pending").toInt()

    /** Puts the events not yet sent into a batch: its file name and contents, or null if there are none. */
    fun takeBatch(db: Sql, nowMillis: Long = System.currentTimeMillis()): Pair<String, String>? = db.transaction {
        val rows = db.query("SELECT seq, json FROM pending WHERE batch IS NULL ORDER BY seq")
        if (rows.isEmpty()) return@transaction null
        val device = device(db)
        val batch = "%013d-%s".format(nowMillis, UUID.randomUUID().toString().take(6))
        db.exec("UPDATE pending SET batch=? WHERE batch IS NULL AND seq<=?", batch, rows.last().long("seq"))
        val text = buildString {
            append("{\"format\":$FORMAT,\"device\":").append(JsonPrimitive(device)).append(",\"batch\":").append(JsonPrimitive(batch))
            append(",\"events\":[")
            rows.forEachIndexed { i, r -> if (i > 0) append(','); append(r.str("json")) }
            append("]}")
        }
        "$batch.json" to text
    }

    /** The batch couldn't be written: its events go out with the next one. */
    fun unbatch(db: Sql, fileName: String) = db.exec("UPDATE pending SET batch=NULL WHERE batch=?", fileName.removeSuffix(".json"))

    /**
     * Takes over the local state of the database the phone used so far (old, if any) into a new
     * snapshot copy, drops the events the snapshot acknowledges and replays the others on it.
     * Returns the names of the acknowledged batch files, which the phone can now delete.
     */
    fun adopt(new: Sql, old: Sql?): List<String> {
        prepare(new)
        return new.transaction {
            if (old != null) {
                for (r in old.query("SELECT key, value FROM local"))
                    new.exec("INSERT OR REPLACE INTO local (key, value) VALUES (?,?)", r["key"], r["value"])
                for (r in old.query("SELECT card_id, until FROM buried"))
                    new.exec("INSERT OR REPLACE INTO buried (card_id, until) VALUES (?,?)", r["card_id"], r["until"])
                for (r in old.query("SELECT seq, id, batch, json FROM pending ORDER BY seq"))
                    new.exec("INSERT OR IGNORE INTO pending (seq, id, batch, json) VALUES (?,?,?,?)", r["seq"], r["id"], r["batch"], r["json"])
            }
            val device = device(new)
            val acked = new.query("SELECT batch FROM acks WHERE device=?", device).map { it.str("batch") }.toSet()
            val done = new.query("SELECT DISTINCT batch FROM pending WHERE batch IS NOT NULL").map { it.str("batch") }.filter { it in acked }
            for (b in done) new.exec("DELETE FROM pending WHERE batch=?", b)
            val srs = Srs(new)
            val gamify = Gamify(srs)
            for (r in new.query("SELECT json FROM pending ORDER BY seq")) {
                try {
                    replay(srs, gamify, Json.parseToJsonElement(r.str("json")).jsonObject)
                } catch (_: Exception) {
                    // an event that no longer applies (its card is gone): the desktop skips it too
                }
            }
            setLocal(new, "snapshot", meta(new, "id") ?: "")
            acked.map { "$it.json" }
        }
    }

    /** Applies a queued event to a snapshot that doesn't reflect it yet. */
    fun replay(srs: Srs, gamify: Gamify, e: JsonObject) {
        val type = e["type"]!!.jsonPrimitive.content
        val at = e["at"]!!.jsonPrimitive.doubleOrNull!!
        val cardId = (e["card"] as? JsonPrimitive)?.longOrNull
        when (type) {
            "review" -> {
                val rating = e["rating"]!!.jsonPrimitive.intOrNull!!
                val card = srs.card(cardId!!) ?: return
                val last = card.doubleOrNull("last_review")
                srs.review(cardId, rating, if (last != null && at < last) last else at,
                    e["response"]?.takeIf { it is JsonObject }, (e["verdict"] as? JsonPrimitive)?.contentOrNull,
                    (e["duration"] as? JsonPrimitive)?.longOrNull ?: 0, (e["new"] as? JsonPrimitive)?.contentOrNull == "true")
            }
            "suspend" -> srs.update(cardId!!, mapOf("suspended" to ((e["suspended"] as? JsonPrimitive)?.intOrNull ?: 1).toLong()))
            "flag" -> srs.update(cardId!!, mapOf("flag" to ((e["flag"] as? JsonPrimitive)?.intOrNull ?: 0).coerceIn(0, 7).toLong()))
            "bury" -> {}  // the buried table came over with the rest of the local state
            "xp", "achievement", "goal", "freeze_spent", "freeze_earned" -> gamify.applyReward(e, at)
        }
    }

    // -- events -------------------------------------------------------------------------------
    fun reviewEvent(cardId: Long, at: Double, rating: Int, durationMs: Long, response: JsonObject?, verdict: String?, new: Boolean) =
        buildJsonObject {
            put("type", "review"); put("card", cardId); put("at", at); put("rating", rating); put("duration", durationMs)
            put("response", response ?: kotlinx.serialization.json.JsonNull); put("verdict", verdict); put("new", new)
        }

    fun cardEvent(type: String, cardId: Long, at: Double, more: kotlinx.serialization.json.JsonObjectBuilder.() -> Unit = {}) =
        buildJsonObject { put("type", type); put("card", cardId); put("at", at); more() }
}
