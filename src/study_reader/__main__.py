import os
import sys

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


def configure_decorations() -> None:
    """COSMIC asks Qt to draw its own title bar; the "adwaita" decoration looks
    nicer than Qt's default "bradient" one."""
    os.environ.setdefault("QT_WAYLAND_DECORATION", "adwaita")


def main() -> int:
    configure_webengine()
    configure_decorations()
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
