package dev.russell.studyreader.core

import org.junit.Assert.assertEquals
import org.junit.Test

class SqlTest {
    @Test
    fun roundTrip() {
        JdbcSql().use { db ->
            db.exec("CREATE TABLE t (a INTEGER, b TEXT, c REAL)")
            db.transaction { db.exec("INSERT INTO t VALUES (?,?,?)", 1, "x", 2.5) }
            val r = db.one("SELECT * FROM t")!!
            assertEquals(1L, r.long("a"))
            assertEquals("x", r.str("b"))
            assertEquals(2.5, r.double("c"), 0.0)
        }
    }
}
