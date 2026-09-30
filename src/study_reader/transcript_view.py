"""Chat transcript rendered in a web view (Markdown + KaTeX, bundled offline)."""

from __future__ import annotations

import json
from pathlib import Path

from PySide6.QtCore import QTimer, QUrl, Signal
from PySide6.QtGui import QColor, QDesktopServices
from PySide6.QtWebEngineCore import QWebEnginePage
from PySide6.QtWebEngineWidgets import QWebEngineView

from .theme import theme

ASSETS = Path(__file__).parent / "assets"


class _Page(QWebEnginePage):
    jump = Signal(int)

    def acceptNavigationRequest(self, url: QUrl, nav_type, is_main_frame: bool) -> bool:
        if url.scheme() == "jump":
            try:
                self.jump.emit(int(url.path().strip("/")))
            except ValueError:
                pass
            return False
        if url.scheme() in ("http", "https", "mailto"):
            QDesktopServices.openUrl(url)
            return False
        return True


class TranscriptView(QWebEngineView):
    jumpRequested = Signal(int)  # snippet id

    def __init__(self, parent=None):
        super().__init__(parent)
        page = _Page(self)
        page.jump.connect(self.jumpRequested)
        self.setPage(page)
        page.setBackgroundColor(QColor(theme().colors["surface"]))
        self._ready = False
        self._pending: str | None = None
        self._stream_text = ""
        self._stream_timer = QTimer(self, singleShot=True, interval=60)
        self._stream_timer.timeout.connect(self._flush_stream)
        self.loadFinished.connect(self._on_loaded)
        self.load(QUrl.fromLocalFile(str(ASSETS / "transcript.html")))

    def show_messages(self, messages: list[dict], streaming: str | None = None, placeholder: str = "") -> None:
        """streaming: None when idle, else the reply text received so far."""
        self.page().setBackgroundColor(QColor(theme().colors["surface"]))
        out = []
        for m in messages:
            snippets = [
                {
                    "id": s["id"], "kind": s["kind"], "page": s["page"], "text": s["text"],
                    "image_url": QUrl.fromLocalFile(s["image_path"]).toString() if s["image_path"] else "",
                }
                for s in m.get("snippets", [])
            ]
            out.append({"role": m["role"], "text": m["text"], "snippets": snippets})
        data = {"messages": out, "streaming": streaming, "placeholder": placeholder,
                "dark": theme().dark, "colors": theme().colors}
        self._run(f"render({json.dumps(data)})")

    def set_streaming(self, text: str) -> None:
        self._stream_text = text
        if not self._stream_timer.isActive():
            self._stream_timer.start()

    def _flush_stream(self) -> None:
        self._run(f"setStreaming({json.dumps(self._stream_text)})", queue=False)

    def _run(self, js: str, queue: bool = True) -> None:
        if self._ready:
            self.page().runJavaScript(js)
        elif queue:
            self._pending = js

    def _on_loaded(self, ok: bool) -> None:
        self._ready = True
        if self._pending:
            self.page().runJavaScript(self._pending)
            self._pending = None
