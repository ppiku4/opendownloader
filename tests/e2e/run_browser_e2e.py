"""End-to-end test: real Chromium + the extension + native host + the app.

    pip install playwright && python tests/e2e/run_browser_e2e.py

Checks: download capture (cookies + referrer forwarded, browser download
cancelled), HLS/file media detection, the in-page video button, HLS capture.
Uses Playwright's bundled Chromium. Linux/macOS/Windows.
"""
import os, sys, json, time, shutil, subprocess, tempfile, threading
from pathlib import Path
ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "tests")); sys.path.insert(0, str(ROOT))
import conftest
from http.server import ThreadingHTTPServer
from playwright.sync_api import sync_playwright

W = Path(tempfile.mkdtemp(prefix="e2e-"))
env = {**os.environ, "OPENDOWNLOADER_HOME": str(W/"home"), "DL_DIR": str(W/"dl"), "HARNESS_LOG": str(W/"msgs.log"), "HARNESS_ITEMS": str(W/"items.json")}
os.environ.update(env)
media = W/"media"; media.mkdir()
SRC = ["-f","lavfi","-i","testsrc=duration=8:size=640x360:rate=25","-f","lavfi","-i","sine=duration=8","-c:v","libx264","-c:a","aac","-shortest"]
subprocess.run(["ffmpeg","-loglevel","error","-f","lavfi","-i","testsrc=duration=8:size=640x360:rate=25,noise=alls=60:allf=t","-c:v","libvpx","-b:v","1500k","-crf","4","-deadline","realtime",str(media/"movie.webm")],check=True)
(media/"hls").mkdir()
subprocess.run(["ffmpeg","-loglevel","error",*SRC,"-hls_time","2","-hls_playlist_type","vod",str(media/"hls/v.m3u8")],check=True)
(media/"hls/master.m3u8").write_text("#EXTM3U\n#EXT-X-STREAM-INF:BANDWIDTH=900000,RESOLUTION=640x360\nv.m3u8\n")

srv = ThreadingHTTPServer(("127.0.0.1",0), conftest.Handler); srv.daemon_threads=True
threading.Thread(target=srv.serve_forever, daemon=True).start()
base = f"http://127.0.0.1:{srv.server_address[1]}"
page_html = """<html><body><h1>Test</h1>
<a id=zip href="/files/archive.zip">archive</a>
<a id=mag href="magnet:?xt=urn:btih:0123456789abcdef0123456789abcdef01234567&dn=Test">magnet</a>
<a id=tor href="/files/pack.torrent">torrent file</a>
<video id=v src="/media/movie.webm" width=640 height=360 autoplay muted loop></video>
<script>document.cookie='sess=abc123; path=/'; fetch('/media/hls/master.m3u8').then(r=>r.text()).then(t=>fetch('/media/hls/v.m3u8'));</script>
</body></html>"""
F = conftest.Handler.files = {}
F["/page.html"] = (page_html.encode(), {"type":"text/html"})
F["/files/archive.zip"] = (os.urandom(3*1024*1024), {"type":"application/zip","delay":0.002})
F["/files/pack.torrent"] = (b"d4:infod6:lengthi5e4:name5:a.txt12:piece lengthi16384e6:pieces20:" + b"x"*20 + b"ee",
                            {"type":"application/x-bittorrent"})
for p in media.rglob("*"):
    if p.is_file():
        t = "video/webm" if p.suffix==".webm" else ("application/vnd.apple.mpegurl" if p.suffix==".m3u8" else "video/mp2t")
        F["/media/"+p.relative_to(media).as_posix()] = (p.read_bytes(), {"type": t})

app = subprocess.Popen([sys.executable, str(Path(__file__).with_name("app_harness.py"))], env=env, stdout=open(W/"app.out","w"), stderr=subprocess.STDOUT)
for _ in range(100):
    if (W/"home/ipc.json").exists(): break
    time.sleep(0.2)
print("app up:", (W/"home/ipc.json").exists())
subprocess.run([sys.executable,"-m","opendownloader","--register-host"],cwd=str(ROOT),env=env,check=True,capture_output=True)
udd = W/"udd"; (udd/"NativeMessagingHosts").mkdir(parents=True)
shutil.copy(W/"home/native_host/com.opendownloader.host.chromium.json", udd/"NativeMessagingHosts/com.opendownloader.host.json")
subprocess.run([sys.executable, str(ROOT / "extension" / "build.py")], check=True, capture_output=True)
ext = str(ROOT / "extension" / "dist" / "chromium")

def items():
    try: return json.loads((W/"items.json").read_text())
    except Exception: return []
def wait(pred, t=40):
    end=time.time()+t
    while time.time()<end:
        if pred(): return True
        time.sleep(0.3)
    return False

ok = True
with sync_playwright() as pw:
    chrome = pw.chromium.executable_path
    proc = subprocess.Popen([chrome, f"--user-data-dir={udd}", "--headless=new", "--remote-debugging-port=9333", "--no-first-run",
        "--no-sandbox", f"--disable-extensions-except={ext}", f"--load-extension={ext}", "about:blank"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    time.sleep(3)
    browser = pw.chromium.connect_over_cdp("http://127.0.0.1:9333")
    ctx = browser.contexts[0]
    cdp = browser.new_browser_cdp_session()
    cdp.send("Browser.setDownloadBehavior", {"behavior": "default"})
    sw = ctx.service_workers[0] if ctx.service_workers else ctx.wait_for_event("serviceworker", timeout=15000)
    print("extension id:", sw.url.split("/")[2])
    page = ctx.new_page()
    page.goto(base + "/page.html")
    page.wait_for_timeout(4000)
    media_state = sw.evaluate("JSON.stringify([...tabMedia.entries()].map(([k,v])=>[k, v.map(m=>({kind:m.kind,label:m.label,url:m.url.split('/').pop(),variants:(m.variants||[]).length}))]))")
    print("detected media:", media_state)
    panel = page.evaluate("(() => { const p=document.querySelector('opendownloader-panel'); return p ? p.style.display : 'none-found'; })()")
    print("video panel display:", panel)
    print("video state:", page.evaluate("(() => { const v=document.getElementById('v'); const r=v.getBoundingClientRect(); return {paused:v.paused, t:v.currentTime, src:v.currentSrc, w:r.width, h:r.height, top:r.top, n:document.querySelectorAll('opendownloader-panel').length, err: v.error && v.error.code}; })()"))
    ok &= panel == "block"
    ok &= '"hls"' in media_state and "master.m3u8" in media_state and "movie.webm" in media_state and "v.m3u8" not in media_state
    page.set_viewport_size({"width": 900, "height": 520})
    page.wait_for_timeout(1200)
    pos = page.evaluate("(() => { const p=document.querySelector('opendownloader-panel'); return [parseFloat(p.style.left), parseFloat(p.style.top)]; })()")
    page.mouse.click(pos[0] + 60, pos[1] + 12)
    page.wait_for_timeout(800)
    page.screenshot(path=str(W / "inpage.png"))
    status = sw.evaluate("pingApp()")
    print("ping:", status)
    # 1) download capture
    page.click("#zip")
    ok &= wait(lambda: any(i["filename"]=="archive.zip" and i["status"]=="Complete" for i in items()))
    z = [i for i in items() if i["filename"]=="archive.zip"]
    print("captured download:", z)
    ok &= bool(z) and "sess=abc123" in (z[0]["cookie"] or "") and z[0]["referer"].endswith("/page.html")
    browser_dl = sw.evaluate("api.downloads.search({}).then(r=>r.map(d=>d.state+':'+d.filename))")
    print("browser download list after takeover:", browser_dl)
    # 1b) torrents: a magnet click and a .torrent download both go to the app
    page.click("#mag")
    page.click("#tor")
    def logged(sub):
        try: return sub in (W/"msgs.log").read_text()
        except OSError: return False
    got_mag = wait(lambda: logged("magnet:?xt=urn:btih:0123456789abcdef"), 15)
    got_tor = wait(lambda: logged("pack.torrent"), 15)
    print("magnet click reached app:", got_mag, "| .torrent download reached app:", got_tor)
    print("page url after magnet click:", page.url)
    ok &= got_mag and got_tor and page.url.endswith("/page.html")
    # 2) video capture: HLS master via the same path as the in-page button
    tab_id = sw.evaluate("[...tabMedia.keys()][0]")
    r = sw.evaluate(f"downloadMedia(tabMedia.get({tab_id}).find(m=>m.master), {tab_id})")
    print("downloadMedia ->", r)
    ok &= wait(lambda: any(i["filename"]=="captured.mp4" and i["status"]=="Complete" for i in items()), 60)
    print("items:", json.dumps(items(), indent=1))
    proc.terminate()
app.terminate()
print("messages:", (W/"msgs.log").read_text()[:1500])
print("RESULT", "PASS" if ok else "FAIL")
sys.exit(0 if ok else 1)
