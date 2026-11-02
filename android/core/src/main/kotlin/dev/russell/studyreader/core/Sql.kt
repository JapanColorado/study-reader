package dev.russell.studyreader.core

/** A row: column name -> Long, Double, String, ByteArray or null. */
typealias Row = Map<String, Any?>

/**
 * The SQLite access the core logic needs. The app implements it over android.database.sqlite;
 * tests over sqlite-jdbc. Arguments are Long/Int/Double/String/null.
 */
interface Sql {
    fun query(sql: String, vararg args: Any?): List<Row>
    fun exec(sql: String, vararg args: Any?)
    /** Runs block in one transaction (nested calls join the outer one). */
    fun <T> transaction(block: () -> T): T

    fun one(sql: String, vararg args: Any?): Row? = query(sql, *args).firstOrNull()
    fun long(sql: String, vararg args: Any?): Long = (query(sql, *args).firstOrNull()?.values?.firstOrNull() as? Number)?.toLong() ?: 0L
}

fun Row.long(key: String): Long = (this[key] as Number).toLong()
fun Row.longOrNull(key: String): Long? = (this[key] as? Number)?.toLong()
fun Row.int(key: String): Int = (this[key] as Number).toInt()
fun Row.double(key: String): Double = (this[key] as Number).toDouble()
fun Row.doubleOrNull(key: String): Double? = (this[key] as? Number)?.toDouble()
fun Row.str(key: String): String = this[key]?.toString() ?: ""
fun Row.strOrNull(key: String): String? = this[key]?.toString()
