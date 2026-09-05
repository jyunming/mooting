"""Getting characters out of a file so a council can read it.

A deliberating seat has no shell and cannot open a path, so an attachment is
only worth anything if its text reaches the prompt. Plain text always did. A
PDF did not, which meant the most common thing anybody attaches from a phone
arrived as a filename and nothing else.

The question here is never "what format is this". It is "did any text actually
come out" -- a scanned PDF has no text layer and an encrypted one refuses, and
both must land as binary rather than as a promise the prompt cannot keep.
"""

from __future__ import annotations

import logging
import re
import zipfile
from pathlib import Path

from .store import looks_like_text

log = logging.getLogger("mooting.extract")

#: Word and PowerPoint files are zip archives of XML. Excel is too, but its
#: text lives in a shared-strings table joined to cells by index, which is a
#: different job -- it is left out rather than half-done.
OOXML = {
    ".docx": "word/document.xml",
    ".pptx": None,          # one XML per slide; matched by prefix below
}


def text_of(path: Path | str) -> str | None:
    """The characters in this file, or None if there are none to be had.

    Never raises. A file somebody sent from a phone can be truncated, encrypted
    or not the format its name claims, and none of that is worth losing an
    attachment -- let alone a turn -- over.
    """
    path = Path(path)
    try:
        # Format first, and only then "does this decode". An uncompressed PDF
        # has no NUL byte and decodes clean, so the text test claims it and the
        # prompt gets `%PDF-1.4 1 0 obj << /Type /Catalog` instead of the
        # document. A file whose name says how to read it is read that way.
        suffix = path.suffix.lower()
        if suffix == ".pdf":
            return _from_pdf(path)
        if suffix in OOXML:
            return _from_ooxml(path, suffix)
        if looks_like_text(path):
            return path.read_text(encoding="utf-8", errors="replace")
    except Exception as exc:                  # never lose the attachment to this
        log.warning("could not read %s: %s", path.name, exc)
    return None


def _from_pdf(path: Path) -> str | None:
    """Text laid out in a PDF, if it has any.

    Imported here rather than at module load: `pypdf` is an extra, and a board
    that never sees a PDF should not need it installed.
    """
    import pypdf                              # noqa: PLC0415 - optional extra

    reader = pypdf.PdfReader(str(path))
    if getattr(reader, "is_encrypted", False):
        try:
            reader.decrypt("")                # some are encrypted with no password
        except Exception:
            return None
    out = "\n\n".join(page.extract_text() or "" for page in reader.pages)
    return out.strip() or None                # a scan has pages and no text


def _from_ooxml(path: Path, suffix: str) -> str | None:
    want = OOXML[suffix]
    with zipfile.ZipFile(path) as zf:
        names = ([want] if want else
                 sorted(n for n in zf.namelist()
                        if n.startswith("ppt/slides/slide") and n.endswith(".xml")))
        parts = []
        for name in names:
            try:
                parts.append(zf.read(name).decode("utf-8", errors="replace"))
            except KeyError:
                return None
    # Paragraph and break tags become newlines before the rest are dropped, or
    # every paragraph in the document runs together into one line.
    xml = "\n".join(parts)
    xml = re.sub(r"</w:p>|<w:br/>|</a:p>", "\n", xml)
    return re.sub(r"<[^>]+>", "", xml).strip() or None


def missing_reader(name: str) -> str | None:
    """Why this file could not be read, when the reason is a missing extra.

    An attachment that says only "binary" reads as a limit of the tool. Naming
    the one command that changes it is the difference between a dead end and a
    thing to do.
    """
    if not name.lower().endswith(".pdf"):
        return None
    try:
        import pypdf                          # noqa: F401,PLC0415
    except ImportError:
        return "install `mooting[pdf]` and the council can read PDFs"
    return None
