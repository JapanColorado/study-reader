"""Fetch a web page in the background and print it to a PDF snapshot with QtWebEngine,
so it reads, selects and chats like any other book."""

from __future__ import annotations

import threading
from pathlib import Path

import pymupdf
from PySide6.QtCore import QMarginsF, QObject, QSizeF, QTimer, QUrl, Signal
from PySide6.QtGui import QPageLayout, QPageSize
from PySide6.QtWebEngineCore import QWebEnginePage, QWebEngineSettings

from . import web_import as wi
from .document import REFLOW_HEIGHT, REFLOW_WIDTH

# Give up waiting for slow images after this long and print what has loaded.
LOAD_TIMEOUT_MS = 45_000
READY_TIMEOUT_MS = 10_000
MARGINS = (46, 44, 46, 44)  # points: left, top, right, bottom


class WebImporter(QObject):
    """One import at a time: `start(url)`, then `finished(pdf_or_epub_path, url)` or `failed(message)`."""

    status = Signal(str)
    finished = Signal(str, str)
    failed = Signal(str)
    _fetched = Signal(object, str)
    _fetch_failed = Signal(str)

    def __init__(self, out_dir: Path, parent=None):
        super().__init__(parent)
        self.out_dir = Path(out_dir).resolve()
        self.page: QWebEnginePage | None = None
        self.busy = False
        self._fetched.connect(self._on_fetched)
        self._fetch_failed.connect(self._fail)

    def start(self, url: str) -> None:
        self.busy = True
        threading.Thread(target=self._fetch, args=(url,), daemon=True).start()

    def _fetch(self, url: str) -> None:
        try:
            result = wi.fetch(url, self.status.emit)
        except Exception as e:  # network errors, API errors, unparseable pages
            self._fetch_failed.emit(str(e) or type(e).__name__)
            return
        self._fetched.emit(result, wi.normalize_url(url))

    def _fail(self, message: str) -> None:
        self.busy = False
        self.failed.emit(message)

    def _done(self, path: str, url: str) -> None:
        self.busy = False
        self.finished.emit(path, url)

    def _on_fetched(self, result, url: str) -> None:
        self.out_dir.mkdir(parents=True, exist_ok=True)
        if isinstance(result, wi.Download):
            path = self.out_dir / f"{wi.snapshot_name(result.title)}.{result.ext}"
            path.write_bytes(result.data)
            self._done(str(path), url)
            return
        self.status.emit("Laying out pages…")
        name = wi.snapshot_name(result.title)
        self._html = self.out_dir / f"{name}.html"
        self._pdf = self.out_dir / f"{name}.pdf"
        doc, self._toc = wi.build_html(result)
        self._html.write_text(doc, encoding="utf-8")
        self._article, self._result_url = result, url

        self.page = QWebEnginePage(self)
        # The snapshot is a local file; its images are still on the web.
        self.page.settings().setAttribute(QWebEngineSettings.WebAttribute.LocalContentCanAccessRemoteUrls, True)
        self.page.loadFinished.connect(self._on_loaded)
        self.page.pdfPrintingFinished.connect(self._on_printed)
        self._load_timer = QTimer(self, singleShot=True, interval=LOAD_TIMEOUT_MS)
        self._load_timer.timeout.connect(self._stop_loading)
        self._timed_out = False
        self._load_timer.start()
        self.page.load(QUrl.fromLocalFile(str(self._html)))

    def _stop_loading(self) -> None:
        self._timed_out = True
        self.page.triggerAction(QWebEnginePage.WebAction.Stop)

    def _on_loaded(self, ok: bool) -> None:
        self._load_timer.stop()
        if not ok and not self._timed_out:
            self._abandon("Couldn't lay out the page")
            return
        # Wait for MathJax to typeset (the page flags when it is done), but not forever.
        self._waited = 0
        self._poll()

    def _poll(self) -> None:
        def check(ready) -> None:
            self._waited += 150
            if ready or self._waited >= READY_TIMEOUT_MS:
                self._print()
            else:
                QTimer.singleShot(150, self._poll)
        self.page.runJavaScript("window.studyReaderReady === true", 0, check)

    def _print(self) -> None:
        layout = QPageLayout(
            QPageSize(QSizeF(REFLOW_WIDTH, REFLOW_HEIGHT), QPageSize.Unit.Point, "web"),
            QPageLayout.Orientation.Portrait, QMarginsF(*MARGINS), QPageLayout.Unit.Point,
        )
        self.page.printToPdf(str(self._pdf), layout)

    def _abandon(self, message: str) -> None:
        self.page.deleteLater()
        self.page = None
        self._html.unlink(missing_ok=True)
        self._fail(message)

    def _on_printed(self, path: str, ok: bool) -> None:
        if not ok:
            self._abandon("Couldn't print the page to PDF")
            return
        self.page.deleteLater()
        self.page = None
        self._html.unlink(missing_ok=True)
        try:
            finish_pdf(path, self._article.title, self._result_url, self._toc)
        except Exception as e:
            self._fail(f"Couldn't finish the PDF: {e}")
            return
        self._done(path, self._result_url)


def finish_pdf(path: str, title: str, url: str, toc: list[tuple[int, str]]) -> None:
    """Title, source URL and a table of contents, found by searching for each heading in
    page order (headings come out of the printer in the order they were written)."""
    doc = pymupdf.open(path)
    entries, page = [], 0
    for level, text in toc:
        needle = " ".join(text.split()[:6])
        for p in range(page, doc.page_count):
            if doc[p].search_for(needle):
                page = p
                break
        entries.append([level, text, page + 1])
    doc.set_metadata({**(doc.metadata or {}), "title": title, "subject": url, "creator": "Study Reader"})
    doc.set_toc(entries)
    tmp = Path(path).with_suffix(".tmp")
    doc.save(tmp, garbage=3, deflate=True)
    doc.close()
    tmp.replace(path)
