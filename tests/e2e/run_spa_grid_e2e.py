"""End-to-end: stock-media style site (a single-page app with a grid of HLS previews and a hidden audio player).

    python tests/e2e/run_spa_grid_e2e.py
    HLSJS=/path/to/hls.min.js python tests/e2e/run_spa_grid_e2e.py   # use real hls.js for the players

Real Chromium + the extension + native host + app. The site (stock.test) mimics what stock-footage
and music sites do:
  * a grid of 4 looping previews, each an HLS stream played through MediaSource (blob: src), all
    started in the same task, half of them loading the playlist before attaching the <video>;
  * music previews played with `new Audio(url)` that is never added to the page;
  * page changes with history.pushState (no reload);
  * a download the page builds itself (blob: link) next to a normal .zip link.
Checks: hover mode on the grid, the button's menu offers the hovered video's own stream first, the
hidden audio is offered as "playing now", old streams are dropped after an in-page navigation, and the
popup's activity list explains what happened to each download.
"""
import json, os, shutil, subprocess, sys, tempfile, threading, time
from http.server import ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "tests")); sys.path.insert(0, str(ROOT))
import conftest  # noqa: E402
from playwright.sync_api import sync_playwright  # noqa: E402

EXT_ID = "hkhijccfgjfdcidbdfgfbmghhjfnnldj"
W = Path(tempfile.mkdtemp(prefix="e2e-spa-"))
env = {**os.environ, "OPENDOWNLOADER_HOME": str(W / "home"), "DL_DIR": str(W / "dl"),
       "HARNESS_LOG": str(W / "msgs.log"), "HARNESS_ITEMS": str(W / "items.json"),
       "HARNESS_PAGE_ONLY_LOG": "1", "HARNESS_LOG_ONLY": "1"}
os.environ.update(env)

F = conftest.Handler.files = {}
CLIPS = {}
for i, uid in enumerate(["a1f0c2d4", "b7e19a33", "c3d5e7f9", "d2468ace", "e0ff1234"]):
    d = W / "hls" / uid
    d.mkdir(parents=True)
    subprocess.run(["ffmpeg", "-loglevel", "error", "-f", "lavfi", "-i", f"testsrc=duration=6:size=480x270:rate=25",
                    "-vf", f"hue=h={i * 70}", "-c:v", "libvpx-vp9", "-b:v", "250k", "-deadline", "realtime", "-cpu-used", "8",
                    "-f", "hls", "-hls_time", "2", "-hls_playlist_type", "vod", "-hls_segment_type", "fmp4",
                    "-hls_fmp4_init_filename", f"{uid}_720p_init.mp4",
                    "-hls_segment_filename", str(d / f"{uid}_720p_%03d.m4s"), str(d / f"{uid}_720p.m3u8")], check=True)
    for f in d.iterdir():
        ctype = "application/vnd.apple.mpegurl" if f.suffix == ".m3u8" else "video/mp4"
        F[f"/content/footage-hls/{f.name}"] = (f.read_bytes(), {"type": ctype})
    CLIPS[uid] = f"/content/footage-hls/{uid}_720p.m3u8"
subprocess.run(["ffmpeg", "-loglevel", "error", "-f", "lavfi", "-i", "sine=frequency=440:duration=30",
                "-c:a", "libmp3lame", "-b:a", "128k", str(W / "song.mp3")], check=True)
SONG = "/content/song-mp3/9f8e7d6c_preview_1779709459703.mp3"
F[SONG] = ((W / "song.mp3").read_bytes(), {"type": "audio/mpeg"})
F["/files/pack.zip"] = (b"PK" + b"\0" * 200_000, {"type": "application/zip"})

HLSJS = os.environ.get("HLSJS")
if HLSJS:
    F["/static/hls.min.js"] = (Path(HLSJS).read_bytes(), {"type": "application/javascript"})

# A small MediaSource HLS player that behaves like hls.js: loadSource() opens the playlist request at
# once, attachMedia() sets video.src to a MediaSource blob, then a timer reads media.buffered and
# loads the next segment with XHR. (With HLSJS set, real hls.js is used instead.)
PLAYER_JS = r"""
function xhr(url, type, cb) { const x = new XMLHttpRequest(); x.open('GET', url); x.responseType = type;
  x.onload = () => cb(x.response); x.send(); }
function MiniHls() { this.url = null; this.media = null; this.list = null; }
MiniHls.prototype.loadSource = function (url) { this.url = url;
  xhr(url, 'text', (t) => { this.list = t; this._go(); }); };
MiniHls.prototype.attachMedia = function (video) { this.media = video; const ms = this.ms = new MediaSource();
  ms.addEventListener('sourceopen', () => { this.open = true; this._go(); }); video.src = URL.createObjectURL(ms); };
MiniHls.prototype.destroy = function () { clearInterval(this.timer); this.dead = true; };
MiniHls.prototype._go = function () {
  if (!this.open || !this.list || this.sb) return;
  const base = new URL(this.url, location.href);
  const init = this.list.match(/URI="([^"]+)"/)[1];
  this.segs = this.list.split('\n').filter((l) => l && !l.startsWith('#')).map((l) => new URL(l, base).href);
  this.sb = this.ms.addSourceBuffer('video/mp4; codecs="vp09.00.10.08"');
  xhr(new URL(init, base).href, 'arraybuffer', (b) => { this.sb.appendBuffer(b); this.next = 0;
    this.timer = setInterval(() => this._tick(), 150); });
};
MiniHls.prototype._tick = function () {
  if (this.dead || this.loading || this.sb.updating) return;
  const v = this.media, b = v.buffered;
  const ahead = b.length ? b.end(b.length - 1) - v.currentTime : 0;
  if (this.next >= this.segs.length) { if (this.ms.readyState === 'open' && !this.ended) { this.ended = true; this.ms.endOfStream(); } return; }
  if (ahead > 3) return;
  this.loading = true;
  xhr(this.segs[this.next++], 'arraybuffer', (buf) => { this.loading = false; this.sb.appendBuffer(buf); });
};
const Player = window.Hls && window.Hls.isSupported() ? window.Hls : MiniHls;
const players = [];
function start(video, url, loadFirst) {
  const h = new Player();
  if (loadFirst) { h.loadSource(url); h.attachMedia(video); } else { h.attachMedia(video); h.loadSource(url); }
  players.push(h);
}
function stopAll() { players.splice(0).forEach((p) => p.destroy()); }
"""
CLIP_IDS = list(CLIPS)
GRID = "".join(f'<video class=prev data-clip="{u}" muted autoplay loop playsinline width=420 height=236></video>'
               for u in CLIP_IDS[:4])
APP_HTML = f"""<!doctype html><html><head><title>Stock footage - Stock Test</title>
{'<script src="/static/hls.min.js"></script>' if HLSJS else ''}
<style>body{{margin:0;font:14px sans-serif}} #grid{{display:grid;grid-template-columns:repeat(2,430px);gap:8px;padding:8px}}
nav{{height:40px}} .song{{margin:8px}}</style></head><body>
<nav><a id=goclip href="/clip/5">Open clip 5</a> | <button id=song class=song>Play song</button>
<button id=blobdl>Export notes</button> <a id=zip href="/files/pack.zip">pack.zip</a></nav>
<main id=main><div id=grid>{GRID}</div></main>
<script>{PLAYER_JS}
const CLIPS = {json.dumps(CLIPS)};
function showGrid() {{ document.querySelectorAll('video.prev').forEach((v, i) => start(v, CLIPS[v.dataset.clip], i % 2 === 0)); }}
function showClip(id) {{ stopAll(); const m = document.getElementById('main');
  m.innerHTML = '<h1>Clip ' + id + '</h1><video id=hero muted autoplay loop playsinline width=640 height=360></video>';
  start(document.getElementById('hero'), CLIPS['{CLIP_IDS[4]}'], false); }}
document.getElementById('goclip').onclick = (e) => {{ e.preventDefault(); history.pushState({{}}, '', '/clip/5'); showClip(5); }};
let audio = null;
document.getElementById('song').onclick = () => {{ audio = audio || new Audio('{SONG}'); audio.play(); }};
document.getElementById('blobdl').onclick = () => {{ const a = document.createElement('a');
  a.href = URL.createObjectURL(new Blob(['my notes'], {{type: 'text/plain'}})); a.download = 'notes.pdf'; a.click(); }};
if (location.pathname.startsWith('/clip/')) showClip(5); else showGrid();
</script></body></html>"""
F["/"] = (APP_HTML.encode(), {"type": "text/html"})
F["/stock-footage"] = F["/"]
F["/clip/5"] = F["/"]

srv = ThreadingHTTPServer(("127.0.0.1", 0), conftest.Handler); srv.daemon_threads = True
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
(udd / "Default").mkdir()
(udd / "Default/Preferences").write_text(json.dumps({"download": {"default_directory": str(W / "browser-dl"), "prompt_for_download": False}}))
subprocess.run([sys.executable, str(ROOT / "extension" / "build.py")], check=True, capture_output=True)
ext = str(ROOT / "extension" / "dist" / "chromium")


def messages():
    try:
        return [json.loads(x) for x in (W / "msgs.log").read_text().splitlines()]
    except OSError:
        return []


results = []


def check(name, cond, info=""):
    results.append(bool(cond))
    print(("PASS " if cond else "FAIL ") + name + (f"  [{info}]" if info else ""))


with sync_playwright() as pw:
    chrome = pw.chromium.executable_path
    proc = subprocess.Popen([chrome, f"--user-data-dir={udd}", "--headless=new", "--remote-debugging-port=9336",
                             "--no-first-run", "--no-sandbox", "--no-proxy-server", "--autoplay-policy=no-user-gesture-required",
                             f"--host-resolver-rules=MAP stock.test 127.0.0.1:{port}",
                             f"--disable-extensions-except={ext}", f"--load-extension={ext}", "about:blank"],
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    time.sleep(3)
    import atexit
    atexit.register(proc.terminate)
    atexit.register(app.terminate)
    browser = pw.chromium.connect_over_cdp("http://127.0.0.1:9336")
    ctx = browser.contexts[0]
    browser.new_browser_cdp_session().send("Browser.setDownloadBehavior", {"behavior": "default"})
    page = ctx.new_page()
    page.set_viewport_size({"width": 1000, "height": 700})

    def shown_panels():
        return page.evaluate("""[...document.querySelectorAll('opendownloader-panel')]
            .filter(p => p.style.display === 'block').map(p => [parseFloat(p.style.left), parseFloat(p.style.top)])""")

    def video_rects():
        return page.evaluate("[...document.querySelectorAll('video')].map(v => { const r = v.getBoundingClientRect();"
                             " return [r.left, r.top, r.right, r.bottom, v.paused, v.currentTime]; })")

    def click_first_row(pos):
        """Open the button's menu and click its first entry (closed shadow DOM: probe by position)."""
        page.mouse.click(pos[0] + 60, pos[1] + 12)
        page.wait_for_timeout(800)
        before = len(messages())
        for dy in range(30, 200, 6):
            x, y = pos[0] - 40, pos[1] + dy
            hit = page.evaluate(f"(() => {{ const e = document.elementFromPoint({x}, {y}); return e && e.tagName; }})()")
            if hit == "OPENDOWNLOADER-PANEL":
                page.mouse.click(x, y)
                page.wait_for_timeout(1500)
                if len(messages()) > before:
                    return messages()[-1]
        return None

    page.goto("http://stock.test/stock-footage")
    page.wait_for_timeout(5000)
    rects = video_rects()
    print("videos:", [(round(r[5], 1), r[4]) for r in rects])
    check("grid previews are playing", sum(1 for r in rects if r[5] > 0) >= 3, rects)
    page.mouse.move(990, 690)
    page.wait_for_timeout(2500)
    check("hover mode: no buttons while the mouse is away from the grid", shown_panels() == [], shown_panels())

    # hover the 3rd preview -> button on that one only
    r = rects[2]
    page.mouse.move((r[0] + r[2]) / 2, (r[1] + r[3]) / 2)
    page.wait_for_timeout(700)
    sp = shown_panels()
    on3 = len(sp) == 1 and r[0] <= sp[0][0] <= r[2] and r[1] <= sp[0][1] <= r[3]
    check("hover mode: one button, on the hovered preview", on3, sp)
    # grace period: leave the video, button stays ~1.5 s
    page.mouse.move(990, 690)
    page.wait_for_timeout(500)
    check("button stays briefly after the mouse leaves", len(shown_panels()) == 1)
    page.wait_for_timeout(2000)
    check("button hides after the grace period", shown_panels() == [])

    # menu: first entry = this preview's own stream
    page.mouse.move((r[0] + r[2]) / 2, (r[1] + r[3]) / 2)
    page.wait_for_timeout(700)
    sp = shown_panels()
    msg = click_first_row(sp[0]) if sp else None
    want = CLIPS[CLIP_IDS[2]]
    got = msg and msg.get("data", {}).get("url", "")
    check("menu's first entry is the hovered preview's own stream", got and got.endswith(want), got)
    # and for a preview that loaded its playlist before attaching (index 1 attached first, 0 loaded first)
    for idx in (0, 1, 3):
        r2 = rects[idx]
        page.mouse.click(5, 690)  # close menus
        page.mouse.move((r2[0] + r2[2]) / 2, (r2[1] + r2[3]) / 2)
        page.wait_for_timeout(800)
        sp = [p for p in shown_panels() if r2[0] <= p[0] <= r2[2] and r2[1] <= p[1] <= r2[3]]
        m2 = click_first_row(sp[0]) if sp else None
        got = m2 and m2.get("data", {}).get("url", "")
        check(f"preview {idx + 1}: own stream first", got and got.endswith(CLIPS[CLIP_IDS[idx]]), got)

    # hidden Audio() player
    page.mouse.click(5, 690)
    page.click("#song")
    page.wait_for_timeout(2500)
    page.mouse.move(990, 690)
    page.wait_for_timeout(2000)
    sp = shown_panels()
    corner = [p for p in sp if p[1] > 600]
    check("hidden audio: corner button appears", len(corner) == 1, sp)
    if corner:
        page.mouse.click(corner[0][0] + 60, corner[0][1] + 12)
        page.wait_for_timeout(800)
        before = len(messages())
        got = None
        for dy in range(-200, 0, 6):   # the corner menu opens upwards
            x, y = corner[0][0] - 40, corner[0][1] + dy
            if page.evaluate(f"(() => {{ const e = document.elementFromPoint({x}, {y}); return e && e.tagName; }})()") == "OPENDOWNLOADER-PANEL":
                page.mouse.click(x, y)
                page.wait_for_timeout(1500)
                if len(messages()) > before:
                    got = messages()[-1]
                    break
        url = got and got.get("data", {}).get("url", "")
        check("hidden audio: first entry is the song that's playing", url and url.endswith(SONG), url)
    page.evaluate("audio && audio.pause()")

    # in-page navigation (pushState): previous streams dropped, single video -> always-visible button
    page.mouse.click(5, 690)
    page.click("#goclip")
    page.wait_for_timeout(4500)
    sp = shown_panels()
    check("after pushState: single video shows its button without hover", len(sp) == 1, sp)
    m3 = click_first_row(sp[0]) if sp else None
    got = m3 and m3.get("data", {}).get("url", "")
    check("after pushState: first entry is the new clip's stream", got and got.endswith(CLIPS[CLIP_IDS[4]]), got)

    # downloads + activity log
    page.mouse.click(5, 690)
    page.click("#blobdl")
    page.wait_for_timeout(1500)
    page.click("#zip")
    page.wait_for_timeout(2500)
    pop = ctx.new_page()
    pop.goto(f"chrome-extension://{EXT_ID}/popup.html")
    pop.wait_for_timeout(1000)
    act = pop.evaluate("new Promise(r => chrome.runtime.sendMessage({type:'getActivity'}, r))")["activity"]
    media = pop.evaluate(f"new Promise(r => chrome.tabs.query({{}}, ts => {{ const t = ts.find(t => (t.url||'').includes('stock.test'));"
                         f" chrome.runtime.sendMessage({{type:'getMedia', tabId: t.id}}, r); }}))")["media"]
    print("streams listed after navigation:", [m["url"].split("/")[-1] for m in media])
    check("after pushState: old grid streams are gone", all(CLIPS[CLIP_IDS[i]].split("/")[-1] not in json.dumps(media) for i in range(4))
          and CLIPS[CLIP_IDS[4]].split("/")[-1] in json.dumps(media))
    for a in act:
        print("activity:", a["captured"], a["file"], "-", a["reason"])
    blob = [a for a in act if a["file"] == "notes.pdf"]
    zipd = [a for a in act if a["file"] == "pack.zip"]
    check("activity explains the blob: download", blob and not blob[0]["captured"] and "blob" in blob[0]["reason"])
    check("activity shows the captured .zip", zipd and zipd[0]["captured"] and "OpenDownloader" in zipd[0]["reason"])
    pop.set_viewport_size({"width": 360, "height": 600})
    pop.evaluate("document.getElementById('activityBox').open = true")
    pop.screenshot(path=str(W / "popup.png"))
    proc.terminate()
app.terminate()
print("work dir:", W)
ok = all(results)
print("RESULT", "PASS" if ok else "FAIL", f"({sum(results)}/{len(results)})")
sys.exit(0 if ok else 1)
