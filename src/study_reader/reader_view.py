"""Continuous-scroll page view with text selection, screenshot-region capture,
highlights on every passage that has been asked about or annotated, and footnote and
cross-reference links that preview on hover and jump on click."""

from __future__ import annotations

import bisect
import html
import time
from collections import OrderedDict

from PySide6.QtCore import QPoint, QPointF, QRectF, Qt, QTimer, Signal
from PySide6.QtGui import QColor, QGuiApplication, QImage, QKeySequence, QPainter, QPen
from PySide6.QtWidgets import QAbstractScrollArea, QFrame, QGraphicsDropShadowEffect, QHBoxLayout, QPushButton, QToolTip

from .document import Document, Link, Rect, Word, line_rects, select_range, words_text
from .theme import darken_page, theme

GAP = 14
MARGIN = 16
CROP_GAP = 6  # between pages when margins are cropped; the side margin is then 0
CACHE_PAGES = 10
CAPTURE_SCALE = 2.0
MIN_ZOOM, MAX_ZOOM = 0.4, 4.0

SELECT_COLOR = QColor(60, 130, 230, 90)
FLASH_COLOR = QColor(255, 200, 40, 110)
SHADOW = [(3, 10), (2, 14), (1, 22)]  # (spread px, alpha), outermost first
# Highlighter ink: multiplied onto light pages, screened onto dark ones, so text stays legible.
MARK_LIGHT = (QColor(255, 228, 130), QColor(255, 204, 80))  # (normal, hovered)
MARK_DARK = (QColor(80, 62, 22), QColor(125, 95, 30))
NOTE_LIGHT = (QColor(178, 218, 255), QColor(138, 196, 250))  # annotated passages, in blue
NOTE_DARK = (QColor(26, 58, 92), QColor(40, 84, 130))
FRAME_COLOR = (QColor(232, 168, 40), QColor(214, 130, 20))  # screenshot regions: (normal, hovered)
# Link pills, blended like the highlighter: footnote markers, and fainter cross-references.
FOOTNOTE_LIGHT = (QColor(200, 190, 252), QColor(172, 156, 246))
FOOTNOTE_DARK = (QColor(62, 52, 118), QColor(88, 74, 160))
XREF_LIGHT = (QColor(236, 233, 253), QColor(212, 204, 252))
XREF_DARK = (QColor(36, 32, 64), QColor(62, 52, 118))
# Reading aloud: the sentence, and the word in it, blended like the highlighter.
SPEECH_LIGHT = (QColor(208, 228, 255), QColor(255, 200, 100))
SPEECH_DARK = (QColor(30, 52, 82), QColor(122, 88, 24))
# Find (Ctrl+F): every match, and the current one.
FIND_LIGHT = (QColor(190, 236, 170), QColor(255, 165, 70))
FIND_DARK = (QColor(34, 78, 40), QColor(150, 84, 18))
FOLLOW_PAUSE = 8.0  # s after the user scrolls before reading aloud moves the view again
CLICK_SLOP = 4  # px a press may move and still count as a click
LINK_SLOP = 2.0  # pt around a link that still hits it (footnote markers are tiny)


class ReaderView(QAbstractScrollArea):
    # dict(kind, page, text, rects, png) and whether it should start a new chat
    snippetChosen = Signal(dict, bool)
    quizChosen = Signal(dict)  # a snippet to be quizzed on
    noteChosen = Signal(dict)  # a text snippet to annotate
    pageChanged = Signal(int)
    zoomChanged = Signal(float)
    highlightClicked = Signal(list, list, QPoint)  # snippets and notes under the click, global position
    readRequested = Signal(object, bool)  # selected words by page; whether to read on past them
    speechClicked = Signal(int, float, float)  # a click while reading aloud: page, x, y
    escapePressed = Signal()  # Escape with nothing selected

    def __init__(self, parent=None):
        super().__init__(parent)
        self.doc: Document | None = None
        self.zoom = 1.25
        self.region_mode = False
        self.fit_mode = True
        self.crop = False
        self._content_box: Rect | None = None
        self._clips: list[Rect] = []  # visible part of each page, in page points
        self._sizes: list[tuple[float, float]] = []  # size of each clip
        self._tops: list[float] = []
        self._total_h = 0.0
        self._max_w = 0.0
        self._cache: OrderedDict[tuple, QImage] = OrderedDict()
        self._anchor: tuple[int, float, float] | None = None
        self._dragging = False
        self._selection: dict[int, list[Word]] = {}
        self._region: tuple[int, tuple[float, float, float, float]] | None = None
        self._flash: tuple[int, list] | None = None
        self._page = 0
        self._marks: dict[int, list[dict]] = {}  # page -> dict(kind, rects, snippets, notes)
        self._hover: list[dict] = []
        self._press: QPointF | None = None
        self._hover_link: Link | None = None
        self._back: list[tuple[int, float]] = []  # positions to return to after following links
        self._speech: tuple[dict, dict | None] | None = None  # read-aloud (sentence, word) rects by page
        self.speech_active = False  # clicks jump the reading voice
        self._find: dict[int, list] = {}  # find matches: page -> rects of each match
        self._find_current: tuple[int, list] | None = None
        self._last_nav = 0.0  # when the user last scrolled or jumped
        self.verticalScrollBar().actionTriggered.connect(self._user_moved)

        self.viewport().setMouseTracking(True)
        self.viewport().setCursor(Qt.CursorShape.IBeamCursor)
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)

        self.setFrameShape(QFrame.Shape.NoFrame)
        theme().changed.connect(self.viewport().update)

        self._popup = QFrame(self.viewport(), objectName="popup")
        self._popup.setCursor(Qt.CursorShape.ArrowCursor)
        shadow = QGraphicsDropShadowEffect(self._popup, blurRadius=24, offset=QPointF(0, 4))
        shadow.setColor(QColor(0, 0, 0, 70))
        self._popup.setGraphicsEffect(shadow)
        row = QHBoxLayout(self._popup)
        row.setContentsMargins(6, 6, 6, 6)
        row.setSpacing(4)
        ask = QPushButton("Ask in new chat")
        ask.setProperty("primary", True)
        ask.clicked.connect(lambda: self.choose(True))
        add = QPushButton("Add to current chat")
        add.clicked.connect(lambda: self.choose(False))
        quiz = QPushButton("Quiz me")
        quiz.clicked.connect(self.choose_quiz)
        self._note_btn = QPushButton("Note")
        self._note_btn.clicked.connect(self.choose_note)
        self._copy_btn = QPushButton("Copy")
        self._copy_btn.clicked.connect(self.copy_selection)
        self._read_btn = QPushButton("Read aloud")
        self._read_btn.setToolTip("Read the selection aloud")
        self._read_btn.clicked.connect(lambda: self.choose_read(False))
        self._read_on_btn = QPushButton("Read from here")
        self._read_on_btn.setToolTip("Read aloud from the selection on")
        self._read_on_btn.clicked.connect(lambda: self.choose_read(True))
        for b in (ask, add, quiz, self._note_btn, self._copy_btn, self._read_btn, self._read_on_btn):
            row.addWidget(b)
        self._popup.hide()

        self._back_bar = QFrame(self.viewport(), objectName="popup")
        self._back_bar.setCursor(Qt.CursorShape.ArrowCursor)
        row = QHBoxLayout(self._back_bar)
        row.setContentsMargins(4, 4, 4, 4)
        back = QPushButton("← Back")
        back.setToolTip("Return to where you followed the link from (Alt+Left)")
        back.clicked.connect(self.go_back)
        row.addWidget(back)
        self._back_bar.hide()

    # -- document / layout -----------------------------------------------------
    def set_document(
        self, doc: Document | None, page: int = 0, zoom: float | None = None, crop: bool = False
    ) -> None:
        self.doc = doc
        self._cache.clear()
        self._marks, self._hover, self._hover_link = {}, [], None
        self._back = []
        self._back_bar.hide()
        self._speech = None
        self._find, self._find_current = {}, None
        self.clear_selection()
        self._content_box = None
        self.crop = crop
        self._set_clips()
        # zoom <= 0 (or None) means "fit width", the default for new books.
        self.fit_mode = not (zoom and zoom > 0)
        self.zoom = self._fit_width_zoom() if self.fit_mode else min(max(zoom, MIN_ZOOM), MAX_ZOOM)
        self._relayout()
        self.go_to(page)
        self.zoomChanged.emit(self.zoom)

    def _set_clips(self) -> None:
        """Each page's visible rect: the whole page, or the book's content box when cropping."""
        n = self.doc.page_count if self.doc else 0
        if self.crop and self._content_box is None and n:
            self._content_box = self.doc.content_box()
        box = self._content_box if self.crop else None
        self._clips = []
        for i in range(n):
            w, h = self.doc.page_size(i)
            clip = (0.0, 0.0, w, h)
            if box:
                c = (max(box[0], 0.0), max(box[1], 0.0), min(box[2], w), min(box[3], h))
                if c[2] - c[0] > 20 and c[3] - c[1] > 20:
                    clip = c
            self._clips.append(clip)
        self._sizes = [(c[2] - c[0], c[3] - c[1]) for c in self._clips]

    @property
    def _margin(self) -> int:
        return 0 if self.crop else MARGIN

    @property
    def _gap(self) -> int:
        return CROP_GAP if self.crop else GAP

    def set_crop(self, on: bool) -> None:
        """Trim the book's white margins and fit the text to the view width."""
        if on == self.crop or not self._tops:
            self.crop = on
            return
        # Keep the spot current_page() looks at (a third down the viewport) in place.
        third = self.viewport().height() / 3
        anchor = self.verticalScrollBar().value() + third
        page = max(bisect.bisect_right(self._tops, anchor) - 1, 0)
        _, top_pt, _, bottom_pt = self._clips[page]
        y_pt = min(top_pt + max(anchor - self._tops[page], 0.0) / self.zoom, bottom_pt)
        self.crop = on
        self._set_clips()
        self._cache.clear()
        self._popup.hide()
        self.fit_mode = True
        self.zoom = self._fit_width_zoom()
        self._relayout()
        _, top_pt, _, bottom_pt = self._clips[page]
        y = self._tops[page] + (min(max(y_pt, top_pt), bottom_pt) - top_pt) * self.zoom - third
        self.verticalScrollBar().setValue(int(y))
        self._check_page()
        self.zoomChanged.emit(self.zoom)

    def _fit_width_zoom(self) -> float:
        widest = max((w for w, _ in self._sizes), default=0)
        if not widest:
            return self.zoom
        avail = self.viewport().width() - 2 * self._margin
        return min(max(avail / widest, MIN_ZOOM), MAX_ZOOM)

    def fit_width(self) -> None:
        self.set_zoom(self._fit_width_zoom())
        self.fit_mode = True

    def saved_zoom(self) -> float:
        """Zoom to persist; 0 keeps the book in fit-width mode."""
        return 0.0 if self.fit_mode else self.zoom

    def _relayout(self) -> None:
        self._tops, y = [], float(self._gap)
        for _, h in self._sizes:
            self._tops.append(y)
            y += h * self.zoom + self._gap
        self._total_h = y
        self._max_w = max((w for w, _ in self._sizes), default=0) * self.zoom
        self._update_scrollbars()
        self.viewport().update()

    def _update_scrollbars(self) -> None:
        vp = self.viewport().size()
        v, h = self.verticalScrollBar(), self.horizontalScrollBar()
        v.setRange(0, max(0, int(self._total_h - vp.height())))
        v.setPageStep(vp.height())
        v.setSingleStep(40)
        h.setRange(0, max(0, int(self._max_w + 2 * self._margin - vp.width())))
        h.setPageStep(vp.width())
        h.setSingleStep(40)

    def resizeEvent(self, event) -> None:
        super().resizeEvent(event)
        if self.fit_mode and self._sizes:
            self.set_zoom(self._fit_width_zoom(), keep_fit=True)
        self._update_scrollbars()
        self._place_back_bar()

    def _page_rect(self, i: int) -> QRectF:
        """Page rectangle in viewport coordinates."""
        w, h = self._sizes[i][0] * self.zoom, self._sizes[i][1] * self.zoom
        vw = self.viewport().width()
        m = self._margin
        x = max((vw - w) / 2, m) if self._max_w + 2 * m <= vw else m + (self._max_w - w) / 2
        return QRectF(x - self.horizontalScrollBar().value(), self._tops[i] - self.verticalScrollBar().value(), w, h)

    def _visible_pages(self) -> range:
        if not self._tops:
            return range(0)
        top = self.verticalScrollBar().value()
        first = max(bisect.bisect_right(self._tops, top) - 1, 0)
        last = bisect.bisect_right(self._tops, top + self.viewport().height()) - 1
        return range(first, min(last, len(self._tops) - 1) + 1)

    # -- highlights ----------------------------------------------------------------
    def set_highlights(self, snippets: list[dict], notes: list[dict] = ()) -> None:
        """snippets: db rows (page, kind, rects, chat_id, ...); notes: db rows (page, rects, body, ...).
        Identical passages share one mark."""
        groups: dict[tuple, dict] = {}

        def group(row: dict, kind: str) -> dict:
            key = (row["page"], tuple(map(tuple, row["rects"])))
            return groups.setdefault(key, {"kind": kind, "rects": row["rects"], "snippets": [], "notes": []})

        for sn in snippets:
            group(sn, sn["kind"])["snippets"].append(sn)
        for note in notes:
            group(note, "text")["notes"].append(note)
        self._marks = {}
        for (page, _), mark in groups.items():
            self._marks.setdefault(page, []).append(mark)
        self._hover = []
        self.viewport().update()

    def _marks_at(self, pos: QPointF) -> list[dict]:
        hit = self._hit(pos)
        if hit is None:
            return []
        page, x, y = hit
        rect = self._page_rect(page)
        if not rect.contains(pos):
            return []
        return [
            m for m in self._marks.get(page, [])
            if any(r[0] - 1 <= x <= r[2] + 1 and r[1] - 1 <= y <= r[3] + 1 for r in m["rects"])
        ]

    # -- links -----------------------------------------------------------------------
    def _link_at(self, pos: QPointF) -> Link | None:
        hit = self._hit(pos)
        if hit is None or not self.doc or not self._page_rect(hit[0]).contains(pos):
            return None
        page, x, y = hit
        for link in self.doc.links(page):
            r = link.rect
            if r[0] - LINK_SLOP <= x <= r[2] + LINK_SLOP and r[1] - LINK_SLOP <= y <= r[3] + LINK_SLOP:
                return link
        return None

    def _link_tip(self, link: Link) -> str:
        text, _ = self.doc.link_target(link)
        if not text:
            return f"Go to page {link.page + 1}"
        # Rich text, so Qt wraps long footnotes.
        return f"<p style='white-space: pre-wrap'>{html.escape(text)}</p><p><i>Page {link.page + 1}</i></p>"

    def follow_link(self, link: Link) -> None:
        """Jump to a link's target and flash it; go_back() returns here."""
        self._back.append(self._position())
        _, rects = self.doc.link_target(link)
        if rects:
            self.flash(link.page, rects)
        else:
            self.go_to(link.page, self.doc.target(link)[1] or 0.001)
        self._back_bar.show()
        self._place_back_bar()

    def go_back(self) -> bool:
        """Return to where the last followed link was clicked; False if there is nowhere to go."""
        if not self._back:
            return False
        page, y_pt = self._back.pop()
        self._user_moved()
        self.verticalScrollBar().setValue(int(self._tops[page] + (y_pt - self._clips[page][1]) * self.zoom))
        self._check_page()
        self._back_bar.setVisible(bool(self._back))
        return True

    def _position(self) -> tuple[int, float]:
        """The page and y (in page points) at the top of the viewport."""
        top = self.verticalScrollBar().value()
        page = max(bisect.bisect_right(self._tops, top) - 1, 0)
        return page, self._clips[page][1] + (top - self._tops[page]) / self.zoom

    def _place_back_bar(self) -> None:
        self._back_bar.adjustSize()
        self._back_bar.move(12, self.viewport().height() - self._back_bar.height() - 12)
        self._back_bar.raise_()

    def snippet_from(self, sn: dict) -> dict:
        """A saved snippet, rebuilt in the form snippetChosen emits."""
        png = None
        if sn["kind"] == "image" and self.doc and sn["rects"]:
            png = self.doc.render(sn["page"], CAPTURE_SCALE, clip=tuple(sn["rects"][0])).tobytes("png")
        return {"kind": sn["kind"], "page": sn["page"], "text": sn["text"], "rects": sn["rects"], "png": png}

    # -- navigation --------------------------------------------------------------
    def current_page(self) -> int:
        if not self._tops:
            return 0
        y = self.verticalScrollBar().value() + self.viewport().height() / 3
        return max(bisect.bisect_right(self._tops, y) - 1, 0)

    def go_to(self, page: int, y_pt: float = 0.0) -> None:
        if not self._tops:
            return
        page = min(max(page, 0), len(self._tops) - 1)
        if y_pt == 0:
            y = self._tops[page] - self._gap
        else:
            y = self._tops[page] + (y_pt - self._clips[page][1]) * self.zoom - 60
        self._user_moved()
        self.verticalScrollBar().setValue(int(y))
        self._check_page()

    def _user_moved(self, *_) -> None:
        self._last_nav = time.monotonic()

    # -- reading aloud -------------------------------------------------------------
    def set_speech(self, sentence: dict, word: dict | None = None) -> None:
        """Highlight the sentence being read aloud and the word in it ({page: rects})."""
        self._speech = (sentence, word)
        self.viewport().update()

    def clear_speech(self) -> None:
        if self._speech:
            self._speech = None
            self.viewport().update()

    def follow_speech(self, rects: dict, force: bool = False) -> None:
        """Scroll so the first of ``rects`` is in view, unless the user has just scrolled."""
        if not rects or not self._tops or (not force and time.monotonic() - self._last_nav < FOLLOW_PAUSE):
            return
        page = min(rects)
        if not rects[page] or not 0 <= page < len(self._tops):
            return
        r = rects[page][0]
        y = self._tops[page] + (r[1] - self._clips[page][1]) * self.zoom  # in content pixels
        top = self.verticalScrollBar().value()
        h = self.viewport().height()
        if top + 0.12 * h <= y <= top + 0.8 * h - (r[3] - r[1]) * self.zoom:
            return
        self.verticalScrollBar().setValue(int(y - 0.3 * h))
        self._check_page()

    def _paint_speech(self, p: QPainter, page: int, rect: QRectF) -> None:
        if not self._speech:
            return
        sentence, word = self._speech
        dark = theme().pages_dark
        colors = SPEECH_DARK if dark else SPEECH_LIGHT
        p.save()
        p.setCompositionMode(
            QPainter.CompositionMode.CompositionMode_Screen if dark else QPainter.CompositionMode.CompositionMode_Multiply
        )
        for r in sentence.get(page, []):
            p.fillRect(self._to_view(page, rect, r).adjusted(-1, 0, 1, 0), colors[0])
        for r in (word or {}).get(page, []):
            p.fillRect(self._to_view(page, rect, r).adjusted(-1, 0, 1, 0), colors[1])
        p.restore()

    # -- find ----------------------------------------------------------------------
    def set_find(self, by_page: dict[int, list], current: tuple[int, list] | None) -> None:
        """Highlight find matches ({page: [rects of a match, ...]}) and the current one."""
        self._find, self._find_current = by_page, current
        self.viewport().update()

    def clear_find(self) -> None:
        if self._find or self._find_current:
            self._find, self._find_current = {}, None
            self.viewport().update()

    def show_match(self, page: int, rects: list) -> None:
        """Scroll a match into view, unless it already is."""
        if not rects or not 0 <= page < len(self._tops):
            return
        x0, y0 = min(r[0] for r in rects), min(r[1] for r in rects)
        x1, y1 = max(r[2] for r in rects), max(r[3] for r in rects)
        cx, cy = self._clips[page][:2]
        top = self._tops[page] + (y0 - cy) * self.zoom
        bottom = self._tops[page] + (y1 - cy) * self.zoom
        v, h = self.verticalScrollBar(), self.horizontalScrollBar()
        self._user_moved()
        if not (v.value() + 0.1 * self.viewport().height() <= top and bottom <= v.value() + 0.9 * self.viewport().height()):
            v.setValue(int(top - 0.35 * self.viewport().height()))
        if h.maximum() > 0:
            left = self._page_rect(page).x() + h.value() + (x0 - self._clips[page][0]) * self.zoom
            right = left + (x1 - x0) * self.zoom
            if left < h.value() or right > h.value() + self.viewport().width():
                h.setValue(int(left - 0.3 * self.viewport().width()))
        self._check_page()

    def _paint_find(self, p: QPainter, page: int, rect: QRectF) -> None:
        matches = self._find.get(page)
        current = self._find_current if self._find_current and self._find_current[0] == page else None
        if not matches and not current:
            return
        dark = theme().pages_dark
        normal, strong = FIND_DARK if dark else FIND_LIGHT
        p.save()
        p.setCompositionMode(
            QPainter.CompositionMode.CompositionMode_Screen if dark else QPainter.CompositionMode.CompositionMode_Multiply
        )
        for rects in matches or ():
            if current and rects is current[1]:
                continue
            for r in rects:
                p.fillRect(self._to_view(page, rect, r).adjusted(-1, 0, 1, 0), normal)
        for r in current[1] if current else ():
            p.fillRect(self._to_view(page, rect, r).adjusted(-1, 0, 1, 0), strong)
        p.restore()

    def set_zoom(self, zoom: float, keep_fit: bool = False) -> None:
        self.fit_mode = self.fit_mode and keep_fit
        zoom = min(max(zoom, MIN_ZOOM), MAX_ZOOM)
        if not self._tops or abs(zoom - self.zoom) < 1e-3:
            return
        # Keep the same spot of the same page at the top of the viewport.
        top = self.verticalScrollBar().value()
        page = max(bisect.bisect_right(self._tops, top) - 1, 0)
        offset_pt = (top - self._tops[page]) / self.zoom
        self.zoom = zoom
        self._popup.hide()
        self._relayout()
        self.verticalScrollBar().setValue(int(self._tops[page] + offset_pt * zoom))
        self.zoomChanged.emit(zoom)

    def zoom_in(self) -> None:
        self.set_zoom(self.zoom * 1.15)

    def zoom_out(self) -> None:
        self.set_zoom(self.zoom / 1.15)

    def flash(self, page: int, rects: list) -> None:
        """Scroll to a snippet's source and highlight it briefly."""
        self._user_moved()
        self.go_to(page, min((r[1] for r in rects), default=0.0) or 0.001)
        self._flash = (page, rects)
        self.viewport().update()
        QTimer.singleShot(1800, self._end_flash)

    def _end_flash(self) -> None:
        self._flash = None
        self.viewport().update()

    def scrollContentsBy(self, dx: int, dy: int) -> None:
        self.viewport().update()
        self._check_page()

    def _check_page(self) -> None:
        page = self.current_page()
        if page != self._page:
            self._page = page
            self.pageChanged.emit(page)

    def wheelEvent(self, event) -> None:
        if event.modifiers() & Qt.KeyboardModifier.ControlModifier:
            self.zoom_in() if event.angleDelta().y() > 0 else self.zoom_out()
            event.accept()
        else:
            self._user_moved()
            super().wheelEvent(event)

    # -- painting ------------------------------------------------------------------
    def _image(self, i: int) -> QImage:
        dpr = self.devicePixelRatioF()
        c = theme().colors if theme().pages_dark else None
        clip = self._clips[i] if self.crop else None
        key = (i, round(self.zoom, 3), dpr, clip, c and (c["page_bg"], c["page_fg"]))
        img = self._cache.get(key)
        if img is None:
            pix = self.doc.render(i, self.zoom * dpr, clip=clip)
            samples, stride = pix.samples, pix.stride
            if c:
                samples = darken_page(samples, pix.width, pix.height, stride, c["page_bg"], c["page_fg"])
                stride = pix.width * 3
            img = QImage(samples, pix.width, pix.height, stride, QImage.Format.Format_RGB888).copy()
            self._cache[key] = img
            while len(self._cache) > CACHE_PAGES:
                self._cache.popitem(last=False)
        else:
            self._cache.move_to_end(key)
        return img

    def _to_view(self, page: int, page_rect: QRectF, r) -> QRectF:
        """A rect in page points -> viewport coordinates."""
        z, (ox, oy, *_) = self.zoom, self._clips[page]
        return QRectF(
            page_rect.x() + (r[0] - ox) * z, page_rect.y() + (r[1] - oy) * z, (r[2] - r[0]) * z, (r[3] - r[1]) * z
        )

    def paintEvent(self, event) -> None:
        p = QPainter(self.viewport())
        p.fillRect(self.viewport().rect(), QColor(theme().colors["canvas"]))
        if not self.doc:
            return
        p.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform)
        for i in self._visible_pages():
            rect = self._page_rect(i)
            for spread, alpha in SHADOW:
                p.fillRect(rect.adjusted(-spread, -spread + 1, spread, spread + 1), QColor(0, 0, 0, alpha))
            p.drawImage(rect, self._image(i))
            self._paint_links(p, i, rect)
            self._paint_marks(p, i, rect)
            self._paint_speech(p, i, rect)
            self._paint_find(p, i, rect)
            for r in line_rects(self._selection.get(i, [])):
                p.fillRect(self._to_view(i, rect, r), SELECT_COLOR)
            if self._region and self._region[0] == i:
                vr = self._to_view(i, rect, self._region[1])
                p.fillRect(vr, SELECT_COLOR)
                p.setPen(QPen(SELECT_COLOR.darker(150), 1))
                p.drawRect(vr)
            if self._flash and self._flash[0] == i:
                for r in self._flash[1]:
                    p.fillRect(self._to_view(i, rect, r), FLASH_COLOR)

    def _paint_marks(self, p: QPainter, page: int, rect: QRectF) -> None:
        marks = self._marks.get(page)
        if not marks:
            return
        dark = theme().pages_dark
        p.save()
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        for m in marks:
            hover = any(m is h for h in self._hover)
            if m["kind"] == "image":
                # A frame just outside the region, so the figure itself stays untouched.
                p.setCompositionMode(QPainter.CompositionMode.CompositionMode_SourceOver)
                p.setPen(QPen(FRAME_COLOR[hover], 3 if hover else 2))
                p.setBrush(Qt.BrushStyle.NoBrush)
                for r in m["rects"]:
                    p.drawRoundedRect(self._to_view(page, rect, r).adjusted(-4, -4, 4, 4), 4, 4)
            else:
                p.setCompositionMode(
                    QPainter.CompositionMode.CompositionMode_Screen if dark
                    else QPainter.CompositionMode.CompositionMode_Multiply
                )
                if m["notes"]:
                    normal, hovered = NOTE_DARK if dark else NOTE_LIGHT
                else:
                    normal, hovered = MARK_DARK if dark else MARK_LIGHT
                for r in m["rects"]:
                    p.fillRect(self._to_view(page, rect, r).adjusted(-1, 0, 1, 0), hovered if hover else normal)
        p.restore()

    def _paint_links(self, p: QPainter, page: int, rect: QRectF) -> None:
        links = self.doc.links(page)
        if not links:
            return
        dark = theme().pages_dark
        p.save()
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        p.setPen(Qt.PenStyle.NoPen)
        p.setCompositionMode(
            QPainter.CompositionMode.CompositionMode_Screen if dark else QPainter.CompositionMode.CompositionMode_Multiply
        )
        for link in links:
            if link.quiet and link != self._hover_link:
                continue
            if link.footnote:
                colors = FOOTNOTE_DARK if dark else FOOTNOTE_LIGHT
            else:
                colors = XREF_DARK if dark else XREF_LIGHT
            r = self._to_view(page, rect, link.rect)
            # Tiny superscripts get a pill at least as wide as it is tall.
            pad = max(2.0, (r.height() - r.width()) / 2 + 1) if link.footnote else 2.0
            r = r.adjusted(-pad, -1, pad, 1)
            p.setBrush(colors[link == self._hover_link])
            p.drawRoundedRect(r, min(r.height() / 2, 6), min(r.height() / 2, 6))
        p.restore()

    # -- selection -------------------------------------------------------------------
    def set_region_mode(self, on: bool) -> None:
        self.region_mode = on
        self.clear_selection()
        self.viewport().setCursor(Qt.CursorShape.CrossCursor if on else Qt.CursorShape.IBeamCursor)

    def clear_selection(self) -> None:
        self._selection, self._region, self._anchor = {}, None, None
        self._popup.hide()
        self.viewport().update()

    def has_selection(self) -> bool:
        return bool(self._selection) or self._region is not None

    def _hit(self, pos: QPointF, page: int | None = None) -> tuple[int, float, float] | None:
        """Viewport position -> (page, x, y) in page points, clamped onto the page."""
        if not self._tops:
            return None
        if page is None:
            y = pos.y() + self.verticalScrollBar().value()
            page = max(bisect.bisect_right(self._tops, y) - 1, 0)
        rect = self._page_rect(page)
        x0, y0, x1, y1 = self._clips[page]
        x = min(max((pos.x() - rect.x()) / self.zoom + x0, x0), x1)
        y = min(max((pos.y() - rect.y()) / self.zoom + y0, y0), y1)
        return page, x, y

    def mousePressEvent(self, event) -> None:
        if event.button() == Qt.MouseButton.BackButton:
            self.go_back()
            return
        if event.button() != Qt.MouseButton.LeftButton or not self.doc:
            return
        self.clear_selection()
        self._anchor = self._hit(event.position())
        self._press = event.position()
        self._dragging = True

    def mouseMoveEvent(self, event) -> None:
        if not self._dragging:
            self._update_hover(event.position())
            return
        if not self._anchor:
            return
        if self.region_mode:
            page, ax, ay = self._anchor
            _, x, y = self._hit(event.position(), page)
            self._region = (page, (min(ax, x), min(ay, y), max(ax, x), max(ay, y)))
        else:
            self._selection = select_range(self.doc, self._anchor, self._hit(event.position()))
        self.viewport().update()

    def mouseReleaseEvent(self, event) -> None:
        if event.button() != Qt.MouseButton.LeftButton or not self._dragging:
            return
        self._dragging = False
        if not self.region_mode and self._press is not None and (
            (event.position() - self._press).manhattanLength() < CLICK_SLOP
        ):
            self.clear_selection()
            if link := self._link_at(event.position()):
                self._hover_link = None
                QToolTip.hideText()
                self.follow_link(link)
                return
            if self.speech_active:
                if hit := self._hit(event.position()):
                    self.speechClicked.emit(*hit)
                return
            marks = self._marks_at(event.position())
            if marks:
                snippets = [sn for m in marks for sn in m["snippets"]]
                notes = [n for m in marks for n in m["notes"]]
                self.highlightClicked.emit(snippets, notes, self.viewport().mapToGlobal(event.position().toPoint()))
            return
        if self._region:
            r = self._region[1]
            if (r[2] - r[0]) * self.zoom < 6 or (r[3] - r[1]) * self.zoom < 6:
                self._region = None
        if self.has_selection():
            self._show_popup(event.position().toPoint())
        self.viewport().update()

    def _update_hover(self, pos: QPointF) -> None:
        link = None if self.region_mode else self._link_at(pos)
        marks = [] if self.region_mode else self._marks_at(pos)
        left_link = self._hover_link is not None and link is None
        if link != self._hover_link:
            self._hover_link = link
            self.viewport().update()
            if link:
                # A link's target (usually a footnote) shows on hover, over any note's text.
                QToolTip.showText(self.viewport().mapToGlobal(pos.toPoint()), self._link_tip(link), self.viewport())
        if left_link or [id(m) for m in marks] != [id(m) for m in self._hover]:
            self._hover = marks
            self.viewport().update()
            # A note's text shows on hover, so a passage's note can be read without opening the panel.
            bodies = [n["body"].strip() for m in marks for n in m["notes"] if n["body"].strip()]
            if bodies and not link:
                tip = "\n\n".join(b if len(b) <= 600 else b[:600] + "…" for b in bodies)
                QToolTip.showText(self.viewport().mapToGlobal(pos.toPoint()), tip, self.viewport())
            elif not link:
                QToolTip.hideText()
        if not self.region_mode:
            pointing = link or marks
            self.viewport().setCursor(Qt.CursorShape.PointingHandCursor if pointing else Qt.CursorShape.IBeamCursor)

    def leaveEvent(self, event) -> None:
        if self._hover_link:
            self._hover_link = None
            self.viewport().update()
        if self._hover:
            self._hover = []
            self.viewport().update()
        super().leaveEvent(event)

    def _show_popup(self, pos: QPoint) -> None:
        self._copy_btn.setVisible(bool(self._selection))
        self._note_btn.setVisible(bool(self._selection))
        self._read_btn.setVisible(bool(self._selection))
        self._read_on_btn.setVisible(bool(self._selection))
        self._popup.adjustSize()
        vp = self.viewport().rect()
        x = min(max(pos.x() - self._popup.width() // 2, 4), vp.width() - self._popup.width() - 4)
        y = pos.y() + 14
        if y + self._popup.height() > vp.height() - 4:
            y = pos.y() - self._popup.height() - 14
        self._popup.move(x, max(y, 4))
        self._popup.show()
        self._popup.raise_()

    def selected_text(self) -> str:
        return "\n".join(words_text(self._selection[p]) for p in sorted(self._selection))

    def copy_selection(self) -> None:
        if self._selection:
            QGuiApplication.clipboard().setText(self.selected_text())
        self._popup.hide()

    def choose(self, new_chat: bool) -> None:
        """Hand the current selection or region to the chat panel."""
        if snippet := self.take_selection():
            self.snippetChosen.emit(snippet, new_chat)

    def choose_quiz(self) -> bool:
        """Ask for a quiz on the current selection or region; False if there is none."""
        if snippet := self.take_selection():
            self.quizChosen.emit(snippet)
        return snippet is not None

    def choose_note(self) -> bool:
        """Annotate the selected text; False if there is none (regions take no notes)."""
        snippet = self.take_selection() if self._selection else None
        if snippet:
            self.noteChosen.emit(snippet)
        return snippet is not None

    def choose_read(self, onward: bool) -> bool:
        """Read the selected text aloud (onward: and on from it); False if there is none."""
        if not self._selection:
            return False
        sel = dict(self._selection)
        self.clear_selection()
        self.readRequested.emit(sel, onward)
        return True

    def take_selection(self) -> dict | None:
        """The current selection or region as a snippet, clearing it."""
        if self._region:
            page, r = self._region
            png = self.doc.render(page, CAPTURE_SCALE, clip=r).tobytes("png")
            snippet = {"kind": "image", "page": page, "text": "", "rects": [list(r)], "png": png}
        elif self._selection:
            page = min(self._selection)
            snippet = {
                "kind": "text", "page": page, "text": self.selected_text(),
                "rects": [list(r) for r in line_rects(self._selection[page])], "png": None,
            }
        else:
            return None
        self.clear_selection()
        return snippet

    def keyPressEvent(self, event) -> None:
        if event.matches(QKeySequence.StandardKey.Copy):
            self.copy_selection()
        elif event.matches(QKeySequence.StandardKey.Back) or (
            event.key() == Qt.Key.Key_Left and event.modifiers() == Qt.KeyboardModifier.AltModifier
        ):
            self.go_back()
        elif event.key() == Qt.Key.Key_Escape:
            if self.has_selection():
                self.clear_selection()
            else:
                self.escapePressed.emit()
        else:
            super().keyPressEvent(event)
