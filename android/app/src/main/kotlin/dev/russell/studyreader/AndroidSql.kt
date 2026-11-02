package dev.russell.studyreader

import android.database.Cursor
import android.database.sqlite.SQLiteCursor
import android.database.sqlite.SQLiteDatabase
import dev.russell.studyreader.core.Row
import dev.russell.studyreader.core.Sql
import java.io.File

/** The core's Sql over android.database.sqlite, binding arguments with their types. */
class AndroidSql(val file: File) : Sql, AutoCloseable {
    val db: SQLiteDatabase = SQLiteDatabase.openDatabase(file.path, null, SQLiteDatabase.OPEN_READWRITE or SQLiteDatabase.CREATE_IF_NECESSARY)

    override fun query(sql: String, vararg args: Any?): List<Row> {
        val cursor = db.rawQueryWithFactory({ _, driver, table, query ->
            args.forEachIndexed { i, a ->
                val n = i + 1
                when (a) {
                    null -> query.bindNull(n)
                    is Int -> query.bindLong(n, a.toLong())
                    is Long -> query.bindLong(n, a)
                    is Double -> query.bindDouble(n, a)
                    is Float -> query.bindDouble(n, a.toDouble())
                    is Boolean -> query.bindLong(n, if (a) 1 else 0)
                    is ByteArray -> query.bindBlob(n, a)
                    else -> query.bindString(n, a.toString())
                }
            }
            SQLiteCursor(driver, table, query)
        }, sql, null, "")
        return cursor.use { c ->
            val out = ArrayList<Row>(c.count)
            val names = c.columnNames
            while (c.moveToNext()) {
                val row = LinkedHashMap<String, Any?>(names.size)
                for (i in names.indices) {
                    row[names[i]] = when (c.getType(i)) {
                        Cursor.FIELD_TYPE_NULL -> null
                        Cursor.FIELD_TYPE_INTEGER -> c.getLong(i)
                        Cursor.FIELD_TYPE_FLOAT -> c.getDouble(i)
                        Cursor.FIELD_TYPE_BLOB -> c.getBlob(i)
                        else -> c.getString(i)
                    }
                }
                out += row
            }
            out
        }
    }

    override fun exec(sql: String, vararg args: Any?) {
        val bound = args.map { if (it is Boolean) (if (it) 1L else 0L) else if (it is Int) it.toLong() else it }.toTypedArray()
        if (bound.isEmpty()) db.execSQL(sql) else db.execSQL(sql, bound)
    }

    override fun <T> transaction(block: () -> T): T {
        db.beginTransaction()
        try {
            val r = block()
            db.setTransactionSuccessful()
            return r
        } finally {
            db.endTransaction()
        }
    }

    override fun close() = db.close()
}

/** A Sql whose database can be swapped for a new snapshot while the rest of the app holds on to it. */
class SwappableSql(var current: Sql) : Sql {
    override fun query(sql: String, vararg args: Any?) = current.query(sql, *args)
    override fun exec(sql: String, vararg args: Any?) = current.exec(sql, *args)
    override fun <T> transaction(block: () -> T): T = current.transaction(block)
}
