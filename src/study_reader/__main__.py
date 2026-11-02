import os
import sys
from pathlib import Path

import PySide6
from PySide6.QtGui import QIcon
from PySide6.QtWidgets import QApplication

from .main_window import MainWindow
from .transcript_view import ASSETS


def configure_webengine() -> None:
    """GPU compositing in QtWebEngine hands back textures the Qt scene graph can't
    use on some Wayland setups (COSMIC: "Backend texture is not a Vulkan texture"),
    leaving the transcript blank. Software rendering is plenty for a chat view."""
    flags = os.environ.get("QTWEBENGINE_CHROMIUM_FLAGS", "")
    if "--disable-gpu" not in flags and os.environ.get("STUDY_READER_WEB_GPU") != "1":
        os.environ["QTWEBENGINE_CHROMIUM_FLAGS"] = f"{flags} --disable-gpu".strip()


def configure_compositing() -> None:
    """Draw the window through the GPU (RHI) at all times. Qt otherwise draws it in software
    until a web view is on screen and switches over when one appears, and on Wayland the
    switch shows a white frame: switching from the Library to Home flashed white.
    QT_WIDGETS_RHI=0 turns this off."""
    os.environ.setdefault("QT_WIDGETS_RHI", "1")


def configure_decorations() -> None:
    """COSMIC asks Qt to draw its own title bar; the "adwaita" decoration looks
    nicer than Qt's default "bradient" one."""
    os.environ.setdefault("QT_WAYLAND_DECORATION", "adwaita")


def configure_platform_theme() -> None:
    """Use the desktop's own file chooser (cosmic-files, Nautilus, ...) through the
    xdg-desktop-portal. Without a platform theme PySide6 actually ships (e.g. the
    common QT_QPA_PLATFORMTHEME=qt5ct), Qt falls back to its own widget dialog."""
    if not sys.platform.startswith("linux"):
        return
    plugins = Path(PySide6.__file__).parent / "Qt" / "plugins" / "platformthemes"
    requested = os.environ.get("QT_QPA_PLATFORMTHEME", "").split(":")[0]
    if requested and any(plugins.glob(f"libq{requested}.so")):
        return
    os.environ["QT_QPA_PLATFORMTHEME"] = "xdgdesktopportal"


def main() -> int:
    configure_webengine()
    configure_compositing()
    configure_decorations()
    configure_platform_theme()
    app = QApplication(sys.argv)
    app.setApplicationName("study-reader")
    app.setApplicationDisplayName("Study Reader")
    app.setDesktopFileName("study-reader")
    app.setWindowIcon(QIcon(str(ASSETS / "icon.svg")))
    window = MainWindow()
    window.show()
    if len(sys.argv) > 1:
        arg = sys.argv[1]
        if arg.startswith(("http://", "https://")):
            window.open_url(arg)
        else:
            window.open_book(arg)
    return app.exec()


if __name__ == "__main__":
    sys.exit(main())
