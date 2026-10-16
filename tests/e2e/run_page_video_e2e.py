"""End-to-end: videos from site players (YouTube & co.) -> in-page button -> app gets the page URL.

    python tests/e2e/run_page_video_e2e.py

Real Chromium + the extension + native host + app. www.youtube.com is mapped to a local
HTTPS test server (self-signed) whose watch page plays a blob: video like YouTube's player.
"""
import json, os, shutil, ssl, subprocess, sys, tempfile, threading, time
from http.server import ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "tests")); sys.path.insert(0, str(ROOT))
import conftest  # noqa: E402
from playwright.sync_api import sync_playwright  # noqa: E402

W = Path(tempfile.mkdtemp(prefix="e2e-page-"))
env = {**os.environ, "OPENDOWNLOADER_HOME": str(W / "home"), "DL_DIR": str(W / "dl"),
       "HARNESS_LOG": str(W / "msgs.log"), "HARNESS_ITEMS": str(W / "items.json"), "HARNESS_PAGE_ONLY_LOG": "1", "HARNESS_LOG_ONLY": "1"}
os.environ.update(env)
subprocess.run(["ffmpeg", "-loglevel", "error", "-f", "lavfi", "-i", "testsrc=duration=8:size=640x360:rate=25",
                "-c:v", "libvpx", "-b:v", "500k", str(W / "clip.webm")], check=True)
subprocess.run(["openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes", "-days", "1", "-subj", "/CN=www.youtube.com",
                "-keyout", str(W / "k.pem"), "-out", str(W / "c.pem")], check=True, capture_output=True)
BLOB_PAGE = """<html><head><title>{title}</title></head><body>
<video id=v width=640 height=360 autoplay muted loop></video>
<script>fetch('/clip.webm').then(r => r.blob()).then(b => {{ document.getElementById('v').src = URL.createObjectURL(b); }});</script>
</body></html>"""
F = conftest.Handler.files = {}
F["/watch"] = (BLOB_PAGE.format(title="(3) My Test Clip - YouTube").encode(), {"type": "text/html"})
F["/"] = (BLOB_PAGE.format(title="YouTube").encode(), {"type": "text/html"})          # home: feed preview
F["/player.html"] = (BLOB_PAGE.format(title="Some other site").encode(), {"type": "text/html"})
F["/clip.webm"] = ((W / "clip.webm").read_bytes(), {"type": "video/webm"})
srv = ThreadingHTTPServer(("127.0.0.1", 0), conftest.Handler); srv.daemon_threads = True
ctx_ssl = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER); ctx_ssl.load_cert_chain(W / "c.pem", W / "k.pem")
srv.socket = ctx_ssl.wrap_socket(srv.socket, server_side=True)
threading.Thread(target=srv.serve_forever, daemon=True).start()
port = srv.server_address[1]

app = subprocess.Popen([sys.executable, str(Path(__file__).with_name("app_harness.py"))], env=env,
                       stdout=open(W / "app.out", "w"), stderr=subprocess.STDOUT)
for _ in range(100):
    if (W / "home/ipc.json").exists():
        break
    time.sleep(0.2)
subprocess.run([sys.executable, "-m", "opendownloader", "--register-host"], cwd=str(ROOT), env=env, check=True, capture_output=True)
udd = W / "udd"; (udd / "NativeMessagingHosts").mkdir(parents=True)
shutil.copy(W / "home/native_host/com.opendownloader.host.chromium.json", udd / "NativeMessagingHosts/com.opendownloader.host.json")
subprocess.run([sys.executable, str(ROOT / "extension" / "build.py")], check=True, capture_output=True)
ext = str(ROOT / "extension" / "dist" / "chromium")


def messages():
    try:
        return [json.loads(x) for x in (W / "msgs.log").read_text().splitlines()]
    except OSError:
        return []


def open_menu_and_click(page, row_text):
    """Open the in-page button menu (closed shadow DOM) and click a row by its text."""
    pos = page.evaluate("(() => { const p=document.querySelector('opendownloader-panel');"
                        " return p && p.style.display==='block' ? [parseFloat(p.style.left), parseFloat(p.style.top)] : null; })()")
    if not pos:
        return False
    page.mouse.click(pos[0] + 60, pos[1] + 12)
    page.wait_for_timeout(700)
    page.screenshot(path=str(W / f"menu-{row_text[:6]}.png"))
    # rows are laid out under the button: find the row by probing points
    for dy in range(34, 160, 6):
        x, y = pos[0] - 60, pos[1] + dy
        hit = page.evaluate(f"(() => {{ const e = document.elementFromPoint({x}, {y}); return e && e.tagName; }})()")
        if hit == "OPENDOWNLOADER-PANEL":
            before = len(messages())
            page.mouse.click(x, y)
            page.wait_for_timeout(2500)
            if len(messages()) > before:
                return True
    return False


ok = True
with sync_playwright() as pw:
    chrome = pw.chromium.executable_path
    proc = subprocess.Popen([chrome, f"--user-data-dir={udd}", "--headless=new", "--remote-debugging-port=9334",
                             "--no-first-run", "--no-sandbox", "--ignore-certificate-errors", "--no-proxy-server",
                             f"--host-resolver-rules=MAP www.youtube.com:443 127.0.0.1:{port}, MAP othersite.test:443 127.0.0.1:{port}",
                             f"--disable-extensions-except={ext}", f"--load-extension={ext}", "about:blank"],
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    time.sleep(3)
    import atexit
    atexit.register(proc.terminate)
    atexit.register(app.terminate)
    browser = pw.chromium.connect_over_cdp("http://127.0.0.1:9334")
    ctx = browser.contexts[0]
    page = ctx.new_page()
    page.set_viewport_size({"width": 900, "height": 520})

    def panel_state():
        return page.evaluate("(() => { const p=document.querySelector('opendownloader-panel'); return p ? p.style.display : 'none'; })()")

    # 1) YouTube watch page
    page.goto("https://www.youtube.com/watch?v=dQw4w9WgXc0&list=PL123&index=4")
    page.wait_for_timeout(4000)
    print("youtube watch: button", panel_state(), "| video src:", page.evaluate("document.getElementById('v').currentSrc.slice(0, 5)"))
    ok &= panel_state() == "block"
    ok &= open_menu_and_click(page, "Choose quality")
    m = [x for x in messages() if x.get("type") == "page"]
    print("app received:", m[-1] if m else None)
    ok &= bool(m) and m[-1]["data"]["url"] == "https://www.youtube.com/watch?v=dQw4w9WgXc0" and m[-1]["data"]["title"] == "My Test Clip"
    # 2) YouTube home page: previews -> no button
    page.goto("https://www.youtube.com/")
    page.wait_for_timeout(4000)
    print("youtube home: button", panel_state())
    ok &= panel_state() != "block"
    # 3) other site with a blob: player -> page fallback
    page.goto("https://othersite.test/player.html")
    page.wait_for_timeout(4000)
    print("other site: button", panel_state())
    # the player fetched a whole file into a blob: -> the menu offers that file first (pagehook.js)
    n = len(messages())
    ok &= panel_state() == "block" and open_menu_and_click(page, "This video file")
    m = messages()[n:]
    print("app received:", m[-1] if m else None)
    ok &= bool(m) and m[-1].get("type") == "download" and m[-1]["data"]["url"] == "https://othersite.test/clip.webm"
    proc.terminate()
app.terminate()
print("screenshots in", W)
print("RESULT", "PASS" if ok else "FAIL")
sys.exit(0 if ok else 1)
