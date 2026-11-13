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
    "window": "#f4f1ea", "surface": "#faf8f4", "raised": "#ffffff", "fg": "#1d1b18",
    "muted": "#69645c", "faint": "#a39e95", "border": "#e2ddd3", "hover": "#ece8df",
    "accent": "#b9532b", "accent_hover": "#a34723", "accent_fg": "#ffffff", "accent_soft": "#f6e4da",
    "canvas": "#e6e2da", "user": "#efebe4", "code": "#f2efe9", "error": "#c0392b", "scroll": "#d2ccc2",
    "success": "#2f8a57", "warning": "#a87a0a", "new": "#2f6db5",
    "success_soft": "#e1f0e6", "new_soft": "#e3ecf8", "rail": "#ebe7de", "shadow": "rgba(60,45,30,0.10)",
    "page_bg": "#ffffff", "page_fg": "#000000",
}
DARK = {
    "window": "#171615", "surface": "#1c1b19", "raised": "#22201e", "fg": "#efebe4",
    "muted": "#a8a298", "faint": "#6f6a63", "border": "#312e2a", "hover": "#2b2926",
    "accent": "#e5895f", "accent_hover": "#efa07c", "accent_fg": "#1d130e", "accent_soft": "#3a2a21",
    "canvas": "#121110", "user": "#2a2826", "code": "#272522", "error": "#ef7a6c", "scroll": "#433f3a",
    "success": "#74c69d", "warning": "#e3be5c", "new": "#7fb0e8",
    "success_soft": "#1f3128", "new_soft": "#1f2a38", "rail": "#121110", "shadow": "rgba(0,0,0,0.35)",
    "page_bg": "#201e1c", "page_fg": "#dcd7ce",
}

# Lucide icons (ISC license), 24x24 stroke paths.
ICONS = {
    "panel-left": '<rect width="18" height="18" x="3" y="3" rx="2"/><path d="M9 3v18"/>',
    "globe": '<circle cx="12" cy="12" r="10"/><path d="M12 2a14.5 14.5 0 0 0 0 20 14.5 14.5 0 0 0 0-20"/>'
             '<path d="M2 12h20"/>',
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
    "stop": '<rect width="10" height="10" x="7" y="7" rx="1.5"/>',
    "ghost": '<path d="M9 10h.01"/><path d="M15 10h.01"/>'
             '<path d="M12 2a8 8 0 0 0-8 8v12l3-3 2.5 2.5L12 19l2.5 2.5L17 19l3 3V10a8 8 0 0 0-8-8z"/>',
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
    "quiz": '<path d="M9 11l2 2 4-4"/><rect width="16" height="18" x="4" y="3" rx="2"/><path d="M9 17h6"/>',
    "check-circle": '<circle cx="12" cy="12" r="10"/><path d="m9 12 2 2 4-4"/>',
    "help": '<circle cx="12" cy="12" r="10"/><path d="M9.09 9a3 3 0 0 1 5.83 1c0 2-3 3-3 3"/><path d="M12 17h.01"/>',
    "note": '<path d="M15.5 3H5a2 2 0 0 0-2 2v14c0 1.1.9 2 2 2h14a2 2 0 0 0 2-2V8.5L15.5 3Z"/><path d="M15 3v6h6"/>',
    "cards": '<rect width="14" height="16" x="7" y="5" rx="2"/><path d="M4 16V5a2 2 0 0 1 2-2h9"/>',
    "chart": '<path d="M3 3v18h18"/><path d="M18 17V9"/><path d="M13 17V5"/><path d="M8 17v-3"/>',
    "sparkles": '<path d="M9.94 15.5A2 2 0 0 0 8.5 14.06l-6.14-1.58a.5.5 0 0 1 0-.96L8.5 9.94A2 2 0 0 0 9.94 8.5l1.58-6.14'
                'a.5.5 0 0 1 .96 0L14.06 8.5A2 2 0 0 0 15.5 9.94l6.14 1.58a.5.5 0 0 1 0 .96L15.5 14.06a2 2 0 0 0-1.44 1.44'
                'l-1.58 6.14a.5.5 0 0 1-.96 0z"/><path d="M20 3v4"/><path d="M22 5h-4"/>',
    "search": '<circle cx="11" cy="11" r="8"/><path d="m21 21-4.3-4.3"/>',
    "import": '<path d="M12 3v12"/><path d="m8 11 4 4 4-4"/>'
              '<path d="M8 5H4a2 2 0 0 0-2 2v10a2 2 0 0 0 2 2h16a2 2 0 0 0 2-2V7a2 2 0 0 0-2-2h-4"/>',
    "home": '<path d="M15 21v-8a1 1 0 0 0-1-1h-4a1 1 0 0 0-1 1v8"/>'
            '<path d="M3 10a2 2 0 0 1 .709-1.528l7-6a2 2 0 0 1 2.582 0l7 6A2 2 0 0 1 21 10v9a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2z"/>',
    "book-open": '<path d="M12 7v14"/><path d="M3 18a1 1 0 0 1-1-1V4a1 1 0 0 1 1-1h5a4 4 0 0 1 4 4 4 4 0 0 1 4-4h5'
                 'a1 1 0 0 1 1 1v13a1 1 0 0 1-1 1h-6a3 3 0 0 0-3 3 3 3 0 0 0-3-3z"/>',
    "play": '<polygon points="6 3 20 12 6 21 6 3"/>',
    "plus": '<path d="M5 12h14"/><path d="M12 5v14"/>',
    "brain": '<path d="M12 5a3 3 0 1 0-5.997.125 4 4 0 0 0-2.526 5.77 4 4 0 0 0 .556 6.588A4 4 0 1 0 12 18Z"/>'
             '<path d="M12 5a3 3 0 1 1 5.997.125 4 4 0 0 1 2.526 5.77 4 4 0 0 1-.556 6.588A4 4 0 1 1 12 18Z"/>'
             '<path d="M15 13a4.5 4.5 0 0 1-3-4 4.5 4.5 0 0 1-3 4"/><path d="M12 18v-5"/>',
    "download": '<path d="M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4"/><path d="m7 10 5 5 5-5"/><path d="M12 15V3"/>',
    "cloud": '<path d="M17.5 19H9a7 7 0 1 1 6.71-9h1.79a4.5 4.5 0 1 1 0 9Z"/>',
    "pause": '<rect x="14" y="4" width="4" height="16" rx="1"/><rect x="6" y="4" width="4" height="16" rx="1"/>',
    "audio-lines": '<path d="M2 10v3"/><path d="M6 6v11"/><path d="M10 3v18"/><path d="M14 8v7"/>'
                   '<path d="M18 5v13"/><path d="M22 10v3"/>',
    "skip-back": '<polygon points="19 20 9 12 19 4 19 20"/><line x1="5" x2="5" y1="19" y2="5"/>',
    "skip-forward": '<polygon points="5 4 15 12 5 20 5 4"/><line x1="19" x2="19" y1="5" y2="19"/>',
    "volume": '<path d="M11 4.702a.705.705 0 0 0-1.203-.498L6.413 7.587A1.4 1.4 0 0 1 5.416 8H3a1 1 0 0 0-1 1v6'
              'a1 1 0 0 0 1 1h2.416a1.4 1.4 0 0 1 .997.413l3.383 3.384A.705.705 0 0 0 11 19.298z"/>'
              '<path d="M16 9a5 5 0 0 1 0 6"/><path d="M19.364 18.364a9 9 0 0 0 0-12.728"/>',
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
        if state == QIcon.State.On and self.role in ("fg", "nav"):
            return c["accent"]
        if self.role == "nav":  # the navigation rail: quiet until chosen
            return c["muted"]
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
QToolBar { background: $window; border: none; border-bottom: 1px solid $border; padding: 6px 10px; spacing: 3px; }
QToolBar::separator { background: $border; width: 1px; margin: 5px 7px; }
QToolButton { background: transparent; color: $fg; border: 1px solid transparent; border-radius: 7px; padding: 4px 5px; }
QToolButton:hover { background: $hover; }
QToolButton:pressed { background: $border; }
QToolButton:checked { background: $accent_soft; color: $accent; }
QToolButton:disabled { color: $faint; }
QToolButton:checked:disabled { background: transparent; }
QToolButton::menu-indicator { image: none; width: 0; }
QToolButton[popupMode="1"] { padding-right: 16px; }
QToolButton::menu-button { border: none; background: transparent; width: 16px; }
QToolButton::menu-arrow { image: url($chevron_down); width: 10px; height: 10px; }
QToolButton#send { background: $accent; border: none; border-radius: 15px; padding: 0; }
QToolButton#send:hover { background: $accent_hover; }
QToolButton#send:disabled { background: $border; }
QToolButton#section { font-weight: 600; color: $muted; padding: 3px 6px 3px 2px; }

QPushButton { background: $raised; color: $fg; border: 1px solid $border; border-radius: 8px; padding: 6px 13px; }
QPushButton:focus { border-color: $accent; }
QPushButton:hover { background: $hover; }
QPushButton:pressed { background: $border; }
QPushButton:disabled { color: $faint; }
QPushButton[primary="true"] { background: $accent; color: $accent_fg; border-color: $accent; font-weight: 600; }
QPushButton[primary="true"]:hover { background: $accent_hover; border-color: $accent_hover; }

QComboBox { background: transparent; color: $fg; border: 1px solid $border; border-radius: 6px; padding: 3px 6px 3px 9px; }
QComboBox:hover { background: $hover; }
QComboBox:disabled { color: $faint; }
QComboBox::drop-down { border: none; width: 18px; }
QComboBox::down-arrow { image: url($chevron_down); width: 11px; height: 11px; }
QComboBox QAbstractItemView { background: $raised; color: $fg; border: 1px solid $border; outline: 0; padding: 4px;
                              selection-background-color: $hover; selection-color: $fg; }

QLineEdit#urlInput { background: $surface; color: $fg; border: 1px solid $border; border-radius: 7px; padding: 6px 8px;
                     selection-background-color: $accent_soft; selection-color: $fg; }
QLineEdit#urlInput:focus { border-color: $accent; }
QLineEdit#notesSearch { background: $raised; color: $fg; border: 1px solid $border; border-radius: 7px; padding: 4px 8px;
                        selection-background-color: $accent_soft; selection-color: $fg; }
QLineEdit#notesSearch:focus { border-color: $accent; }
QLineEdit#browserSearch, QLineEdit#tagsInput, QPlainTextEdit#fieldEdit, QWidget#browser QLineEdit, QDialog QLineEdit,
QDialog QPlainTextEdit, QDoubleSpinBox {
    background: $raised; color: $fg; border: 1px solid $border; border-radius: 7px; padding: 4px 7px;
    selection-background-color: $accent_soft; selection-color: $fg; }
QLineEdit#browserSearch:focus, QLineEdit#tagsInput:focus, QPlainTextEdit#fieldEdit:focus, QWidget#browser QLineEdit:focus,
QDialog QLineEdit:focus, QDialog QPlainTextEdit:focus, QDoubleSpinBox:focus { border-color: $accent; }
QLineEdit#browserSearch { padding: 6px 9px; }
QWidget#browser { background: $window; }
QTableView { background: $surface; alternate-background-color: $hover; color: $fg; border: 1px solid $border;
             border-radius: 6px; gridline-color: $border; outline: 0;
             selection-background-color: $accent_soft; selection-color: $fg; }
QTableView::item { padding: 0 6px; border: none; }
QTableView::item:selected { background: $accent_soft; color: $fg; }
QHeaderView { background: transparent; }
QHeaderView::section { background: $window; color: $muted; border: none; border-bottom: 1px solid $border;
                       border-right: 1px solid $border; padding: 4px 6px; font-weight: 600; }
QHeaderView::section:hover { color: $fg; }
QTableCornerButton::section { background: $window; border: none; }
QCheckBox { color: $fg; spacing: 7px; }
QMenuBar { background: $window; color: $fg; border-bottom: 1px solid $border; }
QMenuBar::item { padding: 4px 10px; background: transparent; border-radius: 5px; }
QMenuBar::item:selected { background: $hover; }
QStatusBar { background: $window; color: $muted; }
QScrollArea { background: transparent; border: none; }
QScrollArea > QWidget > QWidget { background: transparent; }

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
QWidget#navRail { background: $rail; border-right: 1px solid $border; }
QToolButton#navItem { color: $muted; border: none; border-radius: 11px; padding: 6px 0 5px; font-size: 8.5pt; }
QToolButton#navItem:hover { background: $hover; color: $fg; }
QToolButton#navItem:checked { background: $accent_soft; color: $accent; font-weight: 600; }
QToolButton#navItem:pressed { background: $border; }
QLineEdit#libraryFilter { background: $raised; color: $fg; border: 1px solid $border; border-radius: 8px; padding: 6px 9px;
                          selection-background-color: $accent_soft; selection-color: $fg; }
QLineEdit#libraryFilter:focus { border-color: $accent; }
QLineEdit#findInput { background: $surface; color: $fg; border: 1px solid $border; border-radius: 7px; padding: 4px 8px;
                      selection-background-color: $accent_soft; selection-color: $fg; }
QLineEdit#findInput:focus { border-color: $accent; }
QListView#findResults { background: $window; border: none; }
QMenuBar#browserMenu { background: $window; padding: 3px 6px; }
QLabel[role="muted"] { color: $muted; }
QLabel[role="hint"] { color: $faint; font-size: 8pt; }
QLabel[role="heading"] { font-size: 17pt; font-weight: 600; }
QLabel[role="title"] { font-size: 20pt; font-weight: 700; }
QLabel#bookTitle { color: $fg; font-weight: 600; padding: 0 6px 0 4px; }
QLabel[role="section"] { color: $muted; font-weight: 600; padding: 2px 4px; }
QLabel[role="page"] { color: $accent; font-weight: 600; }

QFrame#composer { background: $raised; border: 1px solid $border; border-radius: 12px; }
QFrame#composer[focused="true"] { border-color: $accent; }
QPlainTextEdit#composerInput { background: transparent; color: $fg; border: none; }
QFrame#chip { background: $accent_soft; border-radius: 8px; }
QFrame#chip QLabel { font-size: 9pt; }
QToolButton#angle { color: $muted; font-size: 8pt; font-weight: 600; padding: 4px 6px; }
QFrame#popup { background: $raised; border: 1px solid $border; border-radius: 10px; }
QFrame#readBar { background: $window; border-top: 1px solid $border; }
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
