"""Chat transcript rendered in a web view (Markdown + KaTeX, bundled offline) with its
interactive quizzes, plus the window that shows one of its visualizations at full size."""

from __future__ import annotations

import json
from pathlib import Path

from PySide6.QtCore import QObject, Qt, QTimer, QUrl, Signal, Slot
from PySide6.QtGui import QColor, QDesktopServices
from PySide6.QtWebChannel import QWebChannel
from PySide6.QtWebEngineCore import QWebEnginePage
from PySide6.QtWebEngineWidgets import QWebEngineView
from PySide6.QtWidgets import QVBoxLayout, QWidget

from .theme import theme

ASSETS = Path(__file__).parent / "assets"
# Libraries a visualization may use. Sandboxed frames can't load file:// URLs, so
# transcript.html inlines the ones a visualization's code refers to.
VIZ_LIBS = {"d3": "viz/d3.min.js", "chart": "viz/chart.umd.min.js"}
VIZ_WINDOW = QUrl.fromLocalFile(str(ASSETS / "vizwindow.html"))


def _url_id(url: QUrl) -> int | None:
    try:
        return int(url.path().strip("/"))
    except ValueError:
        return None


def _open_external(url: QUrl, nav_type) -> None:
    """Links clicked inside a visualization open in the browser; scripted navigation doesn't."""
    if url.scheme() in ("http", "https", "mailto") and nav_type == QWebEnginePage.NavigationType.NavigationTypeLinkClicked:
        QDesktopServices.openUrl(url)


class _Page(QWebEnginePage):
    jump = Signal(int)
    openViz = Signal(int)
    edit = Signal(int)
    regenerate = Signal(int)
    page = Signal(int)
    quiz = Signal(int)

    def acceptNavigationRequest(self, url: QUrl, nav_type, is_main_frame: bool) -> bool:
        if not is_main_frame:  # a visualization's iframe: only its own srcdoc
            if url.scheme() in ("about", "data"):
                return True
            _open_external(url, nav_type)
            return False
        actions = {
            "jump": self.jump, "viz": self.openViz, "edit": self.edit, "regen": self.regenerate, "page": self.page,
            "quiz": self.quiz,
        }
        if url.scheme() in actions:
            if (n := _url_id(url)) is not None:
                actions[url.scheme()].emit(n)
            return False
        if url.scheme() in ("http", "https", "mailto"):
            QDesktopServices.openUrl(url)
            return False
        return True


class _QuizBridge(QObject):
    """What transcript.html's quizzes call through the web channel."""

    def __init__(self, view: TranscriptView):
        super().__init__(view)
        self.view = view

    @Slot(int, int, int, str)
    def answer(self, message_id: int, quiz: int, question: int, response: str) -> None:
        self.view.quizAnswered.emit(message_id, quiz, question, response)

    @Slot(int, int)
    def retake(self, message_id: int, quiz: int) -> None:
        self.view.quizRetake.emit(message_id, quiz)

    @Slot(int, int, int)
    def regrade(self, message_id: int, quiz: int, question: int) -> None:
        self.view.quizRegrade.emit(message_id, quiz, question)

    @Slot(int, int)
    def toggleReview(self, message_id: int, quiz: int) -> None:
        self.view.quizReviewToggled.emit(message_id, quiz)

    @Slot(int, int)
    def revise(self, message_id: int, quiz: int) -> None:
        self.view.quizReviseRequested.emit(message_id, quiz)


class TranscriptView(QWebEngineView):
    jumpRequested = Signal(int)  # snippet id
    pageRequested = Signal(int)  # 0-based page, from a quiz question's page link
    editRequested = Signal(int)  # message id
    regenerateRequested = Signal(int)  # message id
    quizRequested = Signal(int)  # message id: quiz me on this reply
    quizAnswered = Signal(int, int, int, str)  # message id, quiz, question, response JSON
    quizRetake = Signal(int, int)  # message id, quiz
    quizRegrade = Signal(int, int, int)  # message id, quiz, question (its grading failed)
    quizReviewToggled = Signal(int, int)  # message id, quiz: put its cards in or out of review
    quizReviseRequested = Signal(int, int)  # message id, quiz: ask Claude to revise it

    def __init__(self, parent=None):
        super().__init__(parent)
        page = _Page(self)
        page.jump.connect(self.jumpRequested)
        page.page.connect(self.pageRequested)
        page.edit.connect(self.editRequested)
        page.regenerate.connect(self.regenerateRequested)
        page.quiz.connect(self.quizRequested)
        page.openViz.connect(self._open_viz)
        self.setPage(page)
        self.channel = QWebChannel(page)
        self.channel.registerObject("quiz", _QuizBridge(self))
        page.setWebChannel(self.channel)
        page.setBackgroundColor(QColor(theme().colors["surface"]))
        self._ready = False
        self._pending: str | None = None
        self._stream_text = ""
        self._stream_timer = QTimer(self, singleShot=True, interval=60)
        self._stream_timer.timeout.connect(self._flush_stream)
        self.loadFinished.connect(self._on_loaded)
        self.load(QUrl.fromLocalFile(str(ASSETS / "transcript.html")))

    def show_messages(
        self, messages: list[dict], streaming: str | None = None, placeholder: str = "", editing: int | None = None
    ) -> None:
        """streaming: None when idle, else the reply text received so far.
        editing: id of the message being rewritten in the composer, if any.
        An assistant message's quizzes come in its "quizzes", from quiz_view(), with its "text"
        holding the placeholders quiz.split() leaves."""
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
            out.append({
                "id": m["id"], "role": m["role"], "text": m["text"], "snippets": snippets,
                "quizzes": m.get("quizzes", []),
            })
        data = {"messages": out, "streaming": streaming, "placeholder": placeholder, "editing": editing,
                "dark": theme().dark, "colors": theme().colors}
        self._run(f"render({json.dumps(data)})")

    def update_quiz(self, message_id: int, index: int, quiz: dict) -> None:
        """Redraw one quiz in place (after an answer), keeping the scroll position."""
        self._run(f"updateQuiz({message_id}, {index}, {json.dumps(quiz)})", queue=False)

    def quiz_input_error(self, message_id: int, index: int, question: int, error: str) -> None:
        self._run(f"quizInputError({message_id}, {index}, {question}, {json.dumps(error)})", queue=False)

    def set_streaming(self, text: str) -> None:
        self._stream_text = text
        if not self._stream_timer.isActive():
            self._stream_timer.start()

    def _flush_stream(self) -> None:
        self._run(f"setStreaming({json.dumps(self._stream_text)})", queue=False)

    def _open_viz(self, viz_id: int) -> None:
        def show(result) -> None:
            viz = json.loads(result) if isinstance(result, str) and result else None
            if viz:
                VisualizationWindow(viz["title"] or "Visualization", viz["doc"], self.window()).show()

        # As JSON: large objects don't survive the conversion to Python.
        self.page().runJavaScript(f"JSON.stringify(VIZ[{viz_id}] ? vizDoc({viz_id}, true) : null)", 0, show)

    def _run(self, js: str, queue: bool = True) -> None:
        if self._ready:
            self.page().runJavaScript(js)
        elif queue:
            self._pending = js

    def _on_loaded(self, ok: bool) -> None:
        self._ready = True
        libs = {name: (ASSETS / rel).read_text(encoding="utf-8") for name, rel in VIZ_LIBS.items()}
        self.page().runJavaScript(f"setVizLibs({json.dumps(libs)})")
        if self._pending:
            self.page().runJavaScript(self._pending)
            self._pending = None


class _VizPage(QWebEnginePage):
    fullScreen = Signal(bool)

    def acceptNavigationRequest(self, url: QUrl, nav_type, is_main_frame: bool) -> bool:
        if is_main_frame and url.scheme() == "viz":  # vizwindow.html's full-screen toggle
            self.fullScreen.emit(url.host() == "fullscreen")
            return False
        if url.scheme() in ("about", "data") or (is_main_frame and url == VIZ_WINDOW):
            return True
        _open_external(url, nav_type)
        return False


class VisualizationWindow(QWidget):
    """A visualization from the transcript in its own resizable window (vizwindow.html),
    fitted to the window, with zoom and full screen."""

    def __init__(self, title: str, doc: str, parent: QWidget | None = None):
        super().__init__(parent, Qt.WindowType.Window)
        self.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose)
        self.setWindowTitle(title)
        screen = (parent or self).screen().availableGeometry()
        self.resize(int(screen.width() * 0.75), int(screen.height() * 0.8))
        self.view = QWebEngineView(self)
        page = _VizPage(self.view)
        page.setBackgroundColor(QColor(theme().colors["surface"]))
        page.fullScreen.connect(self._full_screen)
        self.view.setPage(page)
        data = {"doc": doc, "dark": theme().dark, "colors": theme().colors}
        self.view.loadFinished.connect(lambda ok: page.runJavaScript(f"show({json.dumps(data)})"))
        self.view.load(VIZ_WINDOW)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.addWidget(self.view)

    def _full_screen(self, on: bool) -> None:
        """F11 and the toolbar button toggle; Escape only leaves."""
        if on and not self.isFullScreen():
            self.showFullScreen()
        elif self.isFullScreen():
            self.showNormal()
