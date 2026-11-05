import re
from pathlib import Path

from app import __version__

ROOT = Path(__file__).resolve().parent.parent
VERSION = (ROOT / "VERSION").read_text().strip()
SEMVER = re.compile(r"(?<![\d.])\d+\.\d+\.\d+(?![\d.])")


def test_package_version_reads_file():
    assert __version__ == VERSION and SEMVER.fullmatch(VERSION)


def test_no_hardcoded_versions_in_sources():
    offenders = []
    files = list((ROOT / "static").rglob("*.html")) + list((ROOT / "static").rglob("*.js")) + list((ROOT / "static").rglob("*.css"))
    files += [ROOT / "app" / "main.py", ROOT / "app" / "__init__.py"]
    for f in files:
        txt = f.read_text(encoding="utf-8")
        for m in re.finditer(r"(\?v=|app-version\" content=\"|\bv|version=|\"version\": |VERSION = )\"?(\d+\.\d+\.\d+)", txt):
            offenders.append(f"{f.relative_to(ROOT)}: {m.group(0)}")
    assert not offenders, offenders


def test_health_and_pages_serve_file_version(client):
    assert client.get("/health").json()["version"] == VERSION
    html = client.get("/").text
    assert "__APP_VERSION__" not in html
    assert f'content="{VERSION}"' in html and f"?v={VERSION}" in html and f"v{VERSION}" in html
    js = client.get("/static/js/app.js").text
    assert "__APP_VERSION__" not in js and f"?v={VERSION}" in js
    for page in ("/bind", "/sandbox"):
        r = client.get(page)
        if r.status_code == 200:
            assert "__APP_VERSION__" not in r.text
