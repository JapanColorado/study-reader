package dev.russell.studyreader.core

import java.sql.Connection
import java.sql.DriverManager

/** Sql over sqlite-jdbc, for tests. path ":memory:" for an in-memory database. */
class JdbcSql(path: String = ":memory:") : Sql, AutoCloseable {
    val conn: Connection = DriverManager.getConnection("jdbc:sqlite:$path")
    private var depth = 0

    private fun bind(stmt: java.sql.PreparedStatement, args: Array<out Any?>) {
        args.forEachIndexed { i, a ->
            when (a) {
                null -> stmt.setNull(i + 1, java.sql.Types.NULL)
                is Int -> stmt.setLong(i + 1, a.toLong())
                is Long -> stmt.setLong(i + 1, a)
                is Double -> stmt.setDouble(i + 1, a)
                is Float -> stmt.setDouble(i + 1, a.toDouble())
                is Boolean -> stmt.setLong(i + 1, if (a) 1 else 0)
                is ByteArray -> stmt.setBytes(i + 1, a)
                else -> stmt.setString(i + 1, a.toString())
            }
        }
    }

    override fun query(sql: String, vararg args: Any?): List<Row> =
        conn.prepareStatement(sql).use { stmt ->
            bind(stmt, args)
            stmt.executeQuery().use { rs ->
                val meta = rs.metaData
                val out = mutableListOf<Row>()
                while (rs.next()) {
                    val row = LinkedHashMap<String, Any?>()
                    for (c in 1..meta.columnCount) {
                        row[meta.getColumnLabel(c)] = when (val v = rs.getObject(c)) {
                            is Int -> v.toLong()
                            is Float -> v.toDouble()
                            else -> v
                        }
                    }
                    out += row
                }
                out
            }
        }

    override fun exec(sql: String, vararg args: Any?) {
        conn.prepareStatement(sql).use { stmt -> bind(stmt, args); stmt.execute() }
    }

    override fun <T> transaction(block: () -> T): T {
        if (depth > 0) return block()
        conn.autoCommit = false
        depth++
        try {
            val r = block()
            conn.commit()
            return r
        } catch (e: Throwable) {
            conn.rollback()
            throw e
        } finally {
            depth--
            conn.autoCommit = true
        }
    }

    override fun close() = conn.close()
}
