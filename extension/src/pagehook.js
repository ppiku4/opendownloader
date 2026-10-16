/* OpenDownloader page helper - runs in the page's own JavaScript world (MAIN), at document_start.
 *
 * Purpose: tell the content script WHICH network URL feeds WHICH <video>/<audio>, so the button on
 * a video can offer that video's own stream instead of an anonymous list of everything on the tab.
 *
 * It only observes: every wrapped function calls the original and returns its result unchanged.
 * Nothing is sent anywhere except to the extension's own content script in the same tab, as DOM
 * events carrying JSON strings:
 *   "__od_ev"      on a media element (bubbles, composed) or on document for detached players:
 *                  detail = JSON {url, why, detached?, playing?, src?}
 *   "__od_nav"     on document when the page changes its address without reloading
 *   "__od_replay"  sent BY the content script on an element (or document) to get past evidence again
 *
 * Evidence ("this element was being worked on while this URL was requested"):
 *   - src / srcObject assignment, blob: URLs made from a fetched file or from a MediaSource
 *   - requests opened while the page's player code is busy with one element: the "current element"
 *     is whichever media element (or its MediaSource / SourceBuffer) the page touched last in the
 *     current task. hls.js, dash.js, shaka, video.js all read media.currentTime / buffered or call
 *     SourceBuffer methods right before loading the next piece.
 *   - play() on an <audio>/<video> that is not in the document (custom audio players).
 */
(() => {
  "use strict";
  if (window.__odHook) return;
  Object.defineProperty(window, "__odHook", { value: true });

  const MEDIA = window.HTMLMediaElement && HTMLMediaElement.prototype;
  if (!MEDIA) return;

  const blobSource = new Map();        // blob: URL -> source URL (file) or MediaSource
  const blobTag = new WeakMap();       // Blob -> URL it was downloaded from
  const msOwner = new WeakMap();       // MediaSource -> media element
  const sbOwner = new WeakMap();       // SourceBuffer -> MediaSource
  const evidence = new WeakMap();      // element -> [{url, why}]
  const detached = new Set();          // detached players we've reported
  let ctx = null;                      // element the page is working on in this task
  let ctxTimer = 0;

  const SKIP = /\.(js|mjs|css|png|jpe?g|gif|webp|avif|svg|ico|woff2?|ttf|otf|json|html?)(\?|#|$)|^data:|^blob:/i;

  function setCtx(el) {
    if (!el) return;
    ctx = el;
    if (!ctxTimer) ctxTimer = setTimeout(() => { ctx = null; ctxTimer = 0; }, 0);
  }

  function abs(u) { try { return new URL(String(u), location.href).href; } catch { return ""; } }

  function emit(el, url, why) {
    if (!url) return;
    let list = evidence.get(el);
    if (!list) { list = []; evidence.set(el, list); }
    if (list.some((e) => e.url === url)) return;
    list.push({ url, why });
    if (list.length > 300) list.splice(0, list.length - 300);
    send(el, { url, why });
  }

  function send(el, data) {
    try {
      if (el.isConnected) {
        el.dispatchEvent(new CustomEvent("__od_ev", { bubbles: true, composed: true, detail: JSON.stringify(data) }));
      } else {
        detached.add(el);
        document.dispatchEvent(new CustomEvent("__od_ev", { detail: JSON.stringify({ ...data, detached: true,
          playing: !el.paused, kind: el.tagName.toLowerCase() }) }));
      }
    } catch { /* never break the page */ }
  }

  // ------------------------------------------------------------ requests
  function onRequest(url) {
    if (!ctx || !url || SKIP.test(url)) return;
    emit(ctx, abs(url), "request");
  }
  const XHR = XMLHttpRequest.prototype;
  const xOpen = XHR.open;
  XHR.open = function (method, url) {
    try { this.__odUrl = abs(url); onRequest(this.__odUrl); } catch { /* ignore */ }
    return xOpen.apply(this, arguments);
  };
  const xSend = XHR.send;
  XHR.send = function () {
    try {
      if (this.__odUrl) onRequest(this.__odUrl);
      const xhr = this;
      xhr.addEventListener("load", () => {
        try { if (xhr.response instanceof Blob) blobTag.set(xhr.response, xhr.responseURL || xhr.__odUrl); } catch { /* ignore */ }
      });
    } catch { /* ignore */ }
    return xSend.apply(this, arguments);
  };
  const oFetch = window.fetch;
  if (oFetch) {
    window.fetch = function (input) {
      try { onRequest(typeof input === "string" || input instanceof URL ? String(input) : input && input.url); } catch { /* ignore */ }
      return oFetch.apply(this, arguments);
    };
  }
  if (window.Response) {
    const oBlob = Response.prototype.blob;
    Response.prototype.blob = function () {
      const url = this.url;
      return oBlob.apply(this, arguments).then((b) => { try { if (url) blobTag.set(b, url); } catch { /* ignore */ } return b; });
    };
  }

  // ------------------------------------------------------------ blob: URLs
  const oCreate = URL.createObjectURL;
  URL.createObjectURL = function (obj) {
    const u = oCreate.apply(this, arguments);
    try {
      if (window.MediaSource && obj instanceof MediaSource) blobSource.set(u, obj);
      else if (obj instanceof Blob && blobTag.has(obj)) blobSource.set(u, blobTag.get(obj));
      if (blobSource.size > 500) blobSource.delete(blobSource.keys().next().value);
    } catch { /* ignore */ }
    return u;
  };

  function sourceAssigned(el, value) {
    try {
      const v = typeof value === "string" ? abs(value) : value;
      if (window.MediaSource && v instanceof MediaSource) { msOwner.set(v, el); return; }
      if (typeof v !== "string" || !v) return;
      if (/^blob:/i.test(v)) {
        const s = blobSource.get(v);
        if (window.MediaSource && s instanceof MediaSource) msOwner.set(s, el);
        else if (typeof s === "string") emit(el, s, "src");
      } else if (/^https?:/i.test(v)) {
        emit(el, v, "src");
      }
    } catch { /* ignore */ }
  }

  function hookSetter(proto, prop, fn) {
    const d = Object.getOwnPropertyDescriptor(proto, prop);
    if (!d || !d.set) return;
    Object.defineProperty(proto, prop, { ...d, set(v) { fn(this, v); return d.set.call(this, v); } });
  }
  function hookGetter(proto, prop, fn) {
    const d = Object.getOwnPropertyDescriptor(proto, prop);
    if (!d || !d.get) return;
    Object.defineProperty(proto, prop, { ...d, get() { fn(this); return d.get.call(this); } });
  }
  function hookMethod(proto, name, fn) {
    const o = proto[name];
    if (typeof o !== "function") return;
    proto[name] = function () { fn(this, arguments); return o.apply(this, arguments); };
  }

  hookSetter(MEDIA, "src", (el, v) => { setCtx(el); sourceAssigned(el, v); });
  hookSetter(MEDIA, "srcObject", (el, v) => { setCtx(el); sourceAssigned(el, v); });
  hookMethod(Element.prototype, "setAttribute", (el, a) => {
    if (el instanceof HTMLMediaElement && String(a[0]).toLowerCase() === "src") { setCtx(el); sourceAssigned(el, a[1]); }
    else if (window.HTMLSourceElement && el instanceof HTMLSourceElement && String(a[0]).toLowerCase() === "src"
             && el.parentElement instanceof HTMLMediaElement) sourceAssigned(el.parentElement, a[1]);
  });
  for (const p of ["currentTime", "buffered", "readyState", "duration", "seeking", "networkState"]) {
    hookGetter(MEDIA, p, setCtx);
  }
  hookMethod(MEDIA, "load", (el) => setCtx(el));
  hookMethod(MEDIA, "play", (el) => {
    setCtx(el);
    try {
      if (!el.isConnected) {
        const cur = el.currentSrc || el.src || "";
        const s = /^blob:/i.test(cur) ? blobSource.get(cur) : cur;
        if (typeof s === "string" && s) {
          const data = { url: abs(s), why: "play", kind: el.tagName.toLowerCase() };
          if (!detached.has(el)) {
            el.addEventListener("pause", () => send(el, { ...data, why: "pause" }));
            el.addEventListener("ended", () => send(el, { ...data, why: "pause" }));
          }
          detached.add(el);
          document.dispatchEvent(new CustomEvent("__od_ev", { detail: JSON.stringify({ ...data, detached: true, playing: true }) }));
        }
      }
    } catch { /* ignore */ }
  });

  if (window.MediaSource) {
    const MS = MediaSource.prototype;
    const owner = (ms) => msOwner.get(ms);
    hookMethod(MS, "addSourceBuffer", (ms) => setCtx(owner(ms)));
    hookMethod(MS, "endOfStream", (ms) => setCtx(owner(ms)));
    hookGetter(MS, "readyState", (ms) => setCtx(owner(ms)));
    hookGetter(MS, "duration", (ms) => setCtx(owner(ms)));
    const oAdd = MS.addSourceBuffer;
    MS.addSourceBuffer = function () {
      const sb = oAdd.apply(this, arguments);
      try { sbOwner.set(sb, this); } catch { /* ignore */ }
      return sb;
    };
    if (window.SourceBuffer) {
      const SB = SourceBuffer.prototype;
      const sbEl = (sb) => owner(sbOwner.get(sb));
      for (const m of ["appendBuffer", "remove", "abort", "changeType"]) hookMethod(SB, m, (sb) => setCtx(sbEl(sb)));
      hookGetter(SB, "buffered", (sb) => setCtx(sbEl(sb)));
      hookGetter(SB, "updating", (sb) => setCtx(sbEl(sb)));
    }
  }

  // ------------------------------------------------------------ address changes without reload
  function navigated() {
    try { document.dispatchEvent(new CustomEvent("__od_nav", { detail: JSON.stringify({ url: location.href }) })); } catch { /* ignore */ }
  }
  for (const m of ["pushState", "replaceState"]) {
    const o = history[m];
    if (typeof o !== "function") continue;
    history[m] = function () {
      const before = location.href;
      const r = o.apply(this, arguments);
      if (location.href !== before) navigated();
      return r;
    };
  }
  addEventListener("popstate", navigated);

  // ------------------------------------------------------------ replay for the content script
  document.addEventListener("__od_replay", (e) => {
    const t = e.target;
    const els = t instanceof HTMLMediaElement ? [t] : [...detached];
    for (const el of els) {
      for (const ev of evidence.get(el) || []) send(el, ev);
      if (!el.isConnected && !el.paused) {
        const cur = el.currentSrc || el.src || "";
        const s = /^blob:/i.test(cur) ? blobSource.get(cur) : cur;
        if (typeof s === "string" && s) send(el, { url: abs(s), why: "play" });
      }
    }
  }, true);
})();
