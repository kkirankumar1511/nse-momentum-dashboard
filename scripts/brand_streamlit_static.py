"""Rebrands Streamlit's own shipped index.html/favicon.png so the browser
tab title/icon are correct even BEFORE the app's JS runs (st.set_page_config
only patches document.title/favicon via JS after the React app mounts --
the raw server-rendered HTML Streamlit ships still hardcodes "Streamlit"
and its own default favicon.png). That raw HTML is what browser/OS
"create shortcut" and "add to home screen" flows read, so without this
patch those show "Streamlit" with no icon regardless of page_icon/
page_title.

This edits files INSIDE the installed streamlit package (not this repo),
so it's not covered by git and must be re-run after every `pip install`
that touches streamlit (fresh install, upgrade, or venv rebuild) --
that's exactly why this lives as a standalone idempotent script rather
than a one-off manual edit. Run with the venv's own python so the right
streamlit installation is found:
    .venv/bin/python scripts/brand_streamlit_static.py       (VPS/Linux)
    .venv\\Scripts\\python scripts\\brand_streamlit_static.py  (Windows)
"""
from __future__ import annotations

import shutil
from pathlib import Path

import streamlit

APP_NAME = "KK Trading System"
REPO_ROOT = Path(__file__).resolve().parent.parent
SOURCE_FAVICON = REPO_ROOT / "assets" / "favicon_square.png"


def main() -> None:
    static_dir = Path(streamlit.__file__).resolve().parent / "static"
    index_html = static_dir / "index.html"
    favicon = static_dir / "favicon.png"

    if not index_html.exists():
        raise SystemExit(f"Not found: {index_html} -- is this the right venv?")
    if not SOURCE_FAVICON.exists():
        raise SystemExit(
            f"Not found: {SOURCE_FAVICON} -- run this from the repo root, "
            "with assets/favicon_square.png already generated.")

    html = index_html.read_text(encoding="utf-8")
    changed = False

    if "<title>Streamlit</title>" in html:
        html = html.replace("<title>Streamlit</title>", f"<title>{APP_NAME}</title>")
        changed = True
    elif f"<title>{APP_NAME}</title>" in html:
        pass  # already patched
    else:
        print("WARNING: <title> tag not found in the expected form -- "
              "leaving it untouched (Streamlit's static HTML may have "
              "changed shape in this version).")

    apple_meta = f'<meta name="apple-mobile-web-app-title" content="{APP_NAME}" />'
    if apple_meta not in html:
        html = html.replace("<title>", f"{apple_meta}\n    <title>", 1)
        changed = True

    if changed:
        index_html.write_text(html, encoding="utf-8")
        print(f"Patched {index_html}")
    else:
        print(f"{index_html} already patched, nothing to do")

    shutil.copyfile(SOURCE_FAVICON, favicon)
    print(f"Replaced {favicon} with {SOURCE_FAVICON}")


if __name__ == "__main__":
    main()
