/* OpenDownloader browser integration - background (service worker / event page).
 *
 * 1. Download capture: browser downloads matching the configured file types are
 *    cancelled and handed to the OpenDownloader app (with cookies + referrer).
 * 2. Video capture: network responses are sniffed for HLS (.m3u8), DASH (.mpd)
 *    and direct video/audio files; they're listed per tab (badge, popup and the
 *    in-page "Download this video" button).
 * 3. Context menus: download link / media / all links / page video.
 *
 * Talks to the desktop app through native messaging (host: com.opendownloader.host).
 * DRM-protected streams are detected and marked as not downloadable.
 */
"use strict";

const api = globalThis.browser ?? globalThis.chrome;
const EXT_BUILD = "__EXT_BUILD__"; // replaced by build.py; content.js checks it matches
const HOST = "com.opendownloader.host";
const IS_FIREFOX = typeof globalThis.browser !== "undefined" && !!globalThis.browser.runtime?.getBrowserInfo;

const DEFAULT_EXTS = ("3gp 7z aac ace aif aiff apk arj asf avi bin bz2 deb dmg doc docx epub exe flac flv gz img iso " +
  "lzh m4a m4v mkv mov mp3 mp4 mpa mpe mpeg mpg msi msu ogg ogv opus pdf pkg pps ppt pptx qt ra rar rm rmvb rpm " +
  "rtf sea sit sitx tar tgz torrent wav webm wma wmv xls xlsx xz z zip zst").split(" ");

const DEFAULTS = {
  enabled: true,
  syncWithApp: true,          // use the file-type list configured in the app
  extensions: DEFAULT_EXTS,
  minSizeKB: 0,
  excludedSites: [],
  panelDisabledSites: [],
  showVideoPanel: true,
  captureIncognito: false,
  mediaMinKB: 300,
  captureMagnets: true,       // open magnet: links in OpenDownloader
};

let settings = { ...DEFAULTS };
let appStatus = { ok: false, checked: 0 };
const bypass = new Map();            // url -> expiry (let the browser handle it)
const tabMedia = new Map();          // tabId -> [entry]
const childPlaylists = new Map();    // tabId -> Set(url) (variant playlists of a master)
const reqHeaders = new Map();        // requestId -> headers (short lived)
const segIndex = new Map();          // tabId -> Map(segment path -> playlist entry url)
const kidToMaster = new Map();       // tabId -> Map(variant playlist path -> master entry url)
let activity = [];                   // last downloads and what happened to them (popup "Activity")

// ---------------------------------------------------------------- settings
async function loadSettings() {
  const s = await api.storage.local.get("settings");
  settings = { ...DEFAULTS, ...(s.settings || {}) };
  return settings;
}
const ready = loadSettings().then(restoreMedia);

api.storage.onChanged.addListener((changes, area) => {
  if (area === "local" && changes.settings) settings = { ...DEFAULTS, ...(changes.settings.newValue || {}) };
});

// ---------------------------------------------------------------- native messaging
async function sendNative(msg) {
  try {
    const resp = await api.runtime.sendNativeMessage(HOST, msg);
    appStatus = { ok: !!(resp && resp.ok), checked: Date.now(), error: resp?.error };
    return resp || { ok: false, error: "empty response" };
  } catch (e) {
    const error = String(e?.message || e);
    appStatus = { ok: false, checked: Date.now(), error,
                  notInstalled: /not found|not registered|Specified native messaging host/i.test(error) };
    return { ok: false, error };
  }
}

async function pingApp() {
  const r = await sendNative({ type: "ping" });
  if (r.ok && settings.syncWithApp && Array.isArray(r.captureExtensions) && r.captureExtensions.length) {
    settings.extensions = r.captureExtensions;
    await api.storage.local.set({ settings });
  }
  return { ...appStatus, version: r.version };
}

// ---------------------------------------------------------------- helpers
function extOf(name) {
  const clean = (name || "").split(/[?#]/)[0];
  const last = clean.split(/[\\/]/).pop();
  const i = last.lastIndexOf(".");
  return i > 0 ? last.slice(i + 1).toLowerCase() : "";
}
function baseName(p) { return (p || "").split(/[\\/]/).pop(); }
function fileFromUrl(url) {
  try { return decodeURIComponent(new URL(url).pathname.split("/").pop() || ""); } catch { return ""; }
}
function hostOf(url) { try { return new URL(url).hostname; } catch { return ""; } }
function siteMatches(list, url) {
  const h = hostOf(url);
  return !!h && list.some((s) => s && (h === s || h.endsWith("." + s)));
}
function header(headers, name) {
  const h = (headers || []).find((x) => x.name.toLowerCase() === name);
  return h ? h.value : "";
}

async function cookieString(url, storeId) {
  try {
    const q = { url };
    if (storeId) q.storeId = storeId;
    if (IS_FIREFOX) q.firstPartyDomain = null;
    let list;
    try { list = await api.cookies.getAll(q); } catch { delete q.firstPartyDomain; list = await api.cookies.getAll(q); }
    return list.map((c) => `${c.name}=${c.value}`).join("; ");
  } catch { return ""; }
}

async function buildRequest({ url, referrer, filename, size, mime, tabId, storeId, headers, title, kind, media, preferHeight }) {
  let tab = null;
  if (tabId !== undefined && tabId >= 0) { try { tab = await api.tabs.get(tabId); } catch { /* closed */ } }
  const ref = referrer || tab?.url || "";
  return {
    url, referer: ref, filename: filename || "", fileSize: size || -1, mime: mime || "",
    cookies: await cookieString(url, storeId || tab?.cookieStoreId),
    userAgent: navigator.userAgent, headers: headers || {}, title: title || tab?.title || "",
    kind, media, preferHeight,
  };
}

// ---------------------------------------------------------------- download capture
// Returns [capture?, reason]. The reason is shown in the popup's Activity list.
function captureDecision(item) {
  if (!settings.enabled) return [false, "capture is switched off"];
  const url = item.finalUrl || item.url || "";
  if (/^blob:/i.test(url)) return [false, "the page built this file itself (blob: link) - only the browser can save it"];
  if (/^data:/i.test(url)) return [false, "data: link made by the page - only the browser can save it"];
  if (!/^(https?|ftp):/i.test(url)) return [false, "not a web link (" + url.split(":")[0] + ":)"];
  if (item.byExtensionId && item.byExtensionId === api.runtime.id) return [false, "handed back to the browser by OpenDownloader"];
  const b = bypass.get(url);
  if (b && b > Date.now()) { bypass.delete(url); return [false, "Alt+click or app unavailable - browser keeps it"]; }
  if (item.incognito && !settings.captureIncognito) return [false, "private window (capture is off there)"];
  if (siteMatches(settings.excludedSites, item.referrer || url)) return [false, "capture is disabled on this site"];
  const size = item.totalBytes > 0 ? item.totalBytes : item.fileSize;
  if (settings.minSizeKB > 0 && size > 0 && size < settings.minSizeKB * 1024) return [false, `smaller than ${settings.minSizeKB} KB`];
  const ext = extOf(baseName(item.filename)) || extOf(fileFromUrl(url));
  if (ext && settings.extensions.includes(ext)) return [true, `.${ext} is in the capture list`];
  if (ext) return [false, `.${ext} is not in the capture list`];
  // no usable extension: fall back to the mime type
  if (/^(video|audio)\/|octet-stream|zip|x-msdownload|x-rar|x-7z|pdf/i.test(item.mime || "")) return [true, `type ${item.mime}`];
  return [false, `no file extension and type "${item.mime || "unknown"}" isn't a download type`];
}

function logActivity(item, captured, reason) {
  const url = item.finalUrl || item.url || "";
  const rec = { time: Date.now(), file: baseName(item.filename) || fileFromUrl(url) || "", url: url.slice(0, 300),
                site: hostOf(item.referrer || url), captured, reason };
  const i = activity.findIndex((a) => a.id === item.id);
  rec.id = item.id;
  if (i >= 0) activity[i] = rec; else activity.unshift(rec);
  activity = activity.slice(0, 20);
  try { api.storage.session?.set({ activity }); } catch { /* ignore */ }
}

async function takeOver(item) {
  const url = item.finalUrl || item.url;
  try { await api.downloads.cancel(item.id); } catch { /* already gone */ }
  const req = await buildRequest({
    url, referrer: item.referrer, filename: baseName(item.filename) || "", size: item.totalBytes || item.fileSize,
    mime: item.mime, storeId: item.cookieStoreId,
  });
  const resp = await sendNative({ type: "download", data: req });
  if (resp.ok) {
    logActivity(item, true, "sent to OpenDownloader");
    try { await api.downloads.erase({ id: item.id }); } catch { /* ignore */ }
    flashBadge("✓");
  } else {
    logActivity(item, false, "OpenDownloader didn't answer (" + (resp.error || "unknown") + ") - given back to the browser");
    // App unavailable: give the download back to the browser.
    bypass.set(url, Date.now() + 15000);
    try { await api.downloads.download({ url }); } catch { /* ignore */ }
    flashBadge("!");
  }
}

if (!IS_FIREFOX && api.downloads.onDeterminingFilename) {
  // Chrome: we get the final (Content-Disposition) file name here, before any Save-As prompt.
  api.downloads.onDeterminingFilename.addListener((item, suggest) => {
    ready.then(async () => {
      const [cap, why] = captureDecision(item);
      logActivity(item, cap, cap ? "capturing: " + why : why);
      if (cap) {
        try { await api.downloads.cancel(item.id); } catch { /* ignore */ }
        try { suggest(); } catch { /* ignore */ }
        await takeOver(item);
      } else {
        try { suggest(); } catch { /* ignore */ }
      }
    });
    return true; // we call suggest asynchronously
  });
} else {
  api.downloads.onCreated.addListener(async (item) => {
    await ready;
    if (item.state !== "in_progress") return;
    const [cap, why] = captureDecision(item);
    logActivity(item, cap, cap ? "capturing: " + why : why);
    if (cap) await takeOver(item);
  });
}

function flashBadge(text) {
  api.action.setBadgeText({ text });
  setTimeout(() => api.action.setBadgeText({ text: "" }), 2500);
}

// ---------------------------------------------------------------- media sniffing
const MANIFEST_CT = /mpegurl|dash\+xml/i;
const FILE_EXT = /^(mp4|webm|mkv|flv|mov|m4v|mp3|m4a|ogg|oga|ogv|opus|wav|f4v|3gp|aac|flac)$/;
const SEGMENT_HINT = /(\.ts|\.m4s|\.m4f|\.cmfv|\.cmfa)$|[?&](range|bytes|rn|sq)=|\/(seg|segment|frag|fragment|chunk)[-_/]?\d+|init\.mp4$/i;

const AUDIO_EXT = /^(mp3|m4a|ogg|oga|opus|wav|aac|flac|weba|wma|mka)$/;
const MIME_EXT = { mpeg: "mp3", mp3: "mp3", mp4: "m4a", "x-m4a": "m4a", aac: "aac", ogg: "ogg", opus: "opus",
                   wav: "wav", "x-wav": "wav", wave: "wav", flac: "flac", "x-flac": "flac", webm: "webm" };

function dispositionName(cd) {
  const m = /filename\*=(?:UTF-8'')?([^;]+)|filename="?([^";]+)"?/i.exec(cd || "");
  if (!m) return "";
  try { return decodeURIComponent((m[1] || m[2] || "").trim()); } catch { return (m[1] || m[2] || "").trim(); }
}

// type: the webRequest resource type ("media" = loaded by an <audio>/<video> element)
function classify(url, ct, type, cd) {
  const path = url.split("?")[0].toLowerCase();
  if (path.endsWith(".m3u8") || /mpegurl/i.test(ct)) return "hls";
  if (path.endsWith(".mpd") || /dash\+xml/i.test(ct)) return "dash";
  if (/video\/mp2t/i.test(ct) || SEGMENT_HINT.test(path) || SEGMENT_HINT.test(url)) return null;
  if (/^(video|audio)\//i.test(ct) || FILE_EXT.test(extOf(path))) return "file";
  const dn = dispositionName(cd);
  if (dn && FILE_EXT.test(extOf(dn.toLowerCase()))) return "file";
  // Players that serve audio as application/octet-stream from addresses without an extension
  // (stream.php?id=..., /api/track/123): whatever a media element loads is media.
  if (type === "media" && !/^(text|image|font)\/|json|xml|javascript/i.test(ct)) return "file";
  return null;
}

function isAudio(url, ct, name) {
  if (/^audio\//i.test(ct)) return true;
  if (/^video\//i.test(ct)) return false;
  return AUDIO_EXT.test(extOf((name || url.split("?")[0]).toLowerCase()));
}

function knownExt(entry) {
  for (const n of [entry.name, entry.url.split("?")[0]]) {
    const e = extOf((n || "").toLowerCase());
    if (e && FILE_EXT.test(e)) return e;
  }
  return MIME_EXT[(entry.mime || "").split("/")[1] || ""] || "";
}
function extFor(entry) {
  return knownExt(entry) || (entry.audio ? "mp3" : "mp4");
}

async function persistMedia() {
  if (!api.storage.session) return;
  const obj = {};
  for (const [k, v] of tabMedia) obj[k] = v;
  try { await api.storage.session.set({ tabMedia: obj }); } catch { /* quota */ }
}
async function restoreMedia() {
  if (!api.storage.session) return;
  try {
    const { tabMedia: saved, activity: act } = await api.storage.session.get(["tabMedia", "activity"]);
    if (Array.isArray(act)) activity = act;
    for (const [k, v] of Object.entries(saved || {})) tabMedia.set(Number(k), v);
  } catch { /* ignore */ }
}

function updateBadge(tabId) {
  const n = (tabMedia.get(tabId) || []).filter((m) => !m.protected).length;
  try {
    api.action.setBadgeText({ tabId, text: n ? String(n) : "" });
    api.action.setBadgeBackgroundColor({ tabId, color: "#d64545" });
  } catch { /* tab gone */ }
}

function notifyTab(tabId) {
  const list = tabMedia.get(tabId) || [];
  const audio = list.filter((m) => m.audio && !m.protected).length;
  api.tabs.sendMessage(tabId, { type: "mediaUpdated", count: list.length, audio }).catch?.(() => {});
}

function parseAttrs(s) {
  const out = {};
  for (const m of s.matchAll(/([A-Z0-9-]+)=("[^"]*"|[^,]*)/g)) out[m[1]] = m[2].replace(/^"|"$/g, "");
  return out;
}

async function inspectHls(entry, tabId, headers) {
  const resp = await fetch(entry.url, { credentials: "include", headers });
  const text = await resp.text();
  if (!text.startsWith("#EXTM3U")) { entry.invalid = true; return; }
  if (/#EXT-X-(SESSION-)?KEY:[^\n]*METHOD=SAMPLE-AES|KEYFORMAT="(?!identity)/i.test(text)) entry.protected = true;
  if (text.includes("#EXT-X-STREAM-INF")) {
    const lines = text.split(/\r?\n/);
    const variants = [];
    const kids = childPlaylists.get(tabId) || new Set();
    for (let i = 0; i < lines.length; i++) {
      if (!lines[i].startsWith("#EXT-X-STREAM-INF:")) continue;
      const a = parseAttrs(lines[i].slice(18));
      let j = i + 1;
      while (j < lines.length && (!lines[j] || lines[j].startsWith("#"))) j++;
      if (j >= lines.length) break;
      const vurl = new URL(lines[j].trim(), entry.url).href;
      kids.add(vurl.split("?")[0]);
      const height = a.RESOLUTION ? Number(a.RESOLUTION.split("x")[1]) : 0;
      variants.push({ url: vurl, height, bandwidth: Number(a.BANDWIDTH || 0) });
    }
    for (const m of text.matchAll(/#EXT-X-MEDIA:([^\n]*)/g)) {
      const a = parseAttrs(m[1]);
      if (a.URI) kids.add(new URL(a.URI, entry.url).href.split("?")[0]);
    }
    childPlaylists.set(tabId, kids);
    const k2m = kidToMaster.get(tabId) || new Map();
    for (const k of kids) k2m.set(k, entry.url);
    kidToMaster.set(tabId, k2m);
    const seen = new Set();
    entry.variants = variants.filter((v) => (seen.has(v.height + ":" + v.bandwidth) ? false : seen.add(v.height + ":" + v.bandwidth)))
      .sort((a, b) => b.height - a.height || b.bandwidth - a.bandwidth);
    entry.master = true;
    const top = entry.variants[0];
    const codecs = [...text.matchAll(/CODECS="([^"]*)"/g)].map((m) => m[1]).join(",");
    entry.audio = !top?.height && !!codecs && !/avc|hvc|hev|vp0?[89]|av01/i.test(codecs);
    entry.label = top?.height ? `HLS stream · up to ${top.height}p` : entry.audio ? "HLS audio" : "HLS stream";
    // drop variant playlists that were detected before the master
    const list = tabMedia.get(tabId) || [];
    tabMedia.set(tabId, list.filter((m) => m === entry || !kids.has(m.url.split("?")[0])));
  } else {
    let dur = 0;
    for (const m of text.matchAll(/#EXTINF:([\d.]+)/g)) dur += parseFloat(m[1]);
    entry.live = !text.includes("#EXT-X-ENDLIST");
    entry.duration = dur;
    const segs = [...text.matchAll(/^(?!#)(\S+)$/gm)].map((m) => m[1].split("?")[0].toLowerCase());
    const idx = segIndex.get(tabId) || new Map();
    for (const m of text.matchAll(/^(?!#)(\S+)$/gm)) {
      try { idx.set(new URL(m[1], entry.url).href.split("?")[0], entry.url); } catch { /* bad line */ }
    }
    while (idx.size > 20000) idx.delete(idx.keys().next().value);
    segIndex.set(tabId, idx);
    entry.audio = segs.length > 0 && segs.every((u) => /\.(aac|mp3|m4a|opus|ogg)$/.test(u));
    entry.label = entry.live ? `Live ${entry.audio ? "audio" : "stream"} (HLS)`
      : `HLS ${entry.audio ? "audio" : "stream"} · ${fmtDur(dur)}`;
  }
}

async function inspectDash(entry, headers) {
  const resp = await fetch(entry.url, { credentials: "include", headers });
  const text = await resp.text();
  if (/<ContentProtection/i.test(text)) entry.protected = true;
  const heights = [...text.matchAll(/<Representation[^>]*\sheight="(\d+)"/g)].map((m) => Number(m[1]));
  const top = heights.length ? Math.max(...heights) : 0;
  entry.live = /type="dynamic"/.test(text);
  entry.label = (entry.live ? "Live DASH stream" : "DASH stream") + (top ? ` · up to ${top}p` : "");
}

function fmtDur(s) {
  s = Math.round(s || 0);
  const h = Math.floor(s / 3600), m = Math.floor((s % 3600) / 60), sec = s % 60;
  return h ? `${h}:${String(m).padStart(2, "0")}:${String(sec).padStart(2, "0")}` : `${m}:${String(sec).padStart(2, "0")}`;
}
function fmtSize(n) {
  if (!(n > 0)) return "";
  const u = ["B", "KB", "MB", "GB"]; let i = 0;
  while (n >= 1024 && i < u.length - 1) { n /= 1024; i++; }
  return `${n.toFixed(i ? 1 : 0)} ${u[i]}`;
}

async function onMediaResponse(d) {
  if (d.tabId < 0 || d.statusCode >= 400) return;
  const ct = header(d.responseHeaders, "content-type");
  const cd = header(d.responseHeaders, "content-disposition");
  const kind = classify(d.url, ct, d.type, cd);
  if (!kind) return;
  await ready;
  let size = Number(header(d.responseHeaders, "content-length")) || 0;
  const cr = header(d.responseHeaders, "content-range");
  if (cr && cr.includes("/")) size = Number(cr.split("/").pop()) || size;
  const name = dispositionName(cd) || fileFromUrl(d.url);
  const audio = kind === "file" ? isAudio(d.url, ct, name) : false;
  const video = kind === "file" && !audio && (/^video\//i.test(ct) || FILE_EXT.test(extOf(name.toLowerCase())));
  // short audio (voice notes, previews) is still worth listing; tiny video files are usually ads/previews
  const minKB = audio ? Math.min(settings.mediaMinKB, 40) : settings.mediaMinKB;
  if (kind === "file" && size > 0 && size < minKB * 1024) return;
  const key = d.url.split("?")[0];
  if (kind === "hls" && (childPlaylists.get(d.tabId) || new Set()).has(key)) return;
  const list = tabMedia.get(d.tabId) || [];
  if (list.some((m) => m.url === d.url || (kind === "file" && m.url.split("?")[0] === key))) return;
  // pieces of an HLS/DASH stream already listed (e.g. .aac / .mp3 segments next to the playlist)
  if (kind === "file" && list.some((m) => m.kind !== "file" && key.startsWith(m.url.split("?")[0].replace(/[^/]*$/, ""))
                                   && size > 0 && size < 8 * 1024 * 1024)) return;
  const sent = reqHeaders.get(d.requestId) || {};
  reqHeaders.delete(d.requestId);
  const entry = {
    url: d.url, kind, mime: ct.split(";")[0], size, time: Date.now(), headers: sent, audio, video,
    label: "", name,
  };
  entry.label = kind !== "file" ? kind.toUpperCase() + " stream"
    : audio || video ? `${audio ? "Audio" : "Video"} · ${extFor(entry).toUpperCase()} ${fmtSize(size)}`.trim()
    : `Media file ${fmtSize(size)}`.trim();
  list.push(entry);
  tabMedia.set(d.tabId, list.slice(-40));
  try {
    if (kind === "hls") await inspectHls(entry, d.tabId, sent);
    if (kind === "dash") await inspectDash(entry, sent);
  } catch { /* keep the entry, the app will inspect it again */ }
  if (entry.invalid) tabMedia.set(d.tabId, (tabMedia.get(d.tabId) || []).filter((m) => m !== entry));
  updateBadge(d.tabId);
  notifyTab(d.tabId);
  persistMedia();
}

const MEDIA_FILTER = { urls: ["<all_urls>"], types: ["media", "xmlhttprequest", "other", "object"] };
api.webRequest.onHeadersReceived.addListener((d) => { onMediaResponse(d); }, MEDIA_FILTER, ["responseHeaders"]);

// Remember request headers some CDNs require (Origin, Referer, Authorization…)
const KEEP_HEADERS = new Set(["origin", "referer", "authorization", "x-requested-with"]);
function onSendHeaders(d) {
  if (d.tabId < 0) return;
  const path = d.url.split("?")[0].toLowerCase();
  if (!/\.(m3u8|mpd)$/.test(path) && d.type !== "media") return;
  const h = {};
  for (const x of d.requestHeaders || []) if (KEEP_HEADERS.has(x.name.toLowerCase())) h[x.name] = x.value;
  reqHeaders.set(d.requestId, h);
  if (reqHeaders.size > 500) reqHeaders.delete(reqHeaders.keys().next().value);
}
try {
  api.webRequest.onSendHeaders.addListener(onSendHeaders, MEDIA_FILTER, ["requestHeaders", "extraHeaders"]);
} catch {
  api.webRequest.onSendHeaders.addListener(onSendHeaders, MEDIA_FILTER, ["requestHeaders"]);
}

api.tabs.onUpdated.addListener((tabId, info) => {
  if (info.status === "loading" && info.url) {
    tabMedia.delete(tabId);
    childPlaylists.delete(tabId);
    segIndex.delete(tabId);
    kidToMaster.delete(tabId);
    updateBadge(tabId);
    persistMedia();
  }
});
api.tabs.onRemoved.addListener((tabId) => {
  tabMedia.delete(tabId);
  childPlaylists.delete(tabId);
  segIndex.delete(tabId);
  kidToMaster.delete(tabId);
  persistMedia();
});

// ---------------------------------------------------------------- which stream feeds which player
// content.js sends the URLs pagehook.js saw a player request (manifests, segments, files). Map them
// to the streams listed for the tab: exact URL, a known segment of a playlist, a variant of a
// master playlist, or failing that the playlist in the same folder with the longest common name.
function resolveEvidence(tabId, urls) {
  const list = tabMedia.get(tabId) || [];
  const byKey = new Map(list.map((m) => [m.url.split("?")[0], m]));
  const segs = segIndex.get(tabId) || new Map();
  const k2m = kidToMaster.get(tabId) || new Map();
  const score = new Map();
  const files = [];
  const add = (m, w) => { if (m) score.set(m.url, (score.get(m.url) || 0) + w); };
  urls.forEach((u, i) => {
    const key = (u || "").split("?")[0];
    if (!key) return;
    const w = 1 + i / Math.max(1, urls.length);          // later evidence counts a bit more
    const direct = list.find((m) => m.url === u) || byKey.get(key);
    if (direct) return add(direct, 3 * w);
    if (k2m.has(key)) return add(byKey.get(k2m.get(key).split("?")[0]), 3 * w);
    if (segs.has(key)) {
      const pl = segs.get(key).split("?")[0];
      return add(byKey.get(k2m.get(pl)?.split("?")[0]) || byKey.get(pl), w);
    }
    const dir = key.replace(/[^/]*$/, "");
    const name = key.slice(dir.length);
    let best = null, bestLen = 0;
    for (const m of list) {
      if (m.kind === "file") continue;
      const mk = m.url.split("?")[0];
      if (!mk.startsWith(dir)) continue;
      const mn = mk.slice(dir.length);
      let n = 0;
      while (n < mn.length && n < name.length && mn[n] === name[n]) n++;
      if (n > bestLen || (n === bestLen && best && m.time > best.time)) { best = m; bestLen = n; }
    }
    if (best && /^(video|audio)\b|\.(ts|m4s|m4a|m4v|mp4|aac|cmf[va]|webm)$/i.test(name) || best && bestLen >= 6) return add(best, 0.5 * w);
    if (FILE_EXT.test(extOf(key))) files.push(u);
  });
  const own = [...score.entries()].sort((a, b) => b[1] - a[1]).map(([u]) => u);
  return { own: own.slice(0, 4), ownFiles: [...new Set(files)].slice(-3) };
}

// The page changed its address without reloading: drop streams from before that aren't in use.
function pageChanged(tabId, since, keep) {
  const list = tabMedia.get(tabId) || [];
  const used = new Set(resolveEvidence(tabId, keep).own);
  const keepKeys = new Set(keep.map((u) => u.split("?")[0]));
  const left = list.filter((m) => m.time >= since - 1000 || used.has(m.url) || keepKeys.has(m.url.split("?")[0]));
  tabMedia.set(tabId, left);
  updateBadge(tabId);
  persistMedia();
  return { ok: true, count: left.length, audio: left.filter((m) => m.audio && !m.protected).length };
}

// ---------------------------------------------------------------- sending media
async function downloadMedia(entry, tabId, variant) {
  if (entry.protected) return { ok: false, error: "This stream is DRM-protected and can't be downloaded." };
  if (entry.kind === "file") {
    const req = await buildRequest({ url: entry.url, tabId, size: entry.size, mime: entry.mime, headers: entry.headers });
    if (!req.filename) {
      const base = entry.name && FILE_EXT.test(extOf(entry.name.toLowerCase())) ? entry.name.replace(/\.[^.]*$/, "") : req.title;
      req.filename = sanitize(base || (entry.audio ? "audio" : "video")) + "." + extFor(entry);
      if (!knownExt(entry)) req.guessedExt = true;   // the app checks the real format after downloading
    }
    return sendNative({ type: "download", data: req });
  }
  const req = await buildRequest({ url: entry.url, tabId, headers: entry.headers, kind: entry.kind,
                                   preferHeight: variant?.height });
  if (entry.audio) req.audio = true;
  return sendNative({ type: "media", data: req });
}
function sanitize(s) { return (s || "").replace(/[<>:"/\\|?*\x00-\x1f]+/g, " ").trim().slice(0, 120) || "video"; }

// ---------------------------------------------------------------- context menus
function createMenus() {
  api.contextMenus.removeAll(() => {
    api.contextMenus.create({ id: "od-link", title: "Download with OpenDownloader", contexts: ["link"] });
    api.contextMenus.create({ id: "od-media", title: "Download this media with OpenDownloader", contexts: ["video", "audio", "image"] });
    api.contextMenus.create({ id: "od-all", title: "Download all links with OpenDownloader", contexts: ["page", "selection"] });
    api.contextMenus.create({ id: "od-page", title: "Download video from this page", contexts: ["page"] });
  });
}
api.runtime.onInstalled.addListener(() => { createMenus(); pingApp(); });
api.runtime.onStartup?.addListener(() => { createMenus(); pingApp(); });

api.contextMenus.onClicked.addListener(async (info, tab) => {
  await ready;
  const tabId = tab?.id ?? -1;
  if (info.menuItemId === "od-link" && info.linkUrl) {
    sendNative({ type: "download", data: await buildRequest({ url: info.linkUrl, referrer: info.pageUrl, tabId }) });
  } else if (info.menuItemId === "od-media") {
    const src = info.srcUrl || "";
    if (/^https?:/i.test(src)) {
      sendNative({ type: "download", data: await buildRequest({ url: src, referrer: info.pageUrl, tabId }) });
    } else {
      // blob:/MSE video - use the best stream detected on this tab
      const list = (tabMedia.get(tabId) || []).filter((m) => !m.protected);
      const best = list.find((m) => m.master) || list.find((m) => m.kind !== "file") || list[list.length - 1];
      if (best && !/(^|\.)youtube(-nocookie)?\.com$/.test(hostOf(info.pageUrl))) downloadMedia(best, tabId);
      else sendNative({ type: "page", data: { url: info.frameUrl || info.pageUrl, referer: info.pageUrl,
                                              title: tab?.title || "" } });
    }
  } else if (info.menuItemId === "od-all") {
    let links = [];
    try {
      links = await api.tabs.sendMessage(tabId, { type: "collectLinks", selectionOnly: !!info.selectionText },
                                         info.frameId !== undefined ? { frameId: info.frameId } : undefined);
    } catch { /* content script not available */ }
    if (links && links.length) {
      sendNative({ type: "links", data: { urls: links, referer: info.pageUrl,
                   cookies: await cookieString(info.pageUrl), userAgent: navigator.userAgent } });
    }
  } else if (info.menuItemId === "od-page") {
    sendNative({ type: "page", data: { url: info.pageUrl, referer: info.pageUrl } });
  }
});

// ---------------------------------------------------------------- messages
api.runtime.onMessage.addListener((msg, sender, sendResponse) => {
  (async () => {
    await ready;
    const tabId = msg.tabId ?? sender.tab?.id ?? -1;
    switch (msg.type) {
      case "hello":
        return { ok: true, build: EXT_BUILD };
      case "getMedia": {
        const res = { media: tabMedia.get(tabId) || [], settings,
                      panelAllowed: settings.showVideoPanel && !siteMatches(settings.panelDisabledSites, sender.tab?.url || msg.pageUrl || "") };
        if (Array.isArray(msg.evidence) && msg.evidence.length) Object.assign(res, resolveEvidence(tabId, msg.evidence));
        return res;
      }
      case "pageChanged":
        return pageChanged(tabId, Number(msg.since) || Date.now(), Array.isArray(msg.keep) ? msg.keep : []);
      case "getActivity":
        return { activity };
      case "clearActivity":
        activity = [];
        try { await api.storage.session?.set({ activity }); } catch { /* ignore */ }
        return { ok: true };
      case "downloadMedia": {
        let entry = (tabMedia.get(tabId) || []).find((m) => m.url === msg.url) ||
          { url: msg.url, kind: msg.kind || "file", mime: "", size: 0, headers: {}, name: fileFromUrl(msg.url) };
      if (msg.audio && !entry.video) entry = { ...entry, audio: true };
        return downloadMedia(entry, tabId, msg.variant);
      }
      case "downloadPage": {
        let tab = null;
        try { tab = tabId >= 0 ? await api.tabs.get(tabId) : null; } catch { /* closed */ }
        return sendNative({ type: "page", data: { url: msg.url, referer: tab?.url || msg.url,
                            title: msg.title || tab?.title || "", preferHeight: msg.preferHeight,
                            preferAudio: !!msg.preferAudio } });
      }
      case "magnet": {
        if (!settings.enabled || settings.captureMagnets === false) return { ok: false, error: "off" };
        let tab = null;
        try { tab = tabId >= 0 ? await api.tabs.get(tabId) : null; } catch { /* closed */ }
        return sendNative({ type: "download", data: { url: msg.url, referer: tab?.url || "" } });
      }
      case "downloadUrl":
        return sendNative({ type: "download", data: await buildRequest({ url: msg.url, referrer: msg.referrer, tabId }) });
      case "bypassNext":
        bypass.set(msg.url, Date.now() + 10000);
        return { ok: true };
      case "status":
        return pingApp();
      case "showApp":
        return sendNative({ type: "show" });
      case "getSettings":
        return settings;
      case "saveSettings":
        settings = { ...settings, ...msg.settings };
        await api.storage.local.set({ settings });
        return settings;
      case "toggleSite": {
        const key = msg.list === "panel" ? "panelDisabledSites" : "excludedSites";
        const set = new Set(settings[key]);
        set.has(msg.host) ? set.delete(msg.host) : set.add(msg.host);
        settings = { ...settings, [key]: [...set] };
        await api.storage.local.set({ settings });
        return settings;
      }
      default:
        return { ok: false, error: "unknown" };
    }
  })().then(sendResponse, (e) => sendResponse({ ok: false, error: String(e) }));
  return true;
});
