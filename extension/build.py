"""Build the browser extension for Chromium-based browsers and Firefox.

    python extension/build.py

Produces:
    extension/dist/chromium/        (Load unpacked in chrome://extensions)
    extension/dist/firefox/         (about:debugging -> Load Temporary Add-on)
    extension/dist/opendownloader-chromium.zip   (Chrome Web Store / Edge Add-ons upload)
    extension/dist/opendownloader-firefox.zip    (addons.mozilla.org upload / signing)
"""
from __future__ import annotations

import json
import shutil
import zipfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
DIST = HERE / "dist"


def _build_id() -> str:
    """Changes whenever any extension source file changes."""
    import hashlib

    h = hashlib.sha256()
    for f in sorted((HERE / "src").rglob("*")):
        if f.is_file():
            h.update(f.name.encode() + f.read_bytes())
    return h.hexdigest()[:12]


BUILD_ID = _build_id()


def build(target: str) -> Path:
    out = DIST / target
    shutil.rmtree(out, ignore_errors=True)
    shutil.copytree(HERE / "src", out)
    for js in ("background.js", "content.js"):
        f = out / js
        f.write_text(f.read_text("utf-8").replace("__EXT_BUILD__", BUILD_ID), "utf-8")
    shutil.copytree(HERE / "icons", out / "icons")
    manifest = json.loads((HERE / f"manifest.{target}.json").read_text("utf-8"))
    # A new version number for every build: browsers cache the background script per
    # version, so an unchanged number can keep an outdated background running after an update.
    manifest["version"] = f"{manifest['version']}.{int(BUILD_ID[:4], 16)}"
    (out / "manifest.json").write_text(json.dumps(manifest, indent=2), "utf-8")
    z = DIST / f"opendownloader-{target}.zip"
    with zipfile.ZipFile(z, "w", zipfile.ZIP_DEFLATED) as zf:
        for f in sorted(out.rglob("*")):
            if f.is_file():
                zf.write(f, f.relative_to(out).as_posix())
    return out


if __name__ == "__main__":
    DIST.mkdir(exist_ok=True)
    for t in ("chromium", "firefox"):
        print("built", build(t))
