import zipfile

import pymupdf
import pytest

PAGES = [
    "Chapter One. A zorblatt is a seven-legged device used to calibrate marmalade.",
    "Chapter Two. Every zorblatt must be rinsed in cold tea for 41 seconds.",
    "Chapter Three. Never store a zorblatt near a window.",
]


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
