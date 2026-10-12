import json
import sqlite3
import struct

import pytest

from study_reader import anki_import, cardmodel, srs
from study_reader.anki_import import Collection, migrate, to_markdown

CRT = 1_600_000_000 - (1_600_000_000 % 86400) + 4 * 3600  # an Anki collection created at 4 am
NOW = 1_800_000_000.0
DAY = (int(NOW) - CRT) // 86400  # today, as an Anki day number
W6 = [0.3172, 1.9345, 3.4506, 12.4106, 6.4133, 0.8334, 3.0194, 0.001, 1.8722, 0.1666, 0.796, 1.4835, 0.0614,
      0.2629, 1.6483, 0.6014, 1.8729, 0.5425, 0.0912, 0.0658, 0.1542]


# -- a tiny protobuf encoder -----------------------------------------------------------------
def _varint(n: int) -> bytes:
    out = bytearray()
    while True:
        b = n & 0x7F
        n >>= 7
        out.append(b | (0x80 if n else 0))
        if not n:
            return bytes(out)


def pb_int(field: int, n: int) -> bytes:
    return _varint(field << 3) + _varint(n)


def pb_bytes(field: int, b: bytes) -> bytes:
    return _varint(field << 3 | 2) + _varint(len(b)) + b


def pb_float(field: int, x: float) -> bytes:
    return _varint(field << 3 | 5) + struct.pack("<f", x)


def pb_floats(field: int, xs) -> bytes:
    return pb_bytes(field, struct.pack(f"<{len(xs)}f", *xs))


# -- a fake collection -----------------------------------------------------------------------
NOTETYPES = {
    1: ("Basic", ["Front", "Back"], ["{{Front}}"], 0),
    2: ("Reversible", ["Front", "Back"], ["{{Front}}", "{{Back}}"], 0),
    3: ("Cloze", ["Text", "Back Extra"], ["{{cloze:Text}}"], 1),
    4: ("Image Occlusion", ["Occlusion", "Image", "Header", "Comments", "Back Extra"], ["{{cloze:Occlusion}}"], 1),
    5: ("Japanese sentences", ["SentKanji", "SentEng"], ["{{SentKanji}}"], 0),
    6: ("BasicSource", ["Front", "Back", "Source"], ["{{Front}}"], 0),
}
DECKS = {1: "Default", 10: "Physics", 11: "Physics\x1fLesson 1", 12: "Physics\x1fLesson 2", 20: "Japanese"}
CLOZE = r"In {{c1::equilibrium::where}}, \(\rho = {{c2::0}}\) and {{c3::<b>surface</b>}}"
OCC = ("{{c1::image-occlusion:rect:left=.1:top=.2:width=.3:height=.1:oi=1}}<br>"
       "{{c2::image-occlusion:ellipse:left=.5:top=.5:width=.2:height=.2:oi=1}}")


def make_collection(tmp_path):
    folder = tmp_path / "Anki2" / "User 1"
    (folder / "collection.media").mkdir(parents=True)
    (folder / "collection.media" / "diagram 2.png").write_bytes(b"\x89PNG fake")
    (folder / "collection.media" / "chart.png").write_bytes(b"\x89PNG chart")
    path = folder / "collection.anki2"
    c = sqlite3.connect(path)
    c.executescript("""
        CREATE TABLE col (id INTEGER PRIMARY KEY, crt INTEGER);
        CREATE TABLE config (key TEXT PRIMARY KEY, val BLOB);
        CREATE TABLE notetypes (id INTEGER PRIMARY KEY, name TEXT, config BLOB);
        CREATE TABLE fields (ntid INTEGER, ord INTEGER, name TEXT);
        CREATE TABLE templates (ntid INTEGER, ord INTEGER, name TEXT, config BLOB);
        CREATE TABLE decks (id INTEGER PRIMARY KEY, name TEXT, kind BLOB);
        CREATE TABLE deck_config (id INTEGER PRIMARY KEY, name TEXT, config BLOB);
        CREATE TABLE notes (id INTEGER PRIMARY KEY, mid INTEGER, mod INTEGER, tags TEXT, flds TEXT);
        CREATE TABLE cards (id INTEGER PRIMARY KEY, nid INTEGER, did INTEGER, ord INTEGER, mod INTEGER, type INTEGER,
            queue INTEGER, due INTEGER, ivl INTEGER, reps INTEGER, lapses INTEGER, odue INTEGER, odid INTEGER,
            flags INTEGER, data TEXT);
        CREATE TABLE revlog (id INTEGER PRIMARY KEY, cid INTEGER, ease INTEGER, ivl INTEGER, lastIvl INTEGER,
            factor INTEGER, time INTEGER, type INTEGER);
    """)
    c.execute("INSERT INTO col VALUES (1, ?)", (CRT,))
    c.execute("INSERT INTO config VALUES ('fsrs', ?)", (b"true",))
    for ntid, (name, fields, qfmts, kind) in NOTETYPES.items():
        c.execute("INSERT INTO notetypes VALUES (?,?,?)", (ntid, name, pb_int(1, kind) if kind else b""))
        for i, f in enumerate(fields):
            c.execute("INSERT INTO fields VALUES (?,?,?)", (ntid, i, f))
        for i, q in enumerate(qfmts):
            c.execute("INSERT INTO templates VALUES (?,?,?,?)", (ntid, i, f"Card {i + 1}", pb_bytes(1, q.encode())))
    for did, name in DECKS.items():
        conf = 2 if name == "Japanese" else 1
        c.execute("INSERT INTO decks VALUES (?,?,?)", (did, name, pb_bytes(1, pb_int(1, conf))))
    c.execute("INSERT INTO decks VALUES (99, 'Filtered', ?)", (pb_bytes(2, b""),))
    c.execute("INSERT INTO deck_config VALUES (1, 'Default', ?)", (pb_floats(6, W6) + pb_float(37, 0.85) + pb_int(9, 20),))
    c.execute("INSERT INTO deck_config VALUES (2, 'Lang', ?)", (pb_floats(5, W6[:19]) + pb_float(37, 0.95),))

    def note(nid, mid, flds, tags=""):
        c.execute("INSERT INTO notes VALUES (?,?,?,?,?)", (nid, mid, nid // 1000 + 5, f" {tags} " if tags else "",
                                                          "\x1f".join(flds)))

    def card(cid, nid, did, ord, type=0, queue=0, due=0, ivl=0, reps=0, lapses=0, flags=0, data="", odid=0, odue=0):
        c.execute("INSERT INTO cards VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                  (cid, nid, did, ord, cid // 1000 + 9, type, queue, due, ivl, reps, lapses, odue, odid, flags, data))

    t0 = 1_700_000_000_000
    # A review card with FSRS memory, two reviews.
    note(t0 + 1, 1, ["What is <b>2+2</b>?", "4<br><img src=\"missing.png\">"], "math::arith marked")
    card(t0 + 101, t0 + 1, 11, 0, type=2, queue=2, due=DAY + 3, ivl=10, reps=2,
         data=json.dumps({"s": 12.5, "d": 4.2, "lrt": int(NOW) - 7 * 86400}))
    c.execute("INSERT INTO revlog VALUES (?,?,?,?,?,?,?,?)", (int(NOW * 1000) - 20 * 86400_000, t0 + 101, 3, -600, 0, 0, 8000, 0))
    c.execute("INSERT INTO revlog VALUES (?,?,?,?,?,?,?,?)", (int(NOW * 1000) - 7 * 86400_000, t0 + 101, 4, 10, -600, 0, 999_999, 1))
    c.execute("INSERT INTO revlog VALUES (?,?,?,?,?,?,?,?)", (int(NOW * 1000) - 6 * 86400_000, t0 + 101, 0, 10, 10, 0, 0, 4))
    # Reversible: two cards, the second suspended and flagged; both new.
    note(t0 + 2, 2, ["Type I error", "False positive"])
    card(t0 + 201, t0 + 2, 11, 0)
    card(t0 + 202, t0 + 2, 11, 1, queue=-1, flags=3)
    # Cloze with three deletions: c1 new, c2 learning (epoch due), c3 in another deck.
    note(t0 + 3, 3, [CLOZE, "Gauss"], "physics")
    card(t0 + 301, t0 + 3, 11, 0)
    card(t0 + 302, t0 + 3, 11, 1, type=1, queue=1, due=int(NOW) + 600, reps=1,
         data=json.dumps({"s": 0.4, "d": 6.0, "lrt": int(NOW) - 60}))
    card(t0 + 303, t0 + 3, 12, 2, type=2, queue=2, due=DAY - 1, ivl=5, reps=3, lapses=1)  # no memory: replayed
    for k, ease in enumerate((1, 3, 3)):
        c.execute("INSERT INTO revlog VALUES (?,?,?,?,?,?,?,?)",
                  (int(NOW * 1000) - (30 - k * 5) * 86400_000 + 1, t0 + 303, ease, 1, 0, 0, 5000, 1))
    # Image occlusion.
    note(t0 + 4, 4, [OCC, '<img src="diagram 2.png">', "Circuit", "A junction", ""])
    card(t0 + 401, t0 + 4, 11, 0)
    card(t0 + 402, t0 + 4, 11, 1)
    # A note type the importer doesn't know, in a language deck.
    note(t0 + 5, 5, ["猫", "cat"])
    card(t0 + 501, t0 + 5, 20, 0)
    # BasicSource with an image, in a filtered deck (home: Lesson 1).
    note(t0 + 6, 6, ['<img src="chart.png">', "", "OWID"])
    card(t0 + 601, t0 + 6, 99, 0, odid=11)
    c.commit()
    c.close()
    return path


@pytest.fixture
def anki(tmp_path):
    return make_collection(tmp_path)


def ids_of(col, *names):
    return [d.id for d in col.decks() if d.name in names]


def run(db, col, names, tmp_path, **kw):
    return migrate(db, col, ids_of(col, *names), media_to=tmp_path / "media", now=NOW, **kw)


# -- reading the collection ------------------------------------------------------------------
def test_reading_a_collection_copy(anki, tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path))
    assert ("User 1", anki) in anki_import.find_collections()
    with Collection(anki) as col:
        decks = {d.name: d for d in col.decks()}
        assert "Filtered" not in decks and decks["Physics::Lesson 1"].cards == 8  # the filtered card counts at home
        assert decks["Physics::Lesson 1"].suspended == 1 and decks["Japanese"].config_id == 2
        assert "Cloze" in decks["Physics::Lesson 1"].note_types
        assert col.fsrs_enabled()
        presets = col.presets()
        assert presets[1]["parameters"] == pytest.approx(W6, abs=1e-4) and presets[1]["desired_retention"] == 0.85
        assert len(presets[2]["parameters"]) == 19
        kinds = {nt.name: nt.kind for nt in col.note_types().values()}
        assert kinds == {"Basic": "basic", "Reversible": "reverse", "Cloze": "cloze", "Image Occlusion": "occlusion",
                         "Japanese sentences": "basic", "BasicSource": "basic"}
        tmp = col._tmp
    assert not tmp.exists()


# -- migrating -------------------------------------------------------------------------------
def test_migrating_notes_cards_and_schedules(anki, db, tmp_path):
    with Collection(anki) as col:
        report = run(db, col, ["Physics::Lesson 1", "Physics::Lesson 2", "Japanese"], tmp_path)
    assert report.notes == 6 and report.cards == 10 and report.skipped == 0 and not report.errors
    assert report.fallback_types == {"Japanese sentences": 1}
    assert report.reviews == 5  # the manual reschedule (ease 0) is left out
    assert report.missing_media == ["missing.png"] and report.media_copied == 2
    assert (tmp_path / "media" / "diagram 2.png").read_bytes() == b"\x89PNG fake"

    basic = db.card_by_anki_id(1_700_000_000_101)
    note = db.card_note(basic["note_id"])
    assert note["kind"] == "basic" and note["tags"] == " math::arith marked "
    assert json.loads(note["fields"])["front"] == "What is **2+2**?"
    assert db.deck(basic["deck_id"])["name"] == "Physics::Lesson 1"
    assert basic["stage"] == 2 and basic["stability"] == 12.5 and basic["difficulty"] == 4.2
    assert basic["last_review"] == NOW - 7 * 86400 and basic["reps"] == 2
    assert srs.day_start(basic["due"]) == srs.day_start(NOW + 3 * 86400)  # due on Anki's day
    logs = db.reviews(basic["id"])
    assert [(r["rating"], r["source"], r["duration"]) for r in logs] == [(3, "anki", 8000), (4, "anki", 120000)]

    rev = [db.card_by_anki_id(1_700_000_000_000 + i) for i in (201, 202)]
    assert rev[0]["note_id"] == rev[1]["note_id"] and [c["ord"] for c in rev] == [0, 1]
    assert rev[1]["suspended"] == 1 and rev[1]["flag"] == 3 and rev[0]["stage"] == 0
    assert json.loads(rev[1]["data"])["question"] == "False positive"

    cl = [db.card_by_anki_id(1_700_000_000_000 + i) for i in (301, 302, 303)]
    fields = json.loads(db.card_note(cl[0]["note_id"])["fields"])
    assert fields == {"text": r"In {{c1::equilibrium::where}}, \(\rho = {{c2::0}}\) and {{c3::**surface**}}",
                      "extra": "Gauss"}
    assert cl[1]["stage"] == 1 and cl[1]["due"] == NOW + 600
    assert cl[2]["stage"] == 2 and cl[2]["stability"] and cl[2]["lapses"] == 1  # memory from the replayed log
    assert db.deck(cl[2]["deck_id"])["name"] == "Physics::Lesson 2"
    assert r"\htmlClass{cloze}" in json.loads(cl[1]["data"])["question"]

    occ = db.card_by_anki_id(1_700_000_000_401)
    occ_fields = json.loads(db.card_note(occ["note_id"])["fields"])
    assert occ_fields["image"] == "diagram 2.png" and occ_fields["header"] == "Circuit"
    assert [s["shape"] for s in occ_fields["shapes"]] == ["rect", "ellipse"] and occ_fields["shapes"][0]["oi"]
    assert json.loads(db.card_by_anki_id(1_700_000_000_402)["data"])["active"] == 2

    src = db.card_by_anki_id(1_700_000_000_601)
    assert db.deck(src["deck_id"])["name"] == "Physics::Lesson 1"  # back home from the filtered deck
    assert json.loads(db.card_note(src["note_id"])["fields"])["extra"] == "Source: OWID"

    # New cards come in the order Anki made them; the parameters of the main preset are used.
    new = srs.next_card(db, None, NOW, deck=db.deck_by_name("Physics")["id"])
    assert new["anki_id"] in (1_700_000_000_302, 1_700_000_000_303)  # due learning/review cards first
    assert report.parameters == pytest.approx(W6, abs=1e-4) and report.retention == 0.85
    assert srs.parameters(db) == pytest.approx(W6, abs=1e-4) and srs.desired_retention(db) == 0.85

    # Running it again changes nothing.
    with Collection(anki) as col:
        again = run(db, col, ["Physics::Lesson 1", "Physics::Lesson 2", "Japanese"], tmp_path)
    assert (again.notes, again.cards, again.reviews, again.skipped) == (0, 0, 0, 10)
    assert db.count_cards() == 10


def test_subset_then_the_rest(anki, db, tmp_path):
    with Collection(anki) as col:
        first = run(db, col, ["Physics::Lesson 1"], tmp_path, prefix="Anki")
        assert first.cards == 8
        cloze = db.card_by_anki_id(1_700_000_000_301)
        assert [c["ord"] for c in db.note_cards(cloze["note_id"])] == [0, 1]  # c3 lives in Lesson 2
        assert db.deck(cloze["deck_id"])["name"] == "Anki::Physics::Lesson 1"
        second = run(db, col, ["Physics::Lesson 2"], tmp_path, prefix="Anki")
    assert (second.notes, second.cards, second.reviews) == (0, 1, 3)
    c3 = db.card_by_anki_id(1_700_000_000_303)
    assert c3["note_id"] == cloze["note_id"] and c3["ord"] == 2 and c3["stage"] == 2
    assert db.deck(c3["deck_id"])["name"] == "Anki::Physics::Lesson 2"


def test_content_only(anki, db, tmp_path):
    with Collection(anki) as col:
        report = run(db, col, ["Physics::Lesson 1"], tmp_path, scheduling=False, parameters=False)
    assert report.reviews == 0 and report.parameters is None
    assert all(c["stage"] == 0 and c["reps"] == 0 for c in db.cards())
    assert db.count_cards(where="suspended=1") == 1  # suspension is content, not schedule
    assert srs.parameters(db) == tuple(srs.Scheduler().parameters)


# -- HTML to Markdown ------------------------------------------------------------------------
@pytest.mark.parametrize("html, md", [
    ('In <i>electrostatic <u>equilibrium</u></i>,<br><br>any&nbsp;<b>net charge</b>&nbsp;is on the {{c1::<u><b>surface</b></u>::where}}',
     "In *electrostatic <u>equilibrium</u>*,\n\nany **net charge** is on the {{c1::<u>**surface**</u>::where}}"),
    ('the&nbsp;<b>density&nbsp;\\(\\rho\\)</b> is {{c1::<u>\\(0\\)</u>&nbsp;everywhere}}',
     "the **density \\(\\rho\\)** is {{c1::<u>\\(0\\)</u> everywhere}}"),
    ('What does <code>if __name__ == "__main__":</code> do?', 'What does `if __name__ == "__main__":` do?'),
    ('Examples<br><a href="https://ko-fi.com/s/1"><img src="No_Image_Available.jpg"></a>',
     "Examples\n[![](No_Image_Available.jpg)](https://ko-fi.com/s/1)"),
    ("<div>line one</div><div>line two</div>", "line one\nline two"),
    ("a <b>(x)</b>y, 5 * 3, snake_case, _x_ and $5", "a <b>(x)</b>y, 5 \\* 3, snake_case, \\_x\\_ and \\$5"),
    ("# not a heading<br>- not a list<br>1. nor this<br>&gt; nor a quote", "\\# not a heading\n\\- not a list\n1\\. nor this\n\\> nor a quote"),
    ("\\(a &lt; b<br>\\) and a &lt;b&gt; tag", "\\(a < b \\) and a &lt;b> tag"),
    ("<ul><li>one</li><li>two</li></ul><p>after</p>", "- one\n- two\n\nafter"),
    ('<span style="color: red; font-family: Arial">red</span> <font color="#00f">blue</font> <span class="x">plain</span>',
     '<span style="color: red">red</span> <span style="color: #00f">blue</span> plain'),
    ('<img src="paste 1.png"> <img src="a.png" width="200"> <img style="display: none" src="b.png">',
     '<img src="paste 1.png"> <img src="a.png" width="200">'),
    ("x<pre>def f():\n    return 1</pre>", "x\n\n```\ndef f():\n    return 1\n```"),
    ("<!-- note -->H<sub>2</sub>O<script>alert(1)</script>", "H<sub>2</sub>O"),
    ("<b>Kirchhoff</b><b>’s</b> <i>a</i><i>b</i>", "**Kirchhoff’s** *ab*"),
    ("", ""),
])
def test_to_markdown(html, md):
    assert to_markdown(html) == md


def test_cloze_overlapping_rows_are_unwrapped():
    nt = anki_import.NoteType(1, "Cloze Overlapping", ["Text", "Back Extra"], ["{{cloze:Text}}"], is_cloze=True)
    kind, fields = anki_import.note_fields(nt, ["[[r::{{c1::x}} = 1]]<br>[[r::{{c2::y}}]]", ""])
    assert kind == "cloze" and fields["text"] == "{{c1::x}} = 1\n{{c2::y}}"


def test_converted_text_keeps_making_the_same_cards():
    text = to_markdown(r'<div>{{c2::The <b>surface&nbsp;<u>\(z=f(x,y)\)</u></b>::what}} is {{c1::<u>\(x,y\)</u>::inputs}}</div>')
    assert cardmodel.card_ords("cloze", {"text": text}) == [0, 1]
    assert "[what]" in cardmodel.cloze_side(text, 2, False)
