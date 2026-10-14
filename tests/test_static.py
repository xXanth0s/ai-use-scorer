"""The dashboard ships as plain local files: no CDN, no build step."""
import re
from pathlib import Path

STATIC = Path(__file__).resolve().parents[1] / "src" / "scorer" / "static"


def test_assets_exist():
    for name in ("index.html", "app.css", "app.js"):
        assert (STATIC / name).is_file(), name


def test_index_references_only_local_assets():
    html = (STATIC / "index.html").read_text()
    refs = re.findall(r'(?:src|href)\s*=\s*"([^"]+)"', html)
    assert "app.css" in refs and "app.js" in refs
    for ref in refs:
        assert not re.match(r"^(https?:)?//", ref), ref


def test_css_and_js_load_nothing_remote():
    css = (STATIC / "app.css").read_text()
    js = (STATIC / "app.js").read_text()
    assert not re.search(r"@import|url\(\s*['\"]?(https?:)?//", css)
    assert not re.search(r"(fetch|import)\(\s*['\"`]https?:", js)
