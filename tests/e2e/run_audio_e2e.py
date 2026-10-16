"""End-to-end test: audio capture (real Chromium + extension + app).

    python tests/e2e/run_audio_e2e.py

Checks: a visible <audio> player gets a "Download this audio" button; audio served as
application/octet-stream from an address without extension (hidden `new Audio()` player) is
detected and gets the floating "Download audio" button; short clips and audio-only HLS are
listed, their segments are not; the octet-stream audio is saved with its real extension.
"""
import os, sys, json, time, shutil, subprocess, tempfile, threading
from pathlib import Path
ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "tests")); sys.path.insert(0, str(ROOT))
import conftest
from http.server import ThreadingHTTPServer
from playwright.sync_api import sync_playwright

W = Path(tempfile.mkdtemp(prefix="e2e-audio-"))
env = {**os.environ, "OPENDOWNLOADER_HOME": str(W/"home"), "DL_DIR": str(W/"dl"), "HARNESS_LOG": str(W/"msgs.log"),
       "HARNESS_ITEMS": str(W/"items.json")}
os.environ.update(env)
m = W/"media"; m.mkdir()
ff = ["ffmpeg", "-loglevel", "error", "-y"]
subprocess.run(ff + ["-f", "lavfi", "-i", "sine=f=440:duration=30", "-b:a", "128k", str(m/"song.mp3")], check=True)
subprocess.run(ff + ["-f", "lavfi", "-i", "sine=f=660:duration=30", "-c:a", "aac", "-b:a", "96k", str(m/"hidden.m4a")], check=True)
subprocess.run(ff + ["-f", "lavfi", "-i", "anoisesrc=d=6", "-c:a", "libvorbis", "-b:a", "160k", str(m/"clip.ogg")], check=True)
(m/"hls").mkdir()
subprocess.run(ff + ["-f", "lavfi", "-i", "sine=duration=8", "-c:a", "aac", "-f", "hls", "-hls_time", "2",
                     "-hls_playlist_type", "vod", "-hls_segment_filename", str(m/"hls/seg%d.aac"), str(m/"hls/a.m3u8")], check=True)

F = conftest.Handler.files = {}
page1 = """<html><head><title>Audio test</title></head><body><h1>Player</h1>
<audio id=a1 controls autoplay src="/a/song.mp3" style="width:400px"></audio>
<script>
fetch('/a/clip.ogg');
fetch('/hls/a.m3u8').then(r=>r.text()).then(()=>fetch('/hls/seg0.aac')).then(()=>fetch('/hls/seg1.aac'));
</script></body></html>"""
page2 = """<html><head><title>Hidden player</title></head><body><h1>Custom player</h1><div id=ui>▶ Now playing</div>
<script>const h = new Audio('/api/stream?id=7'); h.play().catch(e=>console.log(e));</script></body></html>"""
F["/page1.html"] = (page1.encode(), {"type": "text/html"})
F["/page2.html"] = (page2.encode(), {"type": "text/html"})
F["/a/song.mp3"] = ((m/"song.mp3").read_bytes(), {"type": "audio/mpeg"})
F["/a/clip.ogg"] = ((m/"clip.ogg").read_bytes(), {"type": "audio/ogg"})
F["/api/stream"] = ((m/"hidden.m4a").read_bytes(), {"type": "application/octet-stream"})
for p in (m/"hls").iterdir():
    F["/hls/" + p.name] = (p.read_bytes(), {"type": "application/vnd.apple.mpegurl" if p.suffix == ".m3u8" else "audio/aac"})

srv = ThreadingHTTPServer(("127.0.0.1", 0), conftest.Handler); srv.daemon_threads = True
threading.Thread(target=srv.serve_forever, daemon=True).start()
base = f"http://127.0.0.1:{srv.server_address[1]}"

app = subprocess.Popen([sys.executable, str(Path(__file__).with_name("app_harness.py"))], env=env,
                       stdout=open(W/"app.out", "w"), stderr=subprocess.STDOUT)
for _ in range(100):
    if (W/"home/ipc.json").exists(): break
    time.sleep(0.2)
subprocess.run([sys.executable, "-m", "opendownloader", "--register-host"], cwd=str(ROOT), env=env, check=True, capture_output=True)
udd = W/"udd"; (udd/"NativeMessagingHosts").mkdir(parents=True)
shutil.copy(W/"home/native_host/com.opendownloader.host.chromium.json", udd/"NativeMessagingHosts/com.opendownloader.host.json")
subprocess.run([sys.executable, str(ROOT/"extension"/"build.py")], check=True, capture_output=True)
ext = str(ROOT/"extension"/"dist"/"chromium")

def items():
    try: return json.loads((W/"items.json").read_text())
    except Exception: return []
def wait(pred, t=40):
    end = time.time() + t
    while time.time() < end:
        if pred(): return True
        time.sleep(0.3)
    return False
PANEL_TEXTS = """(() => [...document.querySelectorAll('opendownloader-panel')].filter(p => p.style.display === 'block')
   .map(p => ({top: parseFloat(p.style.top), left: parseFloat(p.style.left)})))()"""

ok = True
with sync_playwright() as pw:
    chrome = pw.chromium.executable_path
    proc = subprocess.Popen([chrome, f"--user-data-dir={udd}", "--headless=new", "--remote-debugging-port=9334", "--no-first-run",
        "--no-sandbox", "--autoplay-policy=no-user-gesture-required", f"--disable-extensions-except={ext}",
        f"--load-extension={ext}", "about:blank"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    time.sleep(3)
    browser = pw.chromium.connect_over_cdp("http://127.0.0.1:9334")
    ctx = browser.contexts[0]
    sw = ctx.service_workers[0] if ctx.service_workers else ctx.wait_for_event("serviceworker", timeout=15000)
    MEDIA = "JSON.stringify([...tabMedia.values()].flat().map(m=>({url:m.url.replace(/^https?:\\/\\/[^/]+/,''),kind:m.kind,audio:!!m.audio,video:!!m.video,label:m.label})))"

    page = ctx.new_page()
    page.goto(base + "/page1.html")
    page.wait_for_timeout(3500)
    media = json.loads(sw.evaluate(MEDIA))
    print("page1 media:", json.dumps(media, indent=1))
    urls = {x["url"]: x for x in media}
    c1 = urls.get("/a/song.mp3", {}).get("audio") is True
    c2 = urls.get("/a/clip.ogg", {}).get("audio") is True                   # short clip (below 300 KB) still listed
    c3 = urls.get("/hls/a.m3u8", {}).get("audio") is True
    c4 = not any("seg" in u for u in urls)                                  # HLS pieces not listed separately
    panels = page.evaluate(PANEL_TEXTS)
    a1 = page.evaluate("(() => { const r = document.getElementById('a1').getBoundingClientRect(); return {top:r.top, right:r.right}; })()")
    print("visible panels:", panels, "audio element:", a1)
    c5 = len(panels) == 1 and panels[0]["top"] < a1["top"] + 60
    print("song.mp3 audio:", c1, "| short clip:", c2, "| HLS audio:", c3, "| no segments:", c4, "| button at <audio>:", c5)
    ok &= c1 and c2 and c3 and c4 and c5

    page2 = ctx.new_page()
    page2.goto(base + "/page2.html")
    page2.wait_for_timeout(3500)
    print("page2 title:", page2.title())
    media2 = [x for x in json.loads(sw.evaluate(MEDIA)) if x["url"].startswith("/api/stream")]
    print("page2 media:", media2)
    panels2 = page2.evaluate(PANEL_TEXTS)
    vp = {"height": page2.evaluate("innerHeight")}
    print("page2 panels:", panels2, "viewport:", vp)
    c6 = bool(media2) and media2[0]["kind"] == "file"
    c7 = len(panels2) == 1 and panels2[0]["top"] > vp["height"] - 100      # floating button, bottom right
    page2.screenshot(path=str(W/"page2.png"))
    # download the hidden stream through the same path as the floating button's menu item
    tab_id = sw.evaluate("[...tabMedia.entries()].find(([k,v]) => v.some(m => m.url.includes('/api/stream')))[0]")
    r = sw.evaluate(f"(async () => {{ const e = tabMedia.get({tab_id}).find(m => m.url.includes('/api/stream')); return downloadMedia({{...e, audio: true}}, {tab_id}); }})()")
    print("downloadMedia ->", r)
    c8 = wait(lambda: any(i["status"] == "Complete" and i["filename"].endswith(".m4a") for i in items()), 40)
    print("items:", items())
    print("octet-stream detected:", c6, "| floating button:", c7, "| saved with real extension (.m4a):", c8)
    ok &= c6 and c7 and c8
    proc.terminate()
app.terminate()
print("RESULT", "PASS" if ok else "FAIL")
sys.exit(0 if ok else 1)
