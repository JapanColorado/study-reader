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
    /**
     * Runs block in one transaction: committed if it returns, rolled back if it throws. A call inside
     * another is a savepoint in it ([savepoint]): if it throws, only its own changes are undone, and
     * the outer one goes on if it catches the exception.
     */
    fun <T> transaction(block: () -> T): T

    fun one(sql: String, vararg args: Any?): Row? = query(sql, *args).firstOrNull()
    fun long(sql: String, vararg args: Any?): Long = (query(sql, *args).firstOrNull()?.values?.firstOrNull() as? Number)?.toLong() ?: 0L
}

/** The nested part of [Sql.transaction], for implementations to call when a transaction is already open. */
fun <T> Sql.savepoint(block: () -> T): T {
    exec("SAVEPOINT nested")  // the same name at every depth: SQLite takes the innermost
    val r = try {
        block()
    } catch (e: Throwable) {
        // (the leading ';' keeps Android before API 28 from taking this for a ROLLBACK of the whole transaction)
        exec(";ROLLBACK TO nested")
        exec("RELEASE nested")
        throw e
    }
    exec("RELEASE nested")
    return r
}

fun Row.long(key: String): Long = (this[key] as Number).toLong()
fun Row.longOrNull(key: String): Long? = (this[key] as? Number)?.toLong()
fun Row.double(key: String): Double = (this[key] as Number).toDouble()
fun Row.doubleOrNull(key: String): Double? = (this[key] as? Number)?.toDouble()
fun Row.str(key: String): String = this[key]?.toString() ?: ""
