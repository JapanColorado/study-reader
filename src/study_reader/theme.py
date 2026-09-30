"""Light/dark colors, the app stylesheet, line icons, and dark-page recoloring."""

from __future__ import annotations

import os
import tempfile
from pathlib import Path
from string import Template

import numpy as np
from PySide6.QtCore import QByteArray, QObject, QRectF, QSize, Qt, Signal
from PySide6.QtGui import QColor, QGuiApplication, QIcon, QIconEngine, QPainter, QPalette, QPixmap
from PySide6.QtSvg import QSvgRenderer
from PySide6.QtWidgets import QApplication

MODES = [("system", "Match system"), ("light", "Light"), ("dark", "Dark")]

# Keys are shared with transcript.html, which receives them as CSS variables.
LIGHT = {
    "window": "#f3f1ec", "surface": "#fbfaf8", "raised": "#ffffff", "fg": "#1f1e1c",
    "muted": "#6b6862", "faint": "#a19d95", "border": "#dedad2", "hover": "#ebe8e1",
    "accent": "#b8562f", "accent_hover": "#a24a27", "accent_fg": "#ffffff", "accent_soft": "#f4e2d8",
    "canvas": "#e3e0d9", "user": "#efece6", "code": "#f1eee8", "error": "#c0392b", "scroll": "#cfcac1",
    "page_bg": "#ffffff", "page_fg": "#000000",
}
DARK = {
    "window": "#1a1918", "surface": "#1f1e1d", "raised": "#292725", "fg": "#ece9e2",
    "muted": "#a09c94", "faint": "#6c6862", "border": "#363330", "hover": "#2e2c2a",
    "accent": "#e08a63", "accent_hover": "#eb9c78", "accent_fg": "#1c1410", "accent_soft": "#3d2b22",
    "canvas": "#131211", "user": "#2d2b29", "code": "#2a2826", "error": "#ec7063", "scroll": "#45413d",
    "page_bg": "#201e1c", "page_fg": "#dcd7ce",
}

# Lucide icons (ISC license), 24x24 stroke paths.
ICONS = {
    "panel-left": '<rect width="18" height="18" x="3" y="3" rx="2"/><path d="M9 3v18"/>',
    "panel-right": '<rect width="18" height="18" x="3" y="3" rx="2"/><path d="M15 3v18"/>',
    "library": '<path d="m16 6 4 14"/><path d="M12 6v14"/><path d="M8 8v12"/><path d="M4 4v16"/>',
    "folder-open": '<path d="m6 14 1.5-2.9A2 2 0 0 1 9.24 10H20a2 2 0 0 1 1.94 2.5l-1.54 6a2 2 0 0 1-1.95 1.5H4'
                   'a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h3.9a2 2 0 0 1 1.69.9l.81 1.2a2 2 0 0 0 1.67.9H18a2 2 0 0 1 2 2v2"/>',
    "zoom-in": '<circle cx="11" cy="11" r="8"/><path d="m21 21-4.35-4.35"/><path d="M11 8v6"/><path d="M8 11h6"/>',
    "zoom-out": '<circle cx="11" cy="11" r="8"/><path d="m21 21-4.35-4.35"/><path d="M8 11h6"/>',
    "fit-width": '<path d="m18 8 4 4-4 4"/><path d="m6 8-4 4 4 4"/><path d="M2 12h20"/>',
    "crop": '<path d="M6 2v14a2 2 0 0 0 2 2h14"/><path d="M18 22V8a2 2 0 0 0-2-2H2"/>',
    "scan": '<path d="M3 7V5a2 2 0 0 1 2-2h2"/><path d="M17 3h2a2 2 0 0 1 2 2v2"/><path d="M21 17v2a2 2 0 0 1-2 2h-2"/>'
            '<path d="M7 21H5a2 2 0 0 1-2-2v-2"/><rect width="10" height="8" x="7" y="8" rx="1"/>',
    "message-plus": '<path d="M21 15a2 2 0 0 1-2 2H7l-4 4V5a2 2 0 0 1 2-2h14a2 2 0 0 1 2 2z"/>'
                    '<path d="M12 7v6"/><path d="M9 10h6"/>',
    "message": '<path d="M21 15a2 2 0 0 1-2 2H7l-4 4V5a2 2 0 0 1 2-2h14a2 2 0 0 1 2 2z"/>',
    "sun": '<circle cx="12" cy="12" r="4"/><path d="M12 2v2"/><path d="M12 20v2"/><path d="m4.93 4.93 1.41 1.41"/>'
           '<path d="m17.66 17.66 1.41 1.41"/><path d="M2 12h2"/><path d="M20 12h2"/><path d="m6.34 17.66-1.41 1.41"/>'
           '<path d="m19.07 4.93-1.41 1.41"/>',
    "moon": '<path d="M12 3a6 6 0 0 0 9 9 9 9 0 1 1-9-9Z"/>',
    "settings": '<path d="M21 4h-7"/><path d="M10 4H3"/><path d="M21 12h-9"/><path d="M8 12H3"/><path d="M21 20h-5"/>'
                '<path d="M12 20H3"/><path d="M14 2v4"/><path d="M8 10v4"/><path d="M16 18v4"/>',
    "arrow-up": '<path d="m5 12 7-7 7 7"/><path d="M12 19V5"/>',
    "x": '<path d="M18 6 6 18"/><path d="m6 6 12 12"/>',
    "text": '<path d="M17 6.1H3"/><path d="M21 12.1H3"/><path d="M15.1 18H3"/>',
    "image": '<rect width="18" height="18" x="3" y="3" rx="2"/><circle cx="9" cy="9" r="2"/>'
             '<path d="m21 15-3.086-3.086a2 2 0 0 0-2.828 0L6 21"/>',
    "chevron-down": '<path d="m6 9 6 6 6-6"/>',
    "chevron-right": '<path d="m9 18 6-6-6-6"/>',
    "calculator": '<rect width="16" height="20" x="4" y="2" rx="2"/><path d="M8 6h8"/><path d="M16 14v4"/>'
                  '<path d="M16 10h.01"/><path d="M12 10h.01"/><path d="M8 10h.01"/><path d="M12 14h.01"/>'
                  '<path d="M8 14h.01"/><path d="M12 18h.01"/><path d="M8 18h.01"/>',
    "trash": '<path d="M3 6h18"/><path d="M19 6v14c0 1-1 2-2 2H7c-1 0-2-1-2-2V6"/><path d="M8 6V4c0-1 1-2 2-2h4c1 0 2 1 2 2v2"/>',
    "help": '<circle cx="12" cy="12" r="10"/><path d="M9.09 9a3 3 0 0 1 5.83 1c0 2-3 3-3 3"/><path d="M12 17h.01"/>',
}


def _svg(name: str, color: str, width: float = 1.75) -> bytes:
    return (
        f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24" fill="none" stroke="{color}" '
        f'stroke-width="{width}" stroke-linecap="round" stroke-linejoin="round">{ICONS[name]}</svg>'
    ).encode()


class _LineIcon(QIconEngine):
    """Renders at paint time in the current theme's colors, so icons follow theme changes."""

    _cache: dict[tuple, QPixmap] = {}

    def __init__(self, name: str, role: str):
        super().__init__()
        self.name, self.role = name, role

    def clone(self) -> QIconEngine:
        return _LineIcon(self.name, self.role)

    def _color(self, mode: QIcon.Mode, state: QIcon.State) -> str:
        c = theme().colors
        if mode == QIcon.Mode.Disabled:
            return c["faint"]
        if state == QIcon.State.On and self.role == "fg":
            return c["accent"]
        return c[self.role]

    def scaledPixmap(self, size: QSize, mode: QIcon.Mode, state: QIcon.State, scale: float) -> QPixmap:
        color = self._color(mode, state)
        key = (self.name, color, size.width(), size.height(), scale)
        pm = self._cache.get(key)
        if pm is None:
            pm = QPixmap(size * scale)
            pm.fill(Qt.GlobalColor.transparent)
            p = QPainter(pm)
            QSvgRenderer(QByteArray(_svg(self.name, color))).render(p)
            p.end()
            pm.setDevicePixelRatio(scale)
            self._cache[key] = pm
        return pm

    def pixmap(self, size: QSize, mode: QIcon.Mode, state: QIcon.State) -> QPixmap:
        return self.scaledPixmap(size, mode, state, 1.0)

    def paint(self, painter: QPainter, rect, mode: QIcon.Mode, state: QIcon.State) -> None:
        pm = self.scaledPixmap(rect.size(), mode, state, painter.device().devicePixelRatioF())
        painter.drawPixmap(rect, pm)


def icon(name: str, role: str = "fg") -> QIcon:
    """role: a color key; "fg" icons turn accent-colored when checked."""
    return QIcon(_LineIcon(name, role))


def _icon_file(name: str, color: str) -> str:
    """Stylesheets can only take images from files."""
    folder = Path(tempfile.gettempdir()) / f"study-reader-{os.getuid()}"
    folder.mkdir(exist_ok=True)
    path = folder / f"{name}-{color.lstrip('#')}.svg"
    if not path.exists():
        path.write_bytes(_svg(name, color, 2))
    return path.as_posix()


# -- dark pages ---------------------------------------------------------------------
def darken_page(samples: bytes, width: int, height: int, stride: int, bg: str, fg: str) -> bytes:
    """RGB888 page pixels -> white paper becomes bg and black ink becomes fg, keeping hues.

    Inverts HSL lightness (c + 255 - max - min), which leaves hue and saturation alone
    and always stays within 0..255, then maps each channel onto bg..fg.
    """
    rgb = np.frombuffer(samples, np.uint8).reshape(height, stride)[:, : width * 3].reshape(height, width, 3)
    r, g, b = rgb[..., 0], rgb[..., 1], rgb[..., 2]
    shift = 255 - np.maximum(np.maximum(r, g), b).astype(np.int16) - np.minimum(np.minimum(r, g), b)
    lo, hi = (np.array(QColor(c).getRgb()[:3], np.float32) for c in (bg, fg))
    lut = (lo[:, None] + np.arange(256, dtype=np.float32) / 255 * (hi - lo)[:, None]).round().astype(np.uint8)
    out = np.empty_like(rgb)
    for k in range(3):
        out[..., k] = lut[k][(rgb[..., k] + shift).astype(np.uint8)]
    return out.tobytes()


# -- application theme -------------------------------------------------------------
def _palette(c: dict) -> QPalette:
    R = QPalette.ColorRole
    p = QPalette()
    for role, key in {
        R.Window: "window", R.WindowText: "fg", R.Base: "surface", R.AlternateBase: "hover",
        R.Text: "fg", R.Button: "raised", R.ButtonText: "fg", R.BrightText: "accent",
        R.Highlight: "accent", R.HighlightedText: "accent_fg", R.ToolTipBase: "raised",
        R.ToolTipText: "fg", R.PlaceholderText: "faint", R.Link: "accent", R.LinkVisited: "accent",
        R.Light: "raised", R.Midlight: "hover", R.Mid: "border", R.Dark: "border",
    }.items():
        p.setColor(role, QColor(c[key]))
    for role in (R.WindowText, R.Text, R.ButtonText):
        p.setColor(QPalette.ColorGroup.Disabled, role, QColor(c["faint"]))
    return p


STYLESHEET = Template("""
QMainWindow, QDialog { background: $window; }
QToolBar { background: $window; border: none; border-bottom: 1px solid $border; padding: 5px 8px; spacing: 2px; }
QToolBar::separator { background: $border; width: 1px; margin: 5px 7px; }
QToolButton { background: transparent; color: $fg; border: 1px solid transparent; border-radius: 6px; padding: 4px; }
QToolButton:hover { background: $hover; }
QToolButton:pressed { background: $border; }
QToolButton:checked { background: $accent_soft; color: $accent; }
QToolButton:disabled { color: $faint; }
QToolButton:checked:disabled { background: transparent; }
QToolButton::menu-indicator { image: none; width: 0; }
QToolButton#send { background: $accent; border: none; border-radius: 15px; padding: 0; }
QToolButton#send:hover { background: $accent_hover; }
QToolButton#send:disabled { background: $border; }
QToolButton#section { font-weight: 600; color: $muted; padding: 3px 6px 3px 2px; }

QPushButton { background: $raised; color: $fg; border: 1px solid $border; border-radius: 7px; padding: 5px 12px; }
QPushButton:hover { background: $hover; }
QPushButton:pressed { background: $border; }
QPushButton:disabled { color: $faint; }
QPushButton[primary="true"] { background: $accent; color: $accent_fg; border-color: $accent; }
QPushButton[primary="true"]:hover { background: $accent_hover; border-color: $accent_hover; }

QComboBox { background: transparent; color: $fg; border: 1px solid $border; border-radius: 6px; padding: 3px 6px 3px 9px; }
QComboBox:hover { background: $hover; }
QComboBox:disabled { color: $faint; }
QComboBox::drop-down { border: none; width: 18px; }
QComboBox::down-arrow { image: url($chevron_down); width: 11px; height: 11px; }
QComboBox QAbstractItemView { background: $raised; color: $fg; border: 1px solid $border; outline: 0; padding: 4px;
                              selection-background-color: $hover; selection-color: $fg; }

QSpinBox { background: $surface; color: $fg; border: 1px solid $border; border-radius: 6px; padding: 3px 4px; }
QSpinBox:focus { border-color: $accent; }
QSpinBox:disabled { color: $faint; }

QListView, QTreeView { background: transparent; color: $fg; border: none; outline: 0; }
QListView::item, QTreeView::item { padding: 5px 6px; border-radius: 6px; }
QListView::item:hover, QTreeView::item:hover { background: $hover; }
QListView::item:selected, QTreeView::item:selected { background: $accent_soft; color: $fg; }
QListView#library::item, QListView#library::item:hover, QListView#library::item:selected { background: transparent; padding: 0; }
QListView QLineEdit { background: $raised; color: $fg; border: 1px solid $accent; border-radius: 5px; padding: 0 4px;
                     selection-background-color: $accent_soft; selection-color: $fg; }
QTreeView::branch { background: transparent; }
QTreeView::branch:has-children:closed { image: url($chevron_right); }
QTreeView::branch:has-children:open { image: url($chevron_down); }

QSplitter::handle { background: $border; }
QSplitter::handle:horizontal { width: 1px; }
QSplitter::handle:vertical { height: 1px; }
QSplitter::handle:hover { background: $accent; }

QScrollBar:vertical { background: transparent; width: 11px; margin: 0; }
QScrollBar:horizontal { background: transparent; height: 11px; margin: 0; }
QScrollBar::handle { background: $scroll; border-radius: 3px; margin: 3px; }
QScrollBar::handle:vertical { min-height: 32px; }
QScrollBar::handle:horizontal { min-width: 32px; }
QScrollBar::handle:hover { background: $faint; }
QScrollBar::add-line, QScrollBar::sub-line { width: 0; height: 0; }
QScrollBar::add-page, QScrollBar::sub-page { background: none; }

QMenu { background: $raised; color: $fg; border: 1px solid $border; border-radius: 8px; padding: 4px; }
QMenu::item { padding: 5px 18px 5px 8px; border-radius: 5px; }
QMenu::item:selected { background: $hover; }
QMenu::item:disabled { color: $faint; }
QMenu::icon { padding-left: 6px; }
QMenu::separator { height: 1px; background: $border; margin: 4px 6px; }
QToolTip { background: $raised; color: $fg; border: 1px solid $border; padding: 4px 6px; }

QWidget#sidebar { background: $window; }
QWidget#chatPanel { background: $surface; }
QWidget#library { background: $window; }
QLabel[role="muted"] { color: $muted; }
QLabel[role="hint"] { color: $faint; font-size: 8pt; }
QLabel[role="heading"] { font-size: 17pt; font-weight: 600; }
QLabel[role="section"] { color: $muted; font-weight: 600; padding: 2px 4px; }
QLabel[role="page"] { color: $accent; font-weight: 600; }

QFrame#composer { background: $raised; border: 1px solid $border; border-radius: 12px; }
QFrame#composer[focused="true"] { border-color: $accent; }
QPlainTextEdit#composerInput { background: transparent; color: $fg; border: none; }
QFrame#chip { background: $accent_soft; border-radius: 8px; }
QFrame#chip QLabel { font-size: 9pt; }
QLineEdit#calcInput { background: transparent; color: $fg; border: none; font-size: 12pt; padding: 2px 0; }
QLabel#calcResult { font-size: 13pt; font-weight: 600; }
QLabel[role="error"] { color: $error; }
QToolButton#angle { color: $muted; font-size: 8pt; font-weight: 600; padding: 4px 6px; }
QListView#calcHistory::item, QListView#calcHistory::item:selected { background: transparent; padding: 0; }
QListView#calcHistory::item:hover { background: $hover; }
QFrame#popup { background: $raised; border: 1px solid $border; border-radius: 10px; }
""")


class Theme(QObject):
    changed = Signal()

    def __init__(self):
        super().__init__()
        self.mode = "system"
        self.dark_pages = False
        self.dark = False
        self.colors = LIGHT
        QApplication.setStyle("Fusion")
        QGuiApplication.styleHints().colorSchemeChanged.connect(self._apply)

    def configure(self, mode: str | None = None, dark_pages: bool | None = None) -> None:
        if mode is not None:
            self.mode = mode if mode in dict(MODES) else "system"
        if dark_pages is not None:
            self.dark_pages = dark_pages
        self._apply()

    @property
    def pages_dark(self) -> bool:
        return self.dark and self.dark_pages

    def _apply(self) -> None:
        system_dark = QGuiApplication.styleHints().colorScheme() == Qt.ColorScheme.Dark
        self.dark = self.mode == "dark" or (self.mode == "system" and system_dark)
        self.colors = c = DARK if self.dark else LIGHT
        app = QApplication.instance()
        app.setPalette(_palette(c))
        app.setStyleSheet(STYLESHEET.substitute(
            c, chevron_down=_icon_file("chevron-down", c["muted"]), chevron_right=_icon_file("chevron-right", c["muted"]),
        ))
        self.changed.emit()


_theme: Theme | None = None


def theme() -> Theme:
    global _theme
    if _theme is None:
        _theme = Theme()
    return _theme


def rounded(rect: QRectF, inset: float = 0.5) -> QRectF:
    """Rect adjusted so a 1px pen lands on whole pixels."""
    return rect.adjusted(inset, inset, -inset, -inset)
