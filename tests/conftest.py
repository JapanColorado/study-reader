import zipfile

import pymupdf
import pytest

PAGES = [
    "Chapter One. A zorblatt is a seven-legged device used to calibrate marmalade.",
    "Chapter Two. Every zorblatt must be rinsed in cold tea for 41 seconds.",
    "Chapter Three. Never store a zorblatt near a window.",
]


@pytest.fixture
def db(tmp_path, monkeypatch):
    """An empty database, with the data folder (media) in tmp_path too."""
    from study_reader.db import Database

    monkeypatch.setenv("STUDY_READER_DATA", str(tmp_path / "data"))
    return Database(tmp_path / "t.db")


@pytest.fixture
def pdf_path(tmp_path):
    doc = pymupdf.open()
    for i, text in enumerate(PAGES):
        page = doc.new_page(width=400, height=300)
        page.insert_textbox(pymupdf.Rect(40, 40, 360, 260), text, fontsize=12)
    doc.set_toc([[1, f"Chapter {i + 1}", i + 1] for i in range(len(PAGES))])
    doc.set_metadata({"title": "The Zorblatt Manual"})
    path = tmp_path / "zorblatt.pdf"
    doc.save(path)
    return str(path)


@pytest.fixture
def epub_path(tmp_path):
    path = tmp_path / "zorblatt.epub"
    chapters = "".join(f"<h1>Chapter {i + 1}</h1><p>{t}</p>" for i, t in enumerate(PAGES))
    with zipfile.ZipFile(path, "w") as z:
        z.writestr("mimetype", "application/epub+zip", compress_type=zipfile.ZIP_STORED)
        z.writestr(
            "META-INF/container.xml",
            '<?xml version="1.0"?><container version="1.0" xmlns="urn:oasis:names:tc:opendocument:xmlns:container">'
            '<rootfiles><rootfile full-path="content.opf" media-type="application/oebps-package+xml"/></rootfiles></container>',
        )
        z.writestr(
            "content.opf",
            '<?xml version="1.0"?><package xmlns="http://www.idpf.org/2007/opf" version="2.0" unique-identifier="id">'
            '<metadata xmlns:dc="http://purl.org/dc/elements/1.1/"><dc:title>Zorblatt EPUB</dc:title>'
            '<dc:identifier id="id">zorblatt</dc:identifier><dc:language>en</dc:language></metadata>'
            '<manifest><item id="c" href="c.xhtml" media-type="application/xhtml+xml"/></manifest>'
            '<spine><itemref idref="c"/></spine></package>',
        )
        z.writestr(
            "c.xhtml",
            f'<?xml version="1.0"?><html xmlns="http://www.w3.org/1999/xhtml"><head><title>Z</title></head><body>{chapters}</body></html>',
        )
    return str(path)


@pytest.fixture
def chat_panel(tmp_path, monkeypatch):
    """A ChatPanel on a book, with a stand-in worker that records jobs: (panel, worker, db, book id)."""
    monkeypatch.setenv("QT_QPA_PLATFORM", "offscreen")
    monkeypatch.setenv("STUDY_READER_DATA", str(tmp_path / "data"))
    from PySide6.QtCore import QObject, Signal
    from PySide6.QtWidgets import QApplication
    from study_reader.chat_panel import ChatPanel
    from study_reader.db import Database

    class Worker(QObject):
        status, base, delta = Signal(int, str), Signal(int, str, str), Signal(int, str)
        done, failed = Signal(int, str, str, str), Signal(int, str)
        graded, gradeFailed = Signal(int, str), Signal(int, str)
        cardGraded, cardGradeFailed = Signal(int, str), Signal(int, str)
        titled = Signal(int, str)
        converted, convertFailed = Signal(int, str), Signal(int, str)

        def __init__(self):
            super().__init__()
            self.jobs, self.gradings, self.card_gradings, self.titles = [], [], [], []
            self.conversions = []

        def submit(self, job):
            self.jobs.append(job)

        def submit_grade(self, job, answer_id):
            self.gradings.append((job, answer_id))

        def submit_card_grade(self, job, card_id):
            self.card_gradings.append((job, card_id))

        def submit_title(self, chat_id, question, passage, reply):
            self.titles.append((chat_id, question, passage, reply))

        def submit_conversion(self, batch_id, blocks, model, system_prompt):
            self.conversions.append((batch_id, blocks, model, system_prompt))

    QApplication.instance() or QApplication([])
    db = Database(tmp_path / "t.db")
    book_id = db.upsert_book("h", "/a.pdf", "A", 3)["id"]
    worker = Worker()
    panel = ChatPanel(db, worker)
    panel.set_book(book_id)
    return panel, worker, db, book_id
