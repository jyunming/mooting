"""Assemble the published site: the landing page at the root, the docs beneath it.

Two things live in this repo that both want to be a website. `marketing/index.html`
is a standalone page written to be opened from disk, so it reaches its screenshot
with `../docs/assets/` and links to the documentation as GitHub blob URLs. Both of
those are wrong once it is the site root -- the image path climbs above the root,
and every doc link walks the reader off the site.

The fix is to rewrite them on the way out rather than keeping a second copy of the
page, because a second copy drifts and nobody notices until a link 404s. Every
rewrite below is checked against the built tree, so a renamed doc breaks the build
instead of shipping a dead link.

    python tools/build_site.py        ->  site/
"""

from __future__ import annotations

import re
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / "site"
REPO = "https://github.com/jyunming/mooting"

#: A GitHub blob link to `docs/X.md` becomes the mkdocs page at `docs/X/`.
BLOB = re.compile(rf"{re.escape(REPO)}/blob/main/docs/([A-Za-z0-9_]+)\.md")


def build_docs() -> None:
    """mkdocs owns docs/; it renders into a subdirectory of the site."""
    r = subprocess.run(
        [sys.executable, "-m", "mkdocs", "build", "--strict", "-d", str(OUT / "docs")],
        cwd=ROOT, capture_output=True, text=True, encoding="utf-8", errors="replace",
    )
    if r.returncode != 0:
        sys.exit(f"mkdocs build failed:\n{r.stdout}\n{r.stderr}")


def landing_page() -> str:
    """The marketing page, with its disk-relative paths made site-relative."""
    html = (ROOT / "marketing" / "index.html").read_text(encoding="utf-8")

    html = html.replace("../docs/assets/", "assets/")
    html = BLOB.sub(lambda m: f"docs/{m.group(1)}/", html)

    if "../" in html:
        sys.exit("a disk-relative path survived the rewrite; it would 404 at the root")
    return html


def check_links(html: str) -> None:
    """Every rewritten target must exist in the tree we just built.

    Renaming a doc used to be free -- the landing page kept pointing at the old
    GitHub URL and nobody found out until a reader did. Now it fails here.
    """
    for target in sorted(set(re.findall(r'href="(docs/[A-Za-z0-9_]+/)"', html))):
        if not (OUT / target / "index.html").exists():
            sys.exit(f"landing page links to {target} but the docs build has no such page")

    for src in sorted(set(re.findall(r'src="(assets/[^"]+)"', html))):
        if not (OUT / src).exists():
            sys.exit(f"landing page references {src}, which is not in the site")


def main() -> int:
    if OUT.exists():
        shutil.rmtree(OUT)
    build_docs()

    (OUT / "assets").mkdir(parents=True, exist_ok=True)
    for shot in ("session.svg", "signoff.svg"):
        shutil.copy2(ROOT / "docs" / "assets" / shot, OUT / "assets" / shot)

    html = landing_page()
    (OUT / "index.html").write_text(html, encoding="utf-8")
    check_links(html)

    # GitHub Pages runs Jekyll over the artifact unless told not to, and Jekyll
    # drops any file or directory whose name starts with an underscore.
    (OUT / ".nojekyll").write_text("", encoding="utf-8")

    pages = sum(1 for _ in (OUT / "docs").rglob("index.html"))
    print(f"site/  landing page + {pages} doc pages")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
