package dev.russell.studyreader

import android.content.Context
import android.content.SharedPreferences
import android.net.Uri
import android.provider.DocumentsContract
import android.util.Log
import androidx.documentfile.provider.DocumentFile
import dev.russell.studyreader.core.Practice
import dev.russell.studyreader.core.SyncStore
import java.io.File
import java.util.concurrent.ExecutorService
import java.util.concurrent.Executors

/**
 * The phone's questions (practice.db: a copy of the desktop's snapshot plus what was done here) and
 * syncing them through the shared folder (see phone_sync.py on the desktop for the format).
 * Everything touching the database runs on [worker], one task at a time.
 */
class Store private constructor(private val context: Context) {
    companion object {
        private const val TAG = "StudyPractice"
        @Volatile private var instance: Store? = null
        fun get(context: Context): Store = instance ?: synchronized(this) {
            instance ?: Store(context.applicationContext).also { instance = it }
        }

        const val SNAPSHOT_DIR = "desktop"
        const val SNAPSHOT = "snapshot.db"
        const val MEDIA = "media"
        const val PHONES = "phones"
        private const val TEMP_PREFIX = ".syncthing."
        private const val DB_NAME = "practice.db"
        /** What the database was called before the rename. */
        private const val OLD_DB_NAME = "cards.db"
        private val SIDE_FILES = listOf("-journal", "-wal", "-shm")
    }

    val worker: ExecutorService = Executors.newSingleThreadExecutor { r -> Thread(r, "store").apply { isDaemon = true } }
    val prefs: SharedPreferences = context.getSharedPreferences("settings", Context.MODE_PRIVATE)
    private val dbFile = databaseFile()
    val mediaDir = File(context.filesDir, "media").apply { mkdirs() }
    val sql = SwappableSql(openDb(dbFile))

    init {
        // A copy of a snapshot from before the rename has none of today's tables: take the folder's
        // snapshot in again (or say why it can't be) at the next sync.
        if (SyncStore.schema(sql) < SyncStore.SCHEMA) {
            SyncStore.setLocal(sql, "snapshot", "")
            prefs.edit().putString("snapshot_stamp", "").apply()
        }
    }

    private fun openDb(file: File): AndroidSql = AndroidSql(file).also { SyncStore.prepare(it) }

    /** practice.db, moving an older build's cards.db (and its journal) to it first. If the move
     *  fails, the old file stays in use, so the queue of changes for the computer is never lost. */
    private fun databaseFile(): File {
        val file = File(context.filesDir, DB_NAME)
        val old = File(context.filesDir, OLD_DB_NAME)
        if (file.exists() || !old.exists()) return file
        for (suffix in SIDE_FILES) {
            val from = File(context.filesDir, OLD_DB_NAME + suffix)
            if (from.exists()) from.renameTo(File(context.filesDir, DB_NAME + suffix))
        }
        if (old.renameTo(file)) return file
        for (suffix in SIDE_FILES) File(context.filesDir, DB_NAME + suffix).renameTo(File(context.filesDir, OLD_DB_NAME + suffix))
        return old
    }

    var folder: Uri?
        get() = prefs.getString("folder", null)?.let(Uri::parse)
        set(value) { prefs.edit().putString("folder", value?.toString()).putString("snapshot_stamp", "").apply() }

    val lastSync: Long get() = prefs.getLong("last_sync", 0)
    var lastError: String = ""
        private set

    /** A line for the overview about how syncing stands. */
    fun statusLine(): String {
        if (folder == null) return "Not syncing yet: choose the shared folder in Settings."
        if (lastError.isNotEmpty()) return "Sync problem: $lastError"
        val waiting = SyncStore.unacknowledged(sql)
        val seconds = (System.currentTimeMillis() - lastSync) / 1000.0
        val ago = if (lastSync == 0L) "Not synced yet" else if (seconds < 60) "Synced just now" else "Synced ${Practice.formatInterval(seconds)} ago"
        return if (waiting > 0) "$ago · $waiting change${if (waiting == 1) "" else "s"} on the way to the computer" else ""
    }

    // -- syncing (on the worker) -------------------------------------------------------------
    private fun root(): DocumentFile? = folder?.let { DocumentFile.fromTreeUri(context, it) }?.takeIf { it.isDirectory }

    /** Sends what was done here, and takes in a new snapshot if there is one. True if the questions changed. */
    fun sync(): Boolean {
        val root = root()
        if (root == null) {
            lastError = if (folder != null) "can't open the shared folder (choose it again in Settings)" else ""
            return false
        }
        return try {
            flush(root)
            val changed = takeSnapshot(root)
            syncMedia(root)
            lastError = ""
            prefs.edit().putLong("last_sync", System.currentTimeMillis()).apply()
            changed
        } catch (e: Exception) {
            Log.w(TAG, "sync failed", e)
            lastError = e.message ?: e.toString()
            false
        }
    }

    /**
     * Writes the events not yet sent as a new batch file, and writes again any batch sent but not yet
     * acknowledged whose file isn't in the folder. Events are marked sent only once their file is there.
     */
    fun flush(root: DocumentFile? = root()) {
        root ?: return
        val batch = SyncStore.takeBatch(sql)
        val sent = SyncStore.sentBatches(sql)
        if (batch == null && sent.isEmpty()) return
        val dir = child(child(root, PHONES, create = true)!!, SyncStore.device(sql), create = true)!!
        if (sent.isNotEmpty()) {
            val there = dir.listFiles().mapNotNull { it.name }.toSet()
            for (b in sent) if (b.fileName !in there) write(dir, b)
        }
        if (batch != null) {
            write(dir, batch)
            SyncStore.sent(sql, batch)
        }
    }

    /** Writes a batch as a temporary file renamed into place, so Syncthing never sends half of one. */
    private fun write(dir: DocumentFile, batch: SyncStore.Batch) {
        val bytes = batch.text.toByteArray()
        val tmp = dir.createFile("application/octet-stream", "$TEMP_PREFIX${batch.fileName}.tmp") ?: error("can't write in the shared folder")
        try {
            context.contentResolver.openOutputStream(tmp.uri, "wt")!!.use { it.write(bytes) }
        } catch (e: Exception) {
            tmp.delete()
            throw e
        }
        if (!tmp.renameTo(batch.fileName)) {
            tmp.delete()
            val f = dir.createFile("application/json", batch.fileName) ?: error("can't write in the shared folder")
            context.contentResolver.openOutputStream(f.uri, "wt")!!.use { it.write(bytes) }
        }
    }

    private fun child(dir: DocumentFile, name: String, create: Boolean = false): DocumentFile? =
        dir.findFile(name) ?: if (create) dir.createDirectory(name) else null

    /** Takes in the desktop's snapshot if it changed since last time. A snapshot from a desktop older
     *  than this app is refused (and read again at every sync until the desktop writes a new one). */
    private fun takeSnapshot(root: DocumentFile): Boolean {
        val file = child(root, SNAPSHOT_DIR)?.findFile(SNAPSHOT) ?: return false
        val stamp = "${file.lastModified()}:${file.length()}"
        if (stamp == prefs.getString("snapshot_stamp", "")) return false
        val incoming = File(context.filesDir, "incoming.db")
        incoming.delete()
        context.contentResolver.openInputStream(file.uri)!!.use { input -> incoming.outputStream().use { input.copyTo(it) } }
        val next = AndroidSql(incoming)
        val refusal = try { SyncStore.refusal(next) } catch (e: Exception) { "can't read the computer's snapshot (${e.message})" }
        if (refusal != null) {
            next.close(); incoming.delete()
            error(refusal)
        }
        val id = SyncStore.meta(next, "id")
        if (id != null && id == SyncStore.local(sql, "snapshot")) {
            next.close(); incoming.delete()
            prefs.edit().putString("snapshot_stamp", stamp).apply()
            return false
        }
        val acked = SyncStore.adopt(next, sql)
        next.close()
        (sql.current as AndroidSql).close()
        File(context.filesDir, "${dbFile.name}-journal").delete()
        if (!incoming.renameTo(dbFile)) error("couldn't replace the questions")
        sql.current = openDb(dbFile)
        prefs.edit().putString("snapshot_stamp", stamp).apply()
        // The desktop has these now; the phone's copies can go.
        val dir = child(root, PHONES)?.let { child(it, SyncStore.device(sql)) }
        if (dir != null) for (name in acked) dir.findFile(name)?.delete()
        return true
    }

    /** Mirrors desktop/media into the app's media folder (new or changed files; drops removed ones). */
    private fun syncMedia(root: DocumentFile) {
        val media = child(root, SNAPSHOT_DIR)?.let { child(it, MEDIA) } ?: return
        val resolver = context.contentResolver
        val children = DocumentsContract.buildChildDocumentsUriUsingTree(media.uri, DocumentsContract.getDocumentId(media.uri))
        val remote = mutableMapOf<String, Triple<Uri, Long, Long>>()
        resolver.query(children, arrayOf(
            DocumentsContract.Document.COLUMN_DOCUMENT_ID, DocumentsContract.Document.COLUMN_DISPLAY_NAME,
            DocumentsContract.Document.COLUMN_SIZE, DocumentsContract.Document.COLUMN_LAST_MODIFIED,
            DocumentsContract.Document.COLUMN_MIME_TYPE,
        ), null, null, null)?.use { c ->
            while (c.moveToNext()) {
                val name = c.getString(1) ?: continue
                if (c.getString(4) == DocumentsContract.Document.MIME_TYPE_DIR || name.startsWith(TEMP_PREFIX)) continue
                remote[name] = Triple(DocumentsContract.buildDocumentUriUsingTree(media.uri, c.getString(0)), c.getLong(2), c.getLong(3))
            }
        }
        for ((name, info) in remote) {
            val (uri, size, modified) = info
            val local = File(mediaDir, name)
            if (local.exists() && local.length() == size && local.lastModified() == modified) continue
            val tmp = File(mediaDir, ".$name.tmp")
            resolver.openInputStream(uri)?.use { input -> tmp.outputStream().use { input.copyTo(it) } } ?: continue
            tmp.renameTo(local)
            local.setLastModified(modified)
        }
        mediaDir.listFiles()?.forEach { if (it.name !in remote) it.delete() }
    }

    // -- reminders ---------------------------------------------------------------------------
    /** Questions waiting now: due, plus new ones within today's allowance. */
    fun waiting(): Int {
        val practice = Practice(sql)
        val now = System.currentTimeMillis() / 1000.0
        practice.unbury(now)
        val c = practice.counts(null, now)
        return c["due"]!! + c["new"]!!
    }
}
