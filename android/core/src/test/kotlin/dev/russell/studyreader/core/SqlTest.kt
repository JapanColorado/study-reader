package dev.russell.studyreader.core

import org.junit.Assert.assertEquals
import org.junit.Assert.assertThrows
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

    @Test
    fun aNestedTransactionThatFailsUndoesOnlyItsOwnChanges() {
        JdbcSql().use { db ->
            db.exec("CREATE TABLE t (a INTEGER)")
            db.transaction {
                db.exec("INSERT INTO t VALUES (1)")
                try {
                    db.transaction {
                        db.exec("INSERT INTO t VALUES (2)")
                        db.transaction { db.exec("INSERT INTO t VALUES (3)") }
                        error("inner")
                    }
                } catch (_: IllegalStateException) {}
                db.transaction { db.exec("INSERT INTO t VALUES (4)") }
            }
            assertEquals(listOf(1L, 4L), db.query("SELECT a FROM t ORDER BY a").map { it.long("a") })
            // An outer one that fails undoes everything, its nested ones' changes too.
            assertThrows(IllegalStateException::class.java) {
                db.transaction {
                    db.transaction { db.exec("INSERT INTO t VALUES (5)") }
                    error("outer")
                }
            }
            assertEquals(2L, db.long("SELECT COUNT(*) FROM t"))
        }
    }
}
