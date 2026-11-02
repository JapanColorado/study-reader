package dev.russell.studyreader.core

import kotlinx.serialization.json.Json
import kotlinx.serialization.json.JsonArray
import kotlinx.serialization.json.JsonObject
import kotlinx.serialization.json.JsonPrimitive
import kotlinx.serialization.json.buildJsonArray
import kotlinx.serialization.json.buildJsonObject
import kotlinx.serialization.json.contentOrNull
import kotlinx.serialization.json.doubleOrNull
import kotlinx.serialization.json.intOrNull
import kotlinx.serialization.json.jsonArray
import kotlinx.serialization.json.jsonObject
import kotlinx.serialization.json.jsonPrimitive
import kotlinx.serialization.json.put
import java.time.LocalDate
import java.time.ZonedDateTime
import kotlin.math.max
import kotlin.math.min

/**
 * The review rewards of the desktop's gamify.py: XP and levels, the combo multiplier, the daily
 * goal, streaks and streak freezes, achievements, sessions. The state is the same JSON setting
 * (gamify_state). Whatever changes it is also handed to [record] as a sync event, so the desktop
 * can merge it (phone_sync._apply_reward).
 */
class Gamify(val srs: Srs, val record: (JsonObject) -> Unit = {}) {
    companion object {
        const val ENABLED_SETTING = "gamify"
        const val GOAL_SETTING = "daily_goal"
        const val STATE_SETTING = "gamify_state"
        const val DEFAULT_GOAL = 20
        const val BASE_XP = 10
        const val NEW_XP = 5
        const val COMBO_STEP = 5
        const val MAX_MULTIPLIER = 2.0
        const val COMBO_PAUSE = 120.0
        const val MILESTONE_EVERY = 25
        const val QUICK_CARDS = 5
        const val QUICK_SECONDS = 5 * 60
        const val FREEZE_EVERY = 7
        const val MAX_FREEZES = 2
        const val START_FREEZES = 1
        const val COMEBACK_DAYS = 3
        const val HEAT_WEEKS = 18
        const val MATURE_DAYS = 21

        val LEVEL_TITLES = listOf(1 to "Novice", 5 to "Apprentice", 10 to "Student", 20 to "Scholar", 35 to "Sage",
            50 to "Polymath", 75 to "Luminary", 100 to "Grandmaster")

        data class Achievement(val id: String, val icon: String, val name: String, val desc: String, val value: String, val threshold: Int)

        val ACHIEVEMENTS = listOf(
            Achievement("first", "🌱", "First steps", "Review your first card", "reviews", 1),
            Achievement("reviews_100", "💯", "Centurion", "Review 100 cards", "reviews", 100),
            Achievement("reviews_1000", "📚", "Bookworm", "Review 1,000 cards", "reviews", 1000),
            Achievement("reviews_10000", "🏛️", "Library of Alexandria", "Review 10,000 cards", "reviews", 10000),
            Achievement("streak_3", "🔥", "Warming up", "Reach a 3-day streak", "streak", 3),
            Achievement("streak_7", "🗓️", "A week of it", "Reach a 7-day streak", "streak", 7),
            Achievement("streak_30", "🌙", "Month of memory", "Reach a 30-day streak", "streak", 30),
            Achievement("streak_100", "💎", "Centennial", "Reach a 100-day streak", "streak", 100),
            Achievement("streak_365", "👑", "Year of learning", "Reach a 365-day streak", "streak", 365),
            Achievement("goal_1", "🎯", "On target", "Meet your daily goal", "goals", 1),
            Achievement("goal_30", "🏹", "Sharpshooter", "Meet your daily goal on 30 days", "goals", 30),
            Achievement("combo_25", "⚡", "In the flow", "Review 25 cards in a row without a long pause", "combo", 25),
            Achievement("combo_100", "🌊", "Deep focus", "Review 100 cards in a row without a long pause", "combo", 100),
            Achievement("marathon", "🏃", "Marathon", "Review 100 cards in one session", "session", 100),
            Achievement("caught_up", "🧹", "Inbox zero", "Review every card that's due", "caught_up", 1),
            Achievement("quick", "🍪", "Snack-sized", "Finish a quick session", "quick", 1),
            Achievement("mature_1", "🌳", "Taking root", "Get a card to a 3-week interval", "mature", 1),
            Achievement("mature_100", "🌲", "Forest", "Have 100 cards at 3-week intervals or longer", "mature", 100),
            Achievement("level_10", "⭐", "Rising star", "Reach level 10", "level", 10),
            Achievement("level_25", "🌟", "Shining", "Reach level 25", "level", 25),
            Achievement("night_owl", "🦉", "Night owl", "Review after midnight", "night_owl", 1),
            Achievement("early_bird", "🐦", "Early bird", "Review before 7 am", "early_bird", 1),
            Achievement("comeback", "🤝", "Welcome back", "Come back after $COMEBACK_DAYS or more days away", "comeback", 1),
        )

        fun levelXp(level: Int): Int = 100 * (level - 1) + 25 * (level - 1) * (level - 2)

        fun level(xp: Int): Map<String, Any> {
            var n = 1
            while (levelXp(n + 1) <= xp) n++
            val title = LEVEL_TITLES.filter { it.first <= n }.last().second
            return mapOf("level" to n, "title" to title, "xp" to xp, "into" to xp - levelXp(n), "need" to levelXp(n + 1) - levelXp(n))
        }

        fun multiplier(combo: Int): Double = min(1 + (combo / COMBO_STEP) / 10.0, MAX_MULTIPLIER)

        fun interval(card: Row): Double {
            if (card.long("stage") != 2L || card["last_review"] == null) return 0.0
            return (card.double("due") - card.double("last_review")) / 86400
        }
    }

    /** gamify.py's state dict. */
    data class State(
        var xp: Int = 0, var freezes: Int = START_FREEZES, val frozen: MutableList<String> = mutableListOf(),
        var freezeEarned: String = "", var goals: Int = 0, var goalDay: String = "",
        val achievements: LinkedHashMap<String, Double> = LinkedHashMap(), val extra: Map<String, kotlinx.serialization.json.JsonElement> = emptyMap(),
    ) {
        fun toJson(): String = buildJsonObject {
            extra.forEach { (k, v) -> put(k, v) }
            put("xp", xp); put("freezes", freezes)
            put("frozen", buildJsonArray { frozen.forEach { add(JsonPrimitive(it)) } })
            put("freeze_earned", freezeEarned); put("goals", goals); put("goal_day", goalDay)
            put("achievements", buildJsonObject { achievements.forEach { (k, v) -> put(k, v) } })
        }.toString()

        companion object {
            private val KNOWN = setOf("xp", "freezes", "frozen", "freeze_earned", "goals", "goal_day", "achievements")
            fun fromJson(raw: String): State {
                val o = Json.parseToJsonElement(raw).jsonObject
                fun int(k: String, d: Int) = (o[k] as? JsonPrimitive)?.let { it.intOrNull ?: it.doubleOrNull?.toInt() } ?: d
                fun str(k: String) = (o[k] as? JsonPrimitive)?.contentOrNull ?: ""
                return State(
                    xp = int("xp", 0), freezes = int("freezes", START_FREEZES),
                    frozen = (o["frozen"] as? JsonArray)?.map { it.jsonPrimitive.content }?.toMutableList() ?: mutableListOf(),
                    freezeEarned = str("freeze_earned"), goals = int("goals", 0), goalDay = str("goal_day"),
                    achievements = LinkedHashMap((o["achievements"] as? JsonObject)?.mapValues { it.value.jsonPrimitive.doubleOrNull ?: 0.0 } ?: emptyMap()),
                    extra = o.filterKeys { it !in KNOWN },
                )
            }
        }
    }

    val db get() = srs.db

    fun enabled(): Boolean = srs.setting(ENABLED_SETTING, "1") != "0"
    fun dailyGoal(): Int = srs.setting(GOAL_SETTING, "$DEFAULT_GOAL").toIntOrNull()?.let { max(it, 1) } ?: DEFAULT_GOAL

    fun load(now: Double, logged: Boolean = false): State {
        val raw = srs.setting(STATE_SETTING)
        if (raw.isNotBlank()) try { return State.fromJson(raw) } catch (_: Exception) {}
        val state = State()
        val maxId = db.long("SELECT MAX(id) FROM reviews")
        val row = db.one("SELECT COUNT(*) AS n, COALESCE(SUM(was_new), 0) AS new FROM reviews WHERE id<?", maxId + if (logged) 0 else 1)!!
        val n = row.long("n").toInt()
        state.xp = BASE_XP * n + NEW_XP * row.long("new").toInt()
        unlock(state, mapOf("reviews" to n, "streak" to longest(reviewDays()), "level" to level(state.xp)["level"] as Int, "mature" to mature()), now, quiet = true)
        save(state)
        return state
    }

    fun save(state: State) = srs.setSetting(STATE_SETTING, state.toJson())

    /** Reviews per review day (days roll over at 4 am). */
    fun reviewDays(): Map<LocalDate, Int> =
        db.query("SELECT date(reviewed_at, 'unixepoch', 'localtime', ?) AS d, COUNT(*) AS n FROM reviews GROUP BY d",
            "-${Srs.DAY_STARTS_AT_HOUR} hours").associate { LocalDate.parse(it.str("d")) to it.long("n").toInt() }

    fun mature(): Int = db.long("SELECT COUNT(*) FROM cards WHERE stage=2 AND last_review IS NOT NULL AND due-last_review>=?",
        MATURE_DAYS * 86400.0).toInt()

    private fun frozen(state: State) = state.frozen.map { LocalDate.parse(it) }.toSet()

    fun streak(days: Map<LocalDate, Int>, frozen: Set<LocalDate>, today: LocalDate): Int {
        val covered = days.keys + frozen
        var d = if (today in days) today else today.minusDays(1)
        var n = 0
        while (d in covered) {
            if (d in days) n++
            d = d.minusDays(1)
        }
        return n
    }

    private fun longest(days: Map<LocalDate, Int>): Int {
        var longest = 0; var run = 0; var last: LocalDate? = null
        for (d in days.keys.sorted()) {
            run = if (last != null && last.plusDays(1) == d) run + 1 else 1
            longest = max(longest, run); last = d
        }
        return longest
    }

    /** Spends streak freezes on the days missed just before today, if there are enough. */
    private fun settle(state: State, days: Map<LocalDate, Int>, today: LocalDate): Boolean {
        val covered = days.keys + frozen(state)
        val missed = mutableListOf<LocalDate>()
        var d = today.minusDays(1)
        while (d !in covered && missed.size <= state.freezes) { missed += d; d = d.minusDays(1) }
        if (missed.isEmpty() || missed.size > state.freezes) return false
        state.freezes -= missed.size
        for (m in missed) {
            state.frozen += m.toString()
            record(event("freeze_spent") { put("day", m.toString()) })
        }
        return true
    }

    private fun event(type: String, at: Double = System.currentTimeMillis() / 1000.0, more: kotlinx.serialization.json.JsonObjectBuilder.() -> Unit = {}) =
        buildJsonObject { put("type", type); put("at", at); more() }

    private fun unlock(state: State, ctx: Map<String, Int>, now: Double, quiet: Boolean = false): List<JsonObject> {
        val events = mutableListOf<JsonObject>()
        for (a in ACHIEVEMENTS) {
            if (a.id !in state.achievements && (ctx[a.value] ?: 0) >= a.threshold) {
                state.achievements[a.id] = now
                if (!quiet) record(event("achievement", now) { put("key", a.id) })
                events += buildJsonObject { put("kind", "achievement"); put("id", a.id); put("icon", a.icon); put("name", a.name); put("desc", a.desc) }
            }
        }
        return events
    }

    private fun goalMet(state: State, today: LocalDate, now: Double): List<JsonObject> {
        if (state.goalDay == today.toString()) return emptyList()
        state.goals += 1
        state.goalDay = today.toString()
        record(event("goal", now) { put("day", today.toString()) })
        return listOf(buildJsonObject { put("kind", "goal") })
    }

    /** After a review was logged: its XP and what it earned, as events for the page. */
    fun reviewed(new: Boolean, comboMultiplier: Double, now: Double, combo: Int, session: Int): List<JsonObject> {
        val state = load(now, logged = true)
        val days = reviewDays()
        val today = srs.dayOf(now)
        settle(state, days, today)
        val events = mutableListOf<JsonObject>()
        val before = level(state.xp)["level"] as Int
        val gained = Srs.pyRound((BASE_XP + NEW_XP * (if (new) 1 else 0)) * comboMultiplier).toInt()
        state.xp += gained
        record(event("xp", now) { put("amount", gained) })
        events += buildJsonObject { put("kind", "xp"); put("xp", gained) }
        val after = level(state.xp)
        if (after["level"] as Int > before) events += levelEvent(after)

        val current = streak(days, frozen(state), today)
        if (current > 0 && current % FREEZE_EVERY == 0 && state.freezeEarned != today.toString() && state.freezes < MAX_FREEZES) {
            state.freezes += 1
            state.freezeEarned = today.toString()
            record(event("freeze_earned", now) { put("day", today.toString()) })
            events += buildJsonObject { put("kind", "freeze"); put("freezes", state.freezes) }
        }
        if ((days[today] ?: 0) >= dailyGoal()) events += goalMet(state, today, now)

        val earlier = days.keys.filter { it < today }
        val comeback = (days[today] ?: 0) == 1 && earlier.isNotEmpty() &&
            java.time.temporal.ChronoUnit.DAYS.between(earlier.max(), today) >= COMEBACK_DAYS
        if (comeback) events += buildJsonObject { put("kind", "welcome"); put("days", java.time.temporal.ChronoUnit.DAYS.between(earlier.max(), today)) }
        val hour = ZonedDateTime.ofInstant(java.time.Instant.ofEpochMilli((now * 1000).toLong()), srs.zone).hour
        events += unlock(state, mapOf(
            "combo" to combo, "session" to session, "reviews" to days.values.sum(), "streak" to current,
            "level" to after["level"] as Int, "mature" to mature(), "goals" to state.goals, "comeback" to if (comeback) 1 else 0,
            "night_owl" to if (hour < Srs.DAY_STARTS_AT_HOUR) 1 else 0,
            "early_bird" to if (hour >= Srs.DAY_STARTS_AT_HOUR && hour < 7) 1 else 0,
        ), now)
        save(state)
        return events
    }

    private fun levelEvent(l: Map<String, Any>) = buildJsonObject {
        put("kind", "level"); put("level", l["level"] as Int); put("title", l["title"] as String)
        put("xp", l["xp"] as Int); put("into", l["into"] as Int); put("need", l["need"] as Int)
    }

    /** A session ended: everything reviewed (which meets the goal too), or a quick session finished. */
    fun finished(caughtUp: Boolean, quick: Boolean, now: Double): List<JsonObject> {
        val state = load(now)
        val events = mutableListOf<JsonObject>()
        val today = srs.dayOf(now)
        if (caughtUp && (reviewDays()[today] ?: 0) > 0) events += goalMet(state, today, now)
        events += unlock(state, mapOf("caught_up" to if (caughtUp) 1 else 0, "quick" to if (quick) 1 else 0, "goals" to state.goals), now)
        save(state)
        return events
    }

    /** For the overview: level, streak, today against the goal, the calendar, achievements. */
    fun status(now: Double): JsonObject {
        val state = load(now)
        val days = reviewDays()
        val today = srs.dayOf(now)
        if (settle(state, days, today)) save(state)
        val current = streak(days, frozen(state), today)
        val goal = dailyGoal()
        val start = today.minusDays(HEAT_WEEKS * 7L - 1)
        val l = level(state.xp)
        return buildJsonObject {
            put("level", l["level"] as Int); put("title", l["title"] as String); put("xp", l["xp"] as Int)
            put("into", l["into"] as Int); put("need", l["need"] as Int)
            put("streak", current); put("freezes", state.freezes); put("freeze_every", FREEZE_EVERY)
            put("at_risk", current > 0 && today !in days)
            put("lapsed", current == 0 && days.isNotEmpty())
            put("today", days[today] ?: 0); put("goal", goal)
            put("goal_met", state.goalDay == today.toString() || (days[today] ?: 0) >= goal)
            put("heat", buildJsonObject {
                put("start", start.toString()); put("end", today.toString())
                put("days", buildJsonArray {
                    days.entries.sortedBy { it.key }.filter { it.key >= start }.forEach { (d, n) ->
                        add(buildJsonObject { put("date", d.toString()); put("count", n) })
                    }
                })
                put("frozen", buildJsonArray { state.frozen.filter { it >= start.toString() }.forEach { add(JsonPrimitive(it)) } })
            })
            put("achievements", buildJsonArray {
                ACHIEVEMENTS.forEach { a ->
                    add(buildJsonObject {
                        put("id", a.id); put("icon", a.icon); put("name", a.name); put("desc", a.desc)
                        put("unlocked", a.id in state.achievements)
                    })
                }
            })
        }
    }

    /** Applies a reward event from the sync queue to the stored state: phone_sync._apply_reward. */
    fun applyReward(e: JsonObject, at: Double) {
        val state = load(at)
        val type = e["type"]!!.jsonPrimitive.content
        when (type) {
            "xp" -> state.xp += max(e["amount"]?.jsonPrimitive?.intOrNull ?: 0, 0)
            "achievement" -> {
                val key = e["key"]!!.jsonPrimitive.content
                if (ACHIEVEMENTS.any { it.id == key } && key !in state.achievements) state.achievements[key] = at
            }
            "goal" -> {
                val day = e["day"]!!.jsonPrimitive.content
                if (day > state.goalDay) { state.goals += 1; state.goalDay = day }
            }
            "freeze_spent" -> {
                val day = e["day"]!!.jsonPrimitive.content
                if (day !in state.frozen) { state.frozen += day; state.freezes = max(state.freezes - 1, 0) }
            }
            "freeze_earned" -> {
                val day = e["day"]!!.jsonPrimitive.content
                if (state.freezeEarned != day && state.freezes < MAX_FREEZES) { state.freezes += 1; state.freezeEarned = day }
            }
        }
        save(state)
    }

    /** One sitting: its combo, what it achieved, and how long it runs ("cards", "time" or ""). Times in seconds (monotonic). */
    class Session(val limit: String = "", val started: Double = monotonic()) {
        var reviewed = 0; var passed = 0; var combo = 0; var best = 0; var xp = 0; var matured = 0
        var stability = 0.0
        val multiplier get() = multiplier(combo)

        fun record(before: Row, after: Row, rating: Int, seconds: Double): List<JsonObject> {
            reviewed++
            if (rating > 1) passed++
            combo = if (seconds >= COMBO_PAUSE) 1 else combo + 1
            best = max(best, combo)
            if (interval(before) < MATURE_DAYS && MATURE_DAYS <= interval(after)) matured++
            stability += max((after.doubleOrNull("stability") ?: 0.0) - (before.doubleOrNull("stability") ?: 0.0), 0.0)
            val events = mutableListOf<JsonObject>()
            if (combo % COMBO_STEP == 0 && multiplier > multiplier(combo - 1))
                events += buildJsonObject { put("kind", "combo"); put("combo", combo); put("multiplier", multiplier) }
            if (reviewed % MILESTONE_EVERY == 0) events += buildJsonObject { put("kind", "milestone"); put("n", reviewed) }
            return events
        }

        fun over(now: Double = monotonic()): Boolean = when (limit) {
            "cards" -> reviewed >= QUICK_CARDS
            "time" -> now - started >= QUICK_SECONDS
            else -> false
        }

        fun progress(remaining: Int, now: Double = monotonic()) = buildJsonObject {
            put("limit", limit); put("done", reviewed)
            put("total", if (limit == "cards") QUICK_CARDS else reviewed + remaining)
            put("left", max(QUICK_SECONDS - (now - started), 0.0)); put("seconds", QUICK_SECONDS)
            put("combo", combo); put("multiplier", multiplier)
        }

        fun summary(caughtUp: Boolean, now: Double = monotonic()) = buildJsonObject {
            put("reviewed", reviewed); put("passed", passed); put("seconds", Srs.pyRound(now - started))
            put("xp", xp); put("best", best); put("matured", matured); put("stability", Srs.pyRound(stability))
            put("caught_up", caughtUp); put("quick", limit.isNotEmpty() && over(now)); put("limit", limit)
        }
    }
}

fun monotonic(): Double = System.nanoTime() / 1e9
