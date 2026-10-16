/* OpenDownloader content script:
 *  - floating "Download this video" button over playing videos
 *  - Alt+click on a link = let the browser download it (bypass capture)
 *  - collects links for "Download all links"
 */
(() => {
  "use strict";
  if (window.__openDownloaderInjected) return;
  window.__openDownloaderInjected = true;

  const api = globalThis.browser ?? globalThis.chrome;
  // Same stamp is written into background.js by build.py. If they differ, the browser is
  // still running an older background script (files were updated while it ran).
  const EXT_BUILD = "__EXT_BUILD__";
  let staleBackground = false;
  const RELOAD_HINT = "OpenDownloader was updated. Open chrome://extensions (Firefox: about:addons) " +
                      "and click Reload \u21bb on OpenDownloader, then refresh this page.";
  const MIN_W = 200, MIN_H = 110;
  const panels = new Map();      // video -> { host, root, btn, menu, dismissed }
  let mediaCount = 0;
  let audioCount = 0;          // audio streams/files the extension saw on this tab
  let pagePanel = null;        // floating "Download audio" button for players without a visible element
  let panelAllowed = true;
  const evidence = new WeakMap(); // media element -> Map(url -> why), reported by pagehook.js
  let pageAudio = null;           // {url, playing, time}: hidden <audio> playing outside the document
  let mouse = { x: -1, y: -1 };
  const HOVER_GRACE = 1500;       // keep the button this long after the mouse leaves its video

  const send = (msg) => new Promise((resolve) => {
    try { api.runtime.sendMessage(msg, (r) => { void api.runtime.lastError; resolve(r); }); }
    catch { resolve(null); }
  });

  // ------------------------------------------------------------ evidence from pagehook.js
  // pagehook.js runs in the page's own world and reports which URL feeds which media element.
  document.addEventListener("__od_ev", (e) => {
    let d;
    try { d = JSON.parse(e.detail); } catch { return; }
    if (!d || !d.url) return;
    if (d.detached) {
      if (d.why === "play" || d.why === "pause" || d.why === "src") {
        if (d.why === "pause" && pageAudio && pageAudio.url !== d.url) return;
        pageAudio = { url: d.url, playing: d.why !== "pause", time: Date.now(), kind: d.kind || "audio" };
        schedule();
      } else if (pageAudio && pageAudio.playing) {
        (pageAudio.evidence ||= new Set()).add(d.url);   // MSE-fed hidden audio
      }
      return;
    }
    const el = e.composedPath ? e.composedPath()[0] : e.target;
    if (!(el instanceof HTMLMediaElement)) return;
    let m = evidence.get(el);
    if (!m) { m = new Map(); evidence.set(el, m); }
    if (m.size < 300) m.set(d.url, d.why);
  }, true);

  function evidenceUrls(el) {
    if (!el) return pageAudio ? [pageAudio.url, ...(pageAudio.evidence || [])] : [];
    const m = evidence.get(el);
    return m ? [...m.keys()] : [];
  }

  // ------------------------------------------------------------ address changes without reload
  // Single-page sites (Artlist & co.) change the address without loading a new page, so the
  // background would otherwise keep listing the previous page's streams.
  let lastHref = location.href;
  function checkNavigation() {
    if (location.href === lastHref) return;
    lastHref = location.href;
    const since = Date.now();
    for (const p of panels.values()) p.dismissed = false;
    if (pagePanel) pagePanel.dismissed = false;
    if (pageAudio && !pageAudio.playing) pageAudio = null;
    // give the new page a moment to start its players, then keep only what's still in use
    setTimeout(() => {
      const keep = [];
      for (const v of allMedia()) keep.push(...evidenceUrls(v), v.currentSrc || "");
      if (pageAudio) keep.push(...evidenceUrls(null));
      send({ type: "pageChanged", since, keep: keep.filter((u) => /^https?:/i.test(u)).slice(0, 2000), url: location.href })
        .then((r) => { if (r && typeof r.count === "number") { mediaCount = r.count; audioCount = r.audio || 0; schedule(); } });
    }, 1500);
  }
  document.addEventListener("__od_nav", () => setTimeout(checkNavigation, 0), true);
  addEventListener("popstate", () => setTimeout(checkNavigation, 0));

  // ------------------------------------------------------------ alt-click bypass
  document.addEventListener("click", (e) => {
    if (!e.altKey) return;
    const a = e.target instanceof Element ? e.target.closest("a[href]") : null;
    if (a && /^(https?|ftp):/i.test(a.href)) send({ type: "bypassNext", url: a.href });
  }, true);

  // ------------------------------------------------------------ magnet links
  // Plain click on a magnet: link opens it in OpenDownloader. Alt+click, or no answer from the
  // app, lets the browser handle it as usual.
  let magnetPassthrough = "";
  document.addEventListener("click", (e) => {
    if (e.altKey || e.button !== 0 || e.ctrlKey || e.metaKey || e.shiftKey) return;
    const a = e.target instanceof Element ? e.target.closest("a[href]") : null;
    if (!a || !/^magnet:\?/i.test(a.href)) return;
    if (a.href === magnetPassthrough) { magnetPassthrough = ""; return; }
    e.preventDefault();
    e.stopPropagation();
    send({ type: "magnet", url: a.href }).then((r) => {
      if (r && r.ok) return;
      magnetPassthrough = a.href;
      a.click();
    });
  }, true);

  // ------------------------------------------------------------ collect links
  api.runtime.onMessage.addListener((msg, _sender, sendResponse) => {
    if (msg.type === "collectLinks") {
      sendResponse(collectLinks(msg.selectionOnly));
    } else if (msg.type === "mediaUpdated") {
      mediaCount = msg.count || 0;
      audioCount = msg.audio || 0;
      scan();
    }
    return false;
  });

  function collectLinks(selectionOnly) {
    let scope = [document];
    if (selectionOnly) {
      const sel = window.getSelection();
      if (sel && sel.rangeCount) {
        const frag = sel.getRangeAt(0).cloneContents();
        const div = document.createElement("div");
        div.appendChild(frag);
        scope = [div];
      }
    }
    const out = new Set();
    for (const root of scope) {
      root.querySelectorAll("a[href], area[href]").forEach((a) => {
        try { const u = new URL(a.getAttribute("href"), location.href).href; if (/^(https?|ftp):/.test(u)) out.add(u); } catch { /* bad url */ }
      });
      root.querySelectorAll("img[src], video[src], audio[src], source[src], embed[src]").forEach((el) => {
        try { const u = new URL(el.getAttribute("src"), location.href).href; if (/^https?:/.test(u)) out.add(u); } catch { /* bad url */ }
      });
    }
    return [...out];
  }

  // ------------------------------------------------------------ video panel
  const CSS = `
    :host { all: initial; }
    .wrap { font: 12px/1.3 system-ui, -apple-system, "Segoe UI", sans-serif; position: relative; }
    .btn { display: flex; align-items: center; gap: 6px; cursor: pointer; user-select: none;
      background: linear-gradient(#2d8cf0, #1666c5); color: #fff; border-radius: 6px; padding: 5px 9px 5px 7px;
      box-shadow: 0 2px 8px rgba(0,0,0,.35); opacity: .88; transition: opacity .15s; white-space: nowrap; }
    .btn:hover { opacity: 1; }
    .btn svg { width: 16px; height: 16px; flex: none; }
    .x { margin-left: 4px; opacity: .7; padding: 0 3px; border-radius: 3px; }
    .x:hover { opacity: 1; background: rgba(255,255,255,.2); }
    .menu { position: absolute; right: 0; top: calc(100% + 4px); min-width: 260px; max-width: 420px; max-height: 320px;
      overflow: auto; background: #fff; color: #222; border-radius: 8px; box-shadow: 0 6px 24px rgba(0,0,0,.35);
      padding: 4px; display: none; }
    .menu.open { display: block; }
    .item { padding: 7px 10px; border-radius: 5px; cursor: pointer; display: flex; justify-content: space-between; gap: 12px; }
    .item:hover { background: #e8f1fd; }
    .item.disabled { color: #999; cursor: default; background: none; }
    .sub { color: #777; font-size: 11px; }
    .item.msg, .item.wrap { color: #333; white-space: normal; line-height: 1.4; display: block; }
    .head { padding: 6px 10px 4px; color: #666; font-size: 11px; text-transform: uppercase; letter-spacing: .04em; }
    .foot { border-top: 1px solid #eee; margin-top: 4px; padding: 6px 10px; color: #888; font-size: 11px; cursor: pointer; }
    .foot:hover { color: #333; }
  `;
  function iconSvg() {
    const NS = "http://www.w3.org/2000/svg";
    const svg = document.createElementNS(NS, "svg");
    svg.setAttribute("viewBox", "0 0 24 24");
    svg.setAttribute("fill", "none");
    svg.setAttribute("stroke", "#fff");
    svg.setAttribute("stroke-width", "2.6");
    svg.setAttribute("stroke-linecap", "round");
    svg.setAttribute("stroke-linejoin", "round");
    const path = document.createElementNS(NS, "path");
    path.setAttribute("d", "M12 4v11M7 10l5 5 5-5M5 20h14");
    svg.appendChild(path);
    return svg;
  }

  function makePanel(video, text = "Download this video") {
    const host = document.createElement("opendownloader-panel");
    host.style.cssText = "position:fixed;z-index:2147483647;top:0;left:0;display:none;";
    const root = host.attachShadow({ mode: "closed" });
    const style = el("style", "", CSS);
    const wrap = el("div", "wrap");
    const btn = el("div", "btn");
    const x = el("span", "x", "✕");
    x.title = "Hide";
    btn.append(iconSvg(), el("span", "", text), x);
    const menu = el("div", "menu");
    wrap.append(btn, menu);
    root.append(style, wrap);
    const p = { host, root, btn, menu, dismissed: false, menuOpen: false, hoverUntil: 0, lastRect: null };
    host.addEventListener("mouseenter", () => { p.hoverUntil = Infinity; });
    host.addEventListener("mouseleave", () => { p.hoverUntil = Date.now() + HOVER_GRACE; schedule(); setTimeout(schedule, HOVER_GRACE + 50); });
    btn.addEventListener("click", (e) => {
      e.stopPropagation(); e.preventDefault();
      if (e.target.classList.contains("x")) { p.dismissed = true; host.style.display = "none"; return; }
      if (menu.classList.contains("open")) { menu.classList.remove("open"); p.menuOpen = false; }
      else openMenu(video, p);
    });
    host.addEventListener("mousedown", (e) => e.stopPropagation());
    menu.addEventListener("click", (e) => e.stopPropagation()); // clicking a heading/padding keeps it open
    document.addEventListener("click", () => { menu.classList.remove("open"); p.menuOpen = false; });
    (document.fullscreenElement || document.documentElement).appendChild(host);
    return p;
  }

  function el(tag, cls, text) {
    const e = document.createElement(tag);
    if (cls) e.className = cls;
    if (text !== undefined) e.textContent = text;
    return e;
  }

  // Site players (YouTube etc.) don't expose a file or HLS/DASH manifest - their videos
  // are fetched piecewise into a blob: source. Those are handed to the app by page URL
  // and downloaded with yt-dlp (quality picked in the app).
  const YT_HOST = /(^|\.)youtube(-nocookie)?\.com$/;
  function pageVideoUrl() {
    if (YT_HOST.test(location.hostname)) {
      const embed = location.pathname.match(/^\/embed\/([\w-]{6,})/);
      if (embed) return `https://www.youtube.com/watch?v=${embed[1]}`;
      const v = new URLSearchParams(location.search).get("v");
      if (location.pathname === "/watch" && v) return `https://www.youtube.com/watch?v=${v}`;
      const shorts = location.pathname.match(/^\/shorts\/([\w-]{6,})/);
      if (shorts) return `https://www.youtube.com/shorts/${shorts[1]}`;
      return null; // home page / feed previews
    }
    return location.href;
  }
  function pageTitle() {
    return (document.title || "").replace(/^\(\d+\)\s*/, "").replace(/\s+-\s+YouTube$/, "").trim();
  }

  function mediaItems(m, audioMode) {
    if (m.master && m.variants && m.variants.length) {
      const out = [{ head: m.label }];
      for (const v of m.variants) {
        out.push({ label: v.height ? `${v.height}p` : "Stream", sub: v.bandwidth ? `${(v.bandwidth / 1e6).toFixed(1)} Mbps` : "",
                   msg: { type: "downloadMedia", url: m.url, variant: v }, disabled: m.protected });
      }
      return out;
    }
    return [{ label: m.label + (m.protected ? " \u{1F512} DRM" : ""), sub: m.name || "",
              msg: { type: "downloadMedia", url: m.url, audio: audioMode && !m.video }, disabled: m.protected }];
  }

  async function openMenu(video, p) {
    const r = await send({ type: "getMedia", pageUrl: location.href, evidence: evidenceUrls(video).slice(-300) });
    let media = (r && r.media) || [];
    const audioMode = !video || video.tagName === "AUDIO";
    const menu = p.menu;
    menu.textContent = "";
    p.menuOpen = true;
    const items = [];
    // 1) what feeds THIS player (matched by pagehook.js evidence)
    const ownUrls = (r && r.own) || [];
    const own = ownUrls.map((u) => media.find((m) => m.url === u)).filter(Boolean);
    const ownItems = [];
    for (const m of own) ownItems.push(...mediaItems(m, audioMode));
    const src = video ? (video.currentSrc || video.src) : "";
    const files = [...((r && r.ownFiles) || [])];
    if (src && /^https?:/i.test(src) && !files.includes(src)) files.unshift(src);
    if (!video && pageAudio && /^https?:/i.test(pageAudio.url) && !files.includes(pageAudio.url)
        && !media.some((m) => m.url === pageAudio.url)) files.unshift(pageAudio.url);
    for (const f of files) {
      if (media.some((m) => m.url === f) || own.some((m) => m.url === f)) continue;
      ownItems.push({ label: audioMode ? "This audio file" : "This video file",
                      sub: decodeURIComponent(f.split("/").pop().split("?")[0]).slice(0, 50),
                      msg: { type: "downloadMedia", url: f, kind: "file", audio: audioMode } });
    }
    if (!video && pageAudio) {
      const pm = media.find((m) => m.url === pageAudio.url);
      if (pm && !own.includes(pm)) ownItems.unshift(...mediaItems(pm, true));
    }
    if (ownItems.length) {
      items.push({ head: video ? (audioMode ? "This audio" : "This video") : pageAudio && pageAudio.playing ? "Playing now" : "Last played" });
      items.push(...ownItems);
    }
    // 2) everything else seen on this tab
    if (audioMode && media.some((m) => m.audio)) media = media.filter((m) => m.audio || m.kind === "file" && !m.video);
    const rest = media.filter((m) => !own.includes(m) && !(pageAudio && !video && m.url === pageAudio.url));
    const restItems = [];
    for (const m of rest.slice().reverse().slice(0, 15)) restItems.push(...mediaItems(m, audioMode));
    if (restItems.length) {
      if (items.length) items.push({ head: "Other media on this page" });
      items.push(...restItems);
    }
    const pageUrl = pageVideoUrl();
    const pageMsg = pageUrl && { type: "downloadPage", url: pageUrl, title: pageTitle(),
                                 preferHeight: (video && video.videoHeight) || undefined, preferAudio: audioMode };
    if (YT_HOST.test(location.hostname)) {
      items.length = 0; // YouTube's own chunks are useless on their own
      if (pageMsg) {
        items.push({ head: "YouTube video" });
        items.push({ label: "Choose quality…", sub: "1080p, 720p, audio only…", msg: pageMsg });
      }
    } else if (pageMsg && (audioMode || /^blob:/i.test(src || "") || !items.length) && !ownItems.length) {
      items.push({ head: audioMode ? "Audio player on this page" : "Video player on this page" });
      items.push({ label: audioMode ? "Find the audio on this page…" : "Download from page (choose quality)…",
                   sub: "via yt-dlp", msg: pageMsg });
    } else if (pageMsg && ownItems.length) {
      items.push({ head: "Not working?" });
      items.push({ label: "Find it on the page with yt-dlp…", sub: "", msg: pageMsg });
    }
    if (!items.length) {
      items.push({ label: `No downloadable stream detected yet - play the ${audioMode ? "audio" : "video"} for a moment`,
                   disabled: true });
    }
    if (staleBackground) {
      items.length = 0;
      items.push({ head: "Needs a reload" }, { label: RELOAD_HINT, disabled: true, wrap: true });
    }
    for (const it of items) {
      if (it.head) { menu.appendChild(el("div", "head", it.head)); continue; }
      const row = el("div", "item" + (it.disabled ? " disabled" : "") + (it.wrap ? " wrap" : ""));
      row.appendChild(el("span", "", it.label));
      if (it.sub) row.appendChild(el("span", "sub", it.sub));
      if (!it.disabled && it.msg) {
        row.addEventListener("click", async (e) => {
          e.stopPropagation();
          menu.classList.remove("open");
          p.menuOpen = false;
          const res = await send(it.msg);
          if (res && res.ok) {
            flash(p, "Sent to OpenDownloader \u2713");
          } else {
            const err = res && res.error;
            showMessage(p, err === "unknown" ? RELOAD_HINT
              : err ? `OpenDownloader: ${err}` : "OpenDownloader is not reachable. Is the app installed?");
          }
        });
      }
      menu.appendChild(row);
    }
    const foot = el("div", "foot", "Don't show this button on " + location.hostname);
    foot.addEventListener("click", async (e) => {
      e.stopPropagation();
      await send({ type: "toggleSite", list: "panel", host: location.hostname });
      panelAllowed = false;
      for (const x of panels.values()) x.host.style.display = "none";
      if (pagePanel) pagePanel.host.style.display = "none";
    });
    menu.appendChild(foot);
    menu.classList.add("open");
  }

  function flash(p, text) {
    const span = p.btn.querySelector("span");
    const old = span.textContent;
    span.textContent = text;
    setTimeout(() => { span.textContent = old; }, 3000);
  }

  function showMessage(p, text) {
    const menu = p.menu;
    menu.textContent = "";
    const row = el("div", "item disabled msg", text);
    menu.appendChild(row);
    menu.classList.add("open");
    p.menuOpen = true;
    setTimeout(() => { menu.classList.remove("open"); p.menuOpen = false; }, 12000);
  }


  function isActive(v) { return !v.paused || v.currentTime > 0; }
  function rectVisible(v, r) {
    const isAudio = v.tagName === "AUDIO";
    const [minW, minH] = isAudio ? [100, 14] : [MIN_W, MIN_H];
    return r.width >= minW && r.height >= minH && r.bottom > 0 && r.right > 0 &&
      r.top < innerHeight && r.left < innerWidth && getComputedStyle(v).visibility !== "hidden";
  }

  // hover mode: with several players running at once (preview grids), show the button only on
  // the one under the mouse, and keep it a moment after the mouse leaves so it can be reached.
  function position(video, p, hoverMode) {
    if (p.menuOpen && p.lastRect) return place(video, p, p.lastRect);   // keep an open menu where it is
    if (!panelAllowed || p.dismissed || !video.isConnected) { p.host.style.display = "none"; return false; }
    const r = video.getBoundingClientRect();
    const src = video.currentSrc || video.src || "";
    const hasSource = mediaCount > 0 || /^https?:/i.test(src) || (/^blob:/i.test(src) && !!pageVideoUrl()) ||
      (!YT_HOST.test(location.hostname) && evidenceUrls(video).length > 0);
    if (!rectVisible(video, r) || !hasSource) { p.host.style.display = "none"; return false; }
    const over = mouse.x >= r.left && mouse.x <= r.right && mouse.y >= r.top && mouse.y <= r.bottom;
    if (over) p.hoverUntil = Math.max(p.hoverUntil, Date.now() + HOVER_GRACE);
    const hovered = p.hoverUntil > Date.now();
    hoverMode = hoverMode && video.tagName === "VIDEO";
    if (hoverMode ? !hovered : !(isActive(video) || hovered)) { p.host.style.display = "none"; return false; }
    if (hoverMode && hovered && !over && p.hoverUntil !== Infinity) setTimeout(schedule, p.hoverUntil - Date.now() + 30);
    p.lastRect = r;
    return place(video, p, r);
  }

  function place(video, p, r) {
    const isAudio = video.tagName === "AUDIO";
    const fs = document.fullscreenElement;
    const parent = fs && fs.contains(video) ? fs : document.documentElement;
    if (p.host.parentNode !== parent) parent.appendChild(p.host);
    p.host.style.display = "block";
    const w = p.btn.offsetWidth || 170;
    // audio controls are too short to hold the button: put it just above (or below) the player
    const top = isAudio ? (r.top >= 40 ? r.top - 36 : r.bottom + 6) : r.top + 8;
    p.host.style.top = Math.max(4, top) + "px";
    p.host.style.left = Math.max(4, Math.min(innerWidth - w - 8, r.right - w - (isAudio ? 0 : 10))) + "px";
    return true;
  }

  // <video>/<audio> elements, including ones inside open shadow roots (web-component players)
  let shadowHosts = [];
  let shadowScan = 0;
  function allMedia() {
    const out = [...document.querySelectorAll("video, audio")];
    if (Date.now() - shadowScan > 3000) {
      shadowScan = Date.now();
      shadowHosts = [];
      const walk = (root, depth) => {
        for (const e of root.querySelectorAll("*")) {
          if (e.shadowRoot && e.tagName !== "OPENDOWNLOADER-PANEL") {
            shadowHosts.push(e.shadowRoot);
            if (depth < 4) walk(e.shadowRoot, depth + 1);
          }
        }
      };
      try { walk(document, 0); } catch { /* ignore */ }
    }
    for (const sr of shadowHosts) out.push(...sr.querySelectorAll("video, audio"));
    return out;
  }

  // Custom audio players often play through an invisible <audio> (or one that isn't in the page
  // at all). When the extension has seen audio on this tab but no player button is showing,
  // offer a small floating button in the corner instead.
  function placePagePanel(anyVisible) {
    const hiddenAudio = [...document.querySelectorAll("audio")].some((a) => !a.paused || a.currentTime > 0);
    // media seen on a page without any <video>: almost always an audio player
    const noVideo = mediaCount > 0 && !document.querySelector("video");
    const playingDetached = !!(pageAudio && (pageAudio.playing || Date.now() - pageAudio.time < 60000));
    const want = window.top === window && panelAllowed && (!anyVisible || playingDetached)
      && (audioCount > 0 || hiddenAudio || noVideo || playingDetached) && !YT_HOST.test(location.hostname);
    if (!want && !(pagePanel && pagePanel.menuOpen)) { if (pagePanel) pagePanel.host.style.display = "none"; return; }
    if (!pagePanel) pagePanel = makePanel(null, "Download audio");
    if (pagePanel.dismissed) { pagePanel.host.style.display = "none"; return; }
    const p = pagePanel;
    const label = p.btn.querySelector("span");
    const want_text = pageAudio && pageAudio.playing ? "Download playing audio" : "Download audio";
    if (label && label.textContent !== want_text && !/\u2713/.test(label.textContent)) label.textContent = want_text;
    if (p.host.parentNode !== document.documentElement) document.documentElement.appendChild(p.host);
    p.host.style.display = "block";
    const w = p.btn.offsetWidth || 150;
    p.host.style.top = Math.max(4, innerHeight - 60) + "px";
    p.host.style.left = Math.max(4, innerWidth - w - 24) + "px";
    p.menu.style.top = "auto";
    p.menu.style.bottom = "calc(100% + 4px)";
  }

  function scan() {
    checkNavigation();
    const found = allMedia();
    for (const v of found) {
      if (!panels.has(v)) {
        panels.set(v, makePanel(v, v.tagName === "AUDIO" ? "Download this audio" : "Download this video"));
        v.addEventListener("play", () => schedule());
        try { v.dispatchEvent(new CustomEvent("__od_replay", { bubbles: true, composed: true })); } catch { /* ignore */ }
      }
    }
    // several videos playing at once (preview grids) -> hover mode
    let playing = 0;
    for (const v of panels.keys()) {
      if (v.isConnected && v.tagName === "VIDEO" && isActive(v) && rectVisible(v, v.getBoundingClientRect())) playing++;
    }
    const hoverMode = playing > 1;
    let anyVisible = false;
    for (const [v, p] of panels) {
      if (!v.isConnected && !p.menuOpen) { p.host.remove(); panels.delete(v); continue; }
      anyVisible = position(v, p, hoverMode) || anyVisible;
    }
    placePagePanel(anyVisible);
  }

  let raf = 0;
  const schedule = () => { if (!raf) raf = requestAnimationFrame(() => { raf = 0; scan(); }); };
  addEventListener("scroll", schedule, true);
  let moveTimer = 0;
  addEventListener("mousemove", (e) => {
    mouse = { x: e.clientX, y: e.clientY };
    if (!moveTimer) moveTimer = setTimeout(() => { moveTimer = 0; schedule(); }, 100);
  }, { capture: true, passive: true });
  addEventListener("resize", schedule);
  document.addEventListener("fullscreenchange", schedule);
  document.addEventListener("play", schedule, true);   // media events don't bubble, but they can be captured
  new MutationObserver(schedule).observe(document.documentElement, { childList: true, subtree: true });
  setInterval(scan, 1000);

  send({ type: "hello", build: EXT_BUILD }).then((r) => {
    staleBackground = !r || r.build !== EXT_BUILD;
  });

  try { document.dispatchEvent(new CustomEvent("__od_replay")); } catch { /* ignore */ }
  send({ type: "getMedia", pageUrl: location.href }).then((r) => {
    if (r) {
      mediaCount = (r.media || []).length;
      audioCount = (r.media || []).filter((m) => m.audio && !m.protected).length;
      panelAllowed = r.panelAllowed !== false;
    }
    scan();
  });
})();
