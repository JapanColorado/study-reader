"""Phone sync in the app: syncs with the shared folder (phone_sync.py) at startup, every few
minutes and on quitting, and the settings dialog that picks the folder."""

from __future__ import annotations

import sqlite3
import threading
import time
from pathlib import Path

from PySide6.QtCore import QCoreApplication, QObject, QTimer, Signal
from PySide6.QtWidgets import (
    QDialog, QDialogButtonBox, QFileDialog, QHBoxLayout, QLabel, QLineEdit, QPushButton, QVBoxLayout,
)

from . import phone_sync, practice
from .db import Database

INTERVAL_MS = 2 * 60_000


class PhoneSync(QObject):
    """Runs phone_sync.sync while a folder is set. synced(result) after each run that did something.
    What went wrong last is last_error, which the dialog shows.

    The phones' batches are applied here, on the GUI thread (they write to the database), but the
    snapshot, which takes a second or two to write once there are many attempts, is written on a
    thread of its own with its own database connection: one at a time, and a snapshot asked for
    while one is being written is planned again once it is done. The app waits for it on quitting
    (wait)."""

    synced = Signal(object)
    _written = Signal()  # from the snapshot thread

    def __init__(self, db: Database, after_attempt=None, parent=None):
        super().__init__(parent)
        self.db = db
        self.after_attempt = after_attempt  # called with each question id the phone attempted (new numbers)
        self._error = ""  # of the last sync
        self._export_error = ""  # of the last snapshot written on the thread
        self._thread: threading.Thread | None = None  # writing a snapshot
        self._outcome: tuple[phone_sync.Export, str] | None = None  # its plan and error, once it is done
        self._again: bool | None = None  # a sync (forced or not) waiting for it
        self._written.connect(self._finish)
        self._timer = QTimer(self, interval=INTERVAL_MS)
        self._timer.timeout.connect(self.sync)
        self._timer.start()
        app = QCoreApplication.instance()
        if app is not None:
            app.aboutToQuit.connect(self.wait)  # the sync on closing writes one too

    def sync(self, force: bool = False) -> phone_sync.Result | None:
        root = phone_sync.folder(self.db)
        if root is None:
            return None
        if not root.is_dir():
            self._error = f"The folder {root} isn't there."
            return None
        try:
            result = phone_sync.sync(self.db, root, force=force, write=lambda plan: self._write(plan, force))
        except (OSError, sqlite3.Error, ValueError) as e:  # reported, not raised: this also runs as the window closes
            self._error = str(e)
            return None
        self._error = ""
        if self.after_attempt is not None:
            for question_id in dict.fromkeys(result.attempted):
                self.after_attempt(question_id)
        if result.applied or result.exported:
            self.synced.emit(result)
        return result

    @property
    def last_error(self) -> str:
        return self._error or self._export_error

    def wait(self) -> None:
        """Finish writing the snapshot (and one asked for meanwhile) before going on."""
        while self._thread is not None:
            self._thread.join()
            self._finish()

    def _write(self, plan: phone_sync.Export, force: bool) -> None:
        if self._thread is not None:
            self._again = bool(self._again) or force
            return
        if not Path(self.db.path).is_file():  # nothing another connection could open
            phone_sync.write_snapshot(self.db.conn, plan)
            phone_sync.finish_export(self.db, plan)
            return
        # Not a daemon: should the app quit without waiting, the snapshot is still finished.
        self._thread = threading.Thread(target=self._run, args=(plan,), name="phone-snapshot")
        self._thread.start()

    def _run(self, plan: phone_sync.Export) -> None:
        """On the snapshot thread."""
        error = "The snapshot wasn't finished."
        try:
            phone_sync.write_snapshot_apart(self.db.path, plan)
            error = ""
        except Exception as e:  # noqa: BLE001 - shown in the dialog
            error = str(e) or type(e).__name__
        finally:
            self._outcome = (plan, error)
        try:
            self._written.emit()
        except RuntimeError:  # deleted meanwhile (the app is quitting)
            pass

    def _finish(self) -> None:
        """Back on the GUI thread once the snapshot thread is done (or from wait)."""
        if self._outcome is None:  # handled already
            return
        self._thread.join()
        (plan, error), self._outcome, self._thread = self._outcome, None, None
        self._export_error = error
        if not error and plan.root == phone_sync.folder(self.db):  # not a folder given up meanwhile
            phone_sync.finish_export(self.db, plan)
        if self._again is not None:
            force, self._again = self._again, None
            self.sync(force=force)


class PhoneSyncDialog(QDialog):
    def __init__(self, sync: PhoneSync, parent=None):
        super().__init__(parent)
        self.sync = sync
        self.db = sync.db
        self.setWindowTitle("Phone sync")
        self.setMinimumWidth(520)
        layout = QVBoxLayout(self)
        intro = QLabel(
            "Practice questions on your phone with the Study Practice app. Both share a folder that "
            "<a href='https://syncthing.net'>Syncthing</a> keeps in step: this computer writes your questions "
            "into it, and the phone writes back what you practiced, which is applied here. Pick the folder "
            "Syncthing shares with the phone (an empty folder of its own is best), then choose the same folder "
            "in the app."
        )
        intro.setWordWrap(True)
        intro.setOpenExternalLinks(True)
        layout.addWidget(intro)
        row = QHBoxLayout()
        self.path = QLineEdit(str(phone_sync.folder(self.db) or ""))
        self.path.setPlaceholderText("Not syncing")
        self.path.setReadOnly(True)
        row.addWidget(self.path, 1)
        choose = QPushButton("Choose…")
        choose.clicked.connect(self._choose)
        row.addWidget(choose)
        off = QPushButton("Turn off")
        off.clicked.connect(self._off)
        row.addWidget(off)
        layout.addLayout(row)
        self.status = QLabel()
        self.status.setWordWrap(True)
        layout.addWidget(self.status)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Close)
        self.sync_button = buttons.addButton("Sync now", QDialogButtonBox.ButtonRole.ActionRole)
        self.sync_button.clicked.connect(self._sync_now)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)
        self._show_status()

    def _choose(self) -> None:
        start = self.path.text() or str(Path.home())
        path = QFileDialog.getExistingDirectory(self, "Folder shared with the phone", start)
        if path:
            self.db.set_setting(phone_sync.FOLDER_SETTING, path)
            self.db.set_setting(phone_sync.FINGERPRINT_SETTING, "")
            self.path.setText(path)
            self._sync_now()

    def _off(self) -> None:
        self.db.set_setting(phone_sync.FOLDER_SETTING, "")
        self.path.setText("")
        self._show_status()

    def _sync_now(self) -> None:
        result = self.sync.sync(force=True)
        self.sync.wait()  # asked for: what it wrote (or what went wrong) is shown
        self._show_status(result)

    def _show_status(self, result: phone_sync.Result | None = None) -> None:
        s = phone_sync.status(self.db)
        self.sync_button.setEnabled(bool(s["folder"]))
        if not s["folder"]:
            self.status.setText("Phone sync is off.")
            return
        lines = []
        if self.sync.last_error:
            lines.append(f"<span style='color:#c0392b'>{self.sync.last_error}</span>")
        if s["last"]:
            ago = time.time() - s["last"]
            lines.append("Last synced just now." if ago < 60 else f"Last synced {practice.format_interval(ago)} ago.")
        if result is not None:
            lines.append(f"Just now: {result.applied} change{'s' * (result.applied != 1)} from the phone applied"
                         + (", questions written for the phone." if result.exported else "."))
            lines += [f"Couldn't read yet: {e}" for e in result.errors[:3]]
        lines.append("Phones that have synced: " + (str(s["phones"]) if s["phones"] else "none yet") + ".")
        self.status.setText("<br>".join(lines))
