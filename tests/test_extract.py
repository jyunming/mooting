"""Getting characters out of the files people actually send.

A deliberating seat cannot open a path, so an attachment is only worth
something if its text reaches the prompt. These build real files rather than
mocking a reader, because the failure being guarded against is "this format
went in and nothing came out" -- which a mock cannot have.
"""

import sys
import zipfile

import pytest

from mooting.extract import missing_reader, text_of
from mooting.store import connect


# ------------------------------------------------------------------ fixtures

def write_pdf(path, text="Cap retries at six with partial jitter."):
    """A one-page PDF, assembled here so the test owns every byte.

    Offsets in the cross-reference table are counted off the bytes as they are
    built. Hand-computing them is how a fixture ends up testing arithmetic
    instead of the thing it was written for.
    """
    stream = f"BT /F1 12 Tf 72 720 Td ({text}) Tj ET".encode("latin-1")
    objects = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] "
        b"/Resources << /Font << /F1 5 0 R >> >> /Contents 4 0 R >>",
        b"<< /Length " + str(len(stream)).encode() + b" >>\nstream\n"
        + stream + b"\nendstream",
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
    ]

    out = bytearray(b"%PDF-1.4\n")
    offsets = []
    for i, body in enumerate(objects, start=1):
        offsets.append(len(out))
        out += f"{i} 0 obj\n".encode() + body + b"\nendobj\n"

    xref = len(out)
    out += f"xref\n0 {len(objects) + 1}\n".encode()
    out += b"0000000000 65535 f \n"
    for off in offsets:
        out += f"{off:010d} 00000 n \n".encode()
    out += (f"trailer\n<< /Size {len(objects) + 1} /Root 1 0 R >>\n"
            f"startxref\n{xref}\n%%EOF\n").encode()

    path.write_bytes(bytes(out))
    return path


def write_docx(path, text="The gateway retries on a fixed thirty second schedule."):
    """A .docx is a zip of XML, so one can be built with the standard library."""
    document = (
        '<?xml version="1.0" encoding="UTF-8"?>'
        '<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">'
        f"<w:body><w:p><w:r><w:t>{text}</w:t></w:r></w:p>"
        "<w:p><w:r><w:t>A second paragraph.</w:t></w:r></w:p></w:body></w:document>"
    )
    with zipfile.ZipFile(path, "w") as zf:
        zf.writestr("[Content_Types].xml", "<Types/>")
        zf.writestr("word/document.xml", document)
    return path


@pytest.fixture()
def board(tmp_path):
    s = connect(tmp_path / "board.db", init=True)
    s.add_agent("me", "human")
    s.add_agent("claude", "claude", driver="spawn")
    s.open_topic("t", "T", "brief", "me", seats=("claude", "me"))
    yield s
    s.close()


# ------------------------------------------------------------------ formats

def test_a_pdf_gives_up_its_text(tmp_path):
    """The format somebody actually attached from a phone. It used to arrive as
    a filename and nothing else, which is a document the council cannot read."""
    got = text_of(write_pdf(tmp_path / "rfc.pdf"))

    assert got is not None, "a PDF with a text layer read as binary"
    assert "partial jitter" in got
    # An uncompressed PDF has no NUL byte and decodes as UTF-8, so a plain
    # "does this look like text" check claims it and hands the prompt the file's
    # own structure. The council would then argue about `/Type /Catalog`.
    assert "/Type /Catalog" not in got, "the raw PDF source was inlined as text"
    assert "endobj" not in got


def test_a_word_file_gives_up_its_text(tmp_path):
    got = text_of(write_docx(tmp_path / "notes.docx"))

    assert got is not None
    assert "fixed thirty second schedule" in got
    assert "A second paragraph." in got


def test_paragraphs_do_not_run_together(tmp_path):
    """Stripping tags without turning `</w:p>` into a newline first collapses a
    whole document onto one line, which reads as one sentence to a seat."""
    got = text_of(write_docx(tmp_path / "notes.docx"))

    assert "\n" in got.strip(), "every paragraph ran into the one before it"


def test_plain_text_still_goes_straight_through(tmp_path):
    p = tmp_path / "notes.md"
    p.write_text("# Heading\n\nbody", encoding="utf-8")

    assert text_of(p) == "# Heading\n\nbody"


# ------------------------------------------------- what has no text in it

def test_a_file_that_only_claims_to_be_a_pdf_does_not_blow_up(tmp_path):
    """Somebody's phone will send a truncated or mislabelled file eventually,
    and losing the attachment -- or the turn -- to that is not acceptable."""
    p = tmp_path / "broken.pdf"
    p.write_bytes(b"\x00\x01 not a pdf at all \xff\xfe")

    assert text_of(p) is None


def test_a_pdf_with_no_reader_installed_is_binary_not_a_crash(tmp_path, monkeypatch):
    """The extra is optional. Without it the file must land as binary and say
    so, rather than taking the attachment down with it."""
    monkeypatch.setitem(sys.modules, "pypdf", None)

    assert text_of(write_pdf(tmp_path / "rfc.pdf")) is None
    assert "mooting[pdf]" in (missing_reader("rfc.pdf") or ""), \
        "the one command that fixes it was not named"


def test_the_missing_reader_hint_is_only_about_pdfs(tmp_path):
    assert missing_reader("photo-abc.jpg") is None
    assert missing_reader("rfc.pdf") is None, "pypdf is installed; nothing to say"


# ------------------------------------------------------- end to end

def test_an_attached_pdf_is_marked_readable_and_reaches_the_prompt(board, tmp_path):
    """The whole point. `is_text` drives one thing -- does the content go into
    every seat's prompt -- so a PDF the council can read must set it."""
    from mooting.supervisor import _attachment_section

    tid = int(board.topic("t")["id"])
    board.attach(tid, write_pdf(tmp_path / "rfc.pdf"), "me", note="the RFC")

    row = board.attachments(tid)[0]
    assert row["is_text"], "a readable PDF was recorded as binary"

    prompt = "\n".join(_attachment_section(board, tid, budget=10_000))
    assert "partial jitter" in prompt, "the text never reached the prompt"
    assert "Not text" not in prompt


def test_a_binary_attachment_still_says_so(board, tmp_path):
    from mooting.supervisor import _attachment_section

    tid = int(board.topic("t")["id"])
    shot = tmp_path / "photo.jpg"
    shot.write_bytes(b"\xff\xd8\xff\xe0\x00\x10JFIF" + b"\x00" * 64)
    board.attach(tid, shot, "me")

    assert not board.attachments(tid)[0]["is_text"]
    assert "Not text" in "\n".join(_attachment_section(board, tid, budget=10_000))
