"""Continuous-scroll page view with text selection and screenshot-region capture."""

from __future__ import annotations

import bisect
from collections import OrderedDict

from PySide6.QtCore import QPoint, QPointF, QRectF, Qt, QTimer, Signal
from PySide6.QtGui import QColor, QGuiApplication, QImage, QKeySequence, QPainter, QPen
from PySide6.QtWidgets import QAbstractScrollArea, QFrame, QHBoxLayout, QPushButton

from .document import Document, Word, line_rects, select_range, words_text

GAP = 14
MARGIN = 16
CACHE_PAGES = 10
CAPTURE_SCALE = 2.0
MIN_ZOOM, MAX_ZOOM = 0.4, 4.0

SELECT_COLOR = QColor(60, 130, 230, 90)
FLASH_COLOR = QColor(255, 200, 40, 110)


class ReaderView(QAbstractScrollArea):
    # dict(kind, page, text, rects, png) and whether it should start a new chat
    snippetChosen = Signal(dict, bool)
    pageChanged = Signal(int)
    zoomChanged = Signal(float)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.doc: Document | None = None
        self.zoom = 1.25
        self.region_mode = False
        self.fit_mode = True
        self._sizes: list[tuple[float, float]] = []
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

        self.viewport().setMouseTracking(False)
        self.viewport().setCursor(Qt.CursorShape.IBeamCursor)
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)

        self._popup = QFrame(self.viewport())
        self._popup.setFrameShape(QFrame.Shape.StyledPanel)
        self._popup.setAutoFillBackground(True)
        self._popup.setCursor(Qt.CursorShape.ArrowCursor)
        row = QHBoxLayout(self._popup)
        row.setContentsMargins(4, 4, 4, 4)
        row.setSpacing(4)
        ask = QPushButton("Ask in new chat")
        ask.setDefault(True)
        ask.clicked.connect(lambda: self.choose(True))
        add = QPushButton("Add to current chat")
        add.clicked.connect(lambda: self.choose(False))
        self._copy_btn = QPushButton("Copy")
        self._copy_btn.clicked.connect(self.copy_selection)
        for b in (ask, add, self._copy_btn):
            row.addWidget(b)
        self._popup.hide()

    # -- document / layout -----------------------------------------------------
    def set_document(self, doc: Document | None, page: int = 0, zoom: float | None = None) -> None:
        self.doc = doc
        self._cache.clear()
        self.clear_selection()
        self._sizes = [doc.page_size(i) for i in range(doc.page_count)] if doc else []
        # zoom <= 0 (or None) means "fit width", the default for new books.
        self.fit_mode = not (zoom and zoom > 0)
        self.zoom = self._fit_width_zoom() if self.fit_mode else min(max(zoom, MIN_ZOOM), MAX_ZOOM)
        self._relayout()
        self.go_to(page)
        self.zoomChanged.emit(self.zoom)

    def _fit_width_zoom(self) -> float:
        widest = max((w for w, _ in self._sizes), default=0)
        if not widest:
            return self.zoom
        avail = self.viewport().width() - 2 * MARGIN
        return min(max(avail / widest, MIN_ZOOM), MAX_ZOOM)

    def fit_width(self) -> None:
        self.set_zoom(self._fit_width_zoom())
        self.fit_mode = True

    def saved_zoom(self) -> float:
        """Zoom to persist; 0 keeps the book in fit-width mode."""
        return 0.0 if self.fit_mode else self.zoom

    def _relayout(self) -> None:
        self._tops, y = [], float(GAP)
        for _, h in self._sizes:
            self._tops.append(y)
            y += h * self.zoom + GAP
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
        h.setRange(0, max(0, int(self._max_w + 2 * MARGIN - vp.width())))
        h.setPageStep(vp.width())
        h.setSingleStep(40)

    def resizeEvent(self, event) -> None:
        super().resizeEvent(event)
        if self.fit_mode and self._sizes:
            self.set_zoom(self._fit_width_zoom(), keep_fit=True)
        self._update_scrollbars()

    def _page_rect(self, i: int) -> QRectF:
        """Page rectangle in viewport coordinates."""
        w, h = self._sizes[i][0] * self.zoom, self._sizes[i][1] * self.zoom
        vw = self.viewport().width()
        x = max((vw - w) / 2, MARGIN) if self._max_w + 2 * MARGIN <= vw else MARGIN + (self._max_w - w) / 2
        return QRectF(x - self.horizontalScrollBar().value(), self._tops[i] - self.verticalScrollBar().value(), w, h)

    def _visible_pages(self) -> range:
        if not self._tops:
            return range(0)
        top = self.verticalScrollBar().value()
        first = max(bisect.bisect_right(self._tops, top) - 1, 0)
        last = bisect.bisect_right(self._tops, top + self.viewport().height()) - 1
        return range(first, min(last, len(self._tops) - 1) + 1)

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
        self.verticalScrollBar().setValue(int(self._tops[page] + y_pt * self.zoom - (GAP if y_pt == 0 else 60)))
        self._check_page()

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
            super().wheelEvent(event)

    # -- painting ------------------------------------------------------------------
    def _image(self, i: int) -> QImage:
        dpr = self.devicePixelRatioF()
        key = (i, round(self.zoom, 3), dpr)
        img = self._cache.get(key)
        if img is None:
            pix = self.doc.render(i, self.zoom * dpr)
            img = QImage(pix.samples, pix.width, pix.height, pix.stride, QImage.Format.Format_RGB888).copy()
            self._cache[key] = img
            while len(self._cache) > CACHE_PAGES:
                self._cache.popitem(last=False)
        else:
            self._cache.move_to_end(key)
        return img

    def _to_view(self, page_rect: QRectF, r) -> QRectF:
        z = self.zoom
        return QRectF(page_rect.x() + r[0] * z, page_rect.y() + r[1] * z, (r[2] - r[0]) * z, (r[3] - r[1]) * z)

    def paintEvent(self, event) -> None:
        p = QPainter(self.viewport())
        p.fillRect(self.viewport().rect(), self.palette().color(self.backgroundRole()).darker(115))
        if not self.doc:
            return
        p.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform)
        for i in self._visible_pages():
            rect = self._page_rect(i)
            p.drawImage(rect, self._image(i))
            for r in line_rects(self._selection.get(i, [])):
                p.fillRect(self._to_view(rect, r), SELECT_COLOR)
            if self._region and self._region[0] == i:
                vr = self._to_view(rect, self._region[1])
                p.fillRect(vr, SELECT_COLOR)
                p.setPen(QPen(SELECT_COLOR.darker(150), 1))
                p.drawRect(vr)
            if self._flash and self._flash[0] == i:
                for r in self._flash[1]:
                    p.fillRect(self._to_view(rect, r), FLASH_COLOR)

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
        w, h = self._sizes[page]
        x = min(max((pos.x() - rect.x()) / self.zoom, 0.0), w)
        y = min(max((pos.y() - rect.y()) / self.zoom, 0.0), h)
        return page, x, y

    def mousePressEvent(self, event) -> None:
        if event.button() != Qt.MouseButton.LeftButton or not self.doc:
            return
        self.clear_selection()
        self._anchor = self._hit(event.position())
        self._dragging = True

    def mouseMoveEvent(self, event) -> None:
        if not self._dragging or not self._anchor:
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
        if self._region:
            r = self._region[1]
            if (r[2] - r[0]) * self.zoom < 6 or (r[3] - r[1]) * self.zoom < 6:
                self._region = None
        if self.has_selection():
            self._show_popup(event.position().toPoint())
        self.viewport().update()

    def _show_popup(self, pos: QPoint) -> None:
        self._copy_btn.setVisible(bool(self._selection))
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
            return
        self.clear_selection()
        self.snippetChosen.emit(snippet, new_chat)

    def keyPressEvent(self, event) -> None:
        if event.matches(QKeySequence.StandardKey.Copy):
            self.copy_selection()
        elif event.key() == Qt.Key.Key_Escape:
            self.clear_selection()
        else:
            super().keyPressEvent(event)
