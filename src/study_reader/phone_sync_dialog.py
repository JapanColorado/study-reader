"""Phone sync in the app: syncs with the shared folder (phone_sync.py) at startup, every few
minutes and on quitting, and the settings dialog that picks the folder."""

from __future__ import annotations

import time
from pathlib import Path

from PySide6.QtCore import QObject, QTimer, Signal
from PySide6.QtWidgets import (
    QDialog, QDialogButtonBox, QFileDialog, QHBoxLayout, QLabel, QLineEdit, QPushButton, QVBoxLayout,
)

from . import phone_sync, srs
from .db import Database

INTERVAL_MS = 2 * 60_000


class PhoneSync(QObject):
    """Runs phone_sync.sync while a folder is set. synced(result) after each run that did something."""

    synced = Signal(object)
    failed = Signal(str)

    def __init__(self, db: Database, after_review=None, parent=None):
        super().__init__(parent)
        self.db = db
        self.after_review = after_review  # called with each card id the phone reviewed (new numbers)
        self.last_error = ""
        self._timer = QTimer(self, interval=INTERVAL_MS)
        self._timer.timeout.connect(self.sync)
        self._timer.start()

    def sync(self, force: bool = False) -> phone_sync.Result | None:
        root = phone_sync.folder(self.db)
        if root is None:
            return None
        if not root.is_dir():
            self.last_error = f"The folder {root} isn't there."
            self.failed.emit(self.last_error)
            return None
        try:
            result = phone_sync.sync(self.db, root, force=force)
        except OSError as e:
            self.last_error = str(e)
            self.failed.emit(self.last_error)
            return None
        self.last_error = ""
        if self.after_review is not None:
            for card_id in dict.fromkeys(result.reviewed):
                self.after_review(card_id)
        if result.applied or result.exported:
            self.synced.emit(result)
        return result


class PhoneSyncDialog(QDialog):
    def __init__(self, sync: PhoneSync, parent=None):
        super().__init__(parent)
        self.sync = sync
        self.db = sync.db
        self.setWindowTitle("Phone sync")
        self.setMinimumWidth(520)
        layout = QVBoxLayout(self)
        intro = QLabel(
            "Review cards on your phone with the Study Reader Review app. Both share a folder that "
            "<a href='https://syncthing.net'>Syncthing</a> keeps in step: this computer writes your cards "
            "into it, and the phone writes back what you reviewed, which is applied here. Pick the folder "
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
            lines.append("Last synced just now." if ago < 60 else f"Last synced {srs.format_interval(ago)} ago.")
        if result is not None:
            lines.append(f"Just now: {result.applied} change{'s' * (result.applied != 1)} from the phone applied"
                         + (", cards written for the phone." if result.exported else "."))
            lines += [f"Couldn't read yet: {e}" for e in result.errors[:3]]
        lines.append("Phones that have synced: " + (str(s["phones"]) if s["phones"] else "none yet") + ".")
        self.status.setText("<br>".join(lines))
