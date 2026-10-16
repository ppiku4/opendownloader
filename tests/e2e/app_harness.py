"""App under test for run_browser_e2e.py: real app, offscreen, logs IPC messages."""
import os, sys, json
os.environ["QT_QPA_PLATFORM"] = "offscreen"
ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
sys.path.insert(0, ROOT)
from PySide6.QtWidgets import QApplication
from opendownloader.config import Settings
from opendownloader.ui import app as appmod
LOG = os.environ["HARNESS_LOG"]
orig = appmod.Controller._on_message
def logged(self, msg):
    with open(LOG, "a") as f: f.write(json.dumps(msg) + "\n")
    t = msg.get("type")
    if t == "page" and os.environ.get("HARNESS_PAGE_ONLY_LOG"):
        return  # yt-dlp can't reach real sites from the test sandbox; the message itself is what we check
    if t in ("media", "download") and os.environ.get("HARNESS_LOG_ONLY"):
        return  # the test site's host name only exists inside the test browser
    if t == "media":   # auto-pick best quality instead of showing the dialog
        from opendownloader.core.media_engine import probe_media
        d = msg["data"]; info = probe_media(self.settings, d["url"], self.headers_for(d), d.get("referer",""))
        with open(LOG, "a") as f: f.write(json.dumps({"probe": info}) + "\n")
        opt = info["options"][0]
        self.add_download({**d, "kind": opt["media"]["type"], "media": opt["media"], "filename": "captured.mp4", "filename_locked": True})
        return
    orig(self, msg)
appmod.Controller._on_message = logged
app = QApplication([])
st = Settings(); st["download_dir"] = os.environ["DL_DIR"]; st["show_add_dialog"] = False; st["show_complete_dialog"] = False
st.save()
ctl = appmod.Controller(app, st)
from PySide6.QtCore import QTimer
def dump():
    tmp = os.environ["HARNESS_ITEMS"] + ".tmp"
    with open(tmp, "w") as f:
        json.dump([{"filename": i.filename, "status": i.status.value, "referer": i.referer, "cookie": i.headers.get("Cookie"), "kind": i.kind, "error": i.error, "size": i.size} for i in ctl.manager.items.values()], f)
    os.replace(tmp, os.environ["HARNESS_ITEMS"])
t = QTimer(); t.timeout.connect(dump); t.start(500)
app.exec()
