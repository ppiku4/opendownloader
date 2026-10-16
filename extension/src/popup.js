"use strict";
const api = globalThis.browser ?? globalThis.chrome;
const $ = (id) => document.getElementById(id);
const send = (msg) => new Promise((res) => api.runtime.sendMessage(msg, (r) => { void api.runtime.lastError; res(r); }));

let tab = null;
let host = "";

async function init() {
  [tab] = await api.tabs.query({ active: true, currentWindow: true });
  try { host = new URL(tab.url).hostname; } catch { host = ""; }

  if (api.permissions?.contains) {
    const ok = await api.permissions.contains({ origins: ["<all_urls>"] });
    $("perm").classList.toggle("hidden", ok);
  }
  $("grant").onclick = async () => {
    const ok = await api.permissions.request({ origins: ["<all_urls>"] });
    $("perm").classList.toggle("hidden", ok);
  };

  const settings = await send({ type: "getSettings" });
  $("enabled").checked = !!settings.enabled;
  $("enabled").onchange = async (e) => { await send({ type: "saveSettings", settings: { enabled: e.target.checked } }); };
  renderSite(settings);
  $("site").onclick = async () => renderSite(await send({ type: "toggleSite", list: "capture", host }));

  $("options").onclick = () => api.runtime.openOptionsPage();
  $("openApp").onclick = () => send({ type: "showApp" });

  const st = await send({ type: "status" });
  const dot = $("status");
  dot.className = "dot " + (st && st.ok ? "ok" : "bad");
  dot.title = st && st.ok ? `Connected to OpenDownloader ${st.version || ""}` : (st && st.error) || "Not connected";
  if (!st || !st.ok) {
    const n = $("appdown");
    n.classList.remove("hidden");
    n.textContent = st && st.notInstalled
      ? "The OpenDownloader app isn't registered with this browser. Open OpenDownloader → Options → Browser → Register, then restart the browser."
      : "Can't reach the OpenDownloader app: " + ((st && st.error) || "unknown error");
  }
  renderMedia();
  renderActivity();
}

function ago(t) {
  const s = Math.max(0, Math.round((Date.now() - t) / 1000));
  return s < 60 ? `${s}s ago` : s < 3600 ? `${Math.round(s / 60)} min ago` : `${Math.round(s / 3600)} h ago`;
}

// Why each recent download was (or wasn't) sent to OpenDownloader.
async function renderActivity() {
  const r = await send({ type: "getActivity" });
  const list = (r && r.activity) || [];
  $("activityCount").textContent = list.length ? `(${list.length})` : "";
  $("clearActivity").onclick = async () => { await send({ type: "clearActivity" }); renderActivity(); };
  const box = $("activity");
  if (!list.length) return;
  box.textContent = "";
  for (const a of list) {
    const row = document.createElement("div");
    row.className = "act " + (a.captured ? "yes" : "no");
    const name = document.createElement("div");
    name.className = "name";
    name.textContent = (a.captured ? "\u2713 " : "\u2717 ") + (a.file || a.url || "(unknown)");
    name.title = a.url || "";
    const why = document.createElement("div");
    why.className = "muted why";
    why.textContent = `${a.reason} \u00b7 ${a.site || ""} \u00b7 ${ago(a.time)}`;
    row.append(name, why);
    box.appendChild(row);
  }
}

function renderSite(settings) {
  if (!host) { $("site").classList.add("hidden"); return; }
  const off = (settings.excludedSites || []).includes(host);
  $("site").textContent = off ? `Enable capture on ${host}` : `Disable capture on ${host}`;
}

function youtubeVideoUrl(url) {
  try {
    const u = new URL(url);
    if (!/(^|\.)youtube\.com$/.test(u.hostname)) return null;
    if (u.pathname === "/watch" && u.searchParams.get("v")) return `https://www.youtube.com/watch?v=${u.searchParams.get("v")}`;
    const m = u.pathname.match(/^\/shorts\/([\w-]{6,})/);
    return m ? `https://www.youtube.com/shorts/${m[1]}` : null;
  } catch { return null; }
}

function youtubeRow(url) {
  const row = document.createElement("div");
  row.className = "media";
  const info = document.createElement("div");
  info.className = "info";
  const name = document.createElement("div");
  name.className = "name";
  name.textContent = (tab.title || "YouTube video").replace(/\s+-\s+YouTube$/, "");
  const sub = document.createElement("div");
  sub.className = "muted name";
  sub.textContent = "YouTube video · choose quality in OpenDownloader";
  info.append(name, sub);
  const b = document.createElement("button");
  b.className = "primary";
  b.textContent = "Download";
  b.onclick = async () => {
    b.disabled = true;
    const res = await send({ type: "downloadPage", tabId: tab.id, url, title: name.textContent });
    b.textContent = res && res.ok ? "Sent ✓" : "Failed";
    setTimeout(() => window.close(), res && res.ok ? 700 : 3000);
  };
  row.append(info, b);
  return row;
}

async function renderMedia() {
  const box = $("media");
  const yt = youtubeVideoUrl(tab.url);
  if (yt) {
    box.textContent = "";
    box.appendChild(youtubeRow(yt));
    return;
  }
  const r = await send({ type: "getMedia", tabId: tab.id, pageUrl: tab.url });
  const list = (r && r.media) || [];
  if (!list.length) return;
  box.textContent = "";
  for (const m of list.slice().reverse()) {
    const row = document.createElement("div");
    row.className = "media";
    const info = document.createElement("div");
    info.className = "info";
    const name = document.createElement("div");
    name.className = "name";
    name.textContent = m.label;
    name.title = m.url;
    const sub = document.createElement("div");
    sub.className = "muted name";
    sub.textContent = m.name || new URL(m.url).hostname;
    info.append(name, sub);
    row.appendChild(info);
    if (m.protected) {
      const l = document.createElement("span");
      l.className = "lock";
      l.textContent = "DRM - not supported";
      row.appendChild(l);
    } else {
      let sel = null;
      if (m.variants && m.variants.length) {
        sel = document.createElement("select");
        m.variants.forEach((v, i) => {
          const o = document.createElement("option");
          o.value = String(i);
          o.textContent = v.height ? `${v.height}p` : `${Math.round(v.bandwidth / 1000)} kbps`;
          sel.appendChild(o);
        });
        row.appendChild(sel);
      }
      const b = document.createElement("button");
      b.className = "primary";
      b.textContent = "Download";
      b.onclick = async () => {
        b.disabled = true;
        const variant = sel ? m.variants[Number(sel.value)] : undefined;
        const res = await send({ type: "downloadMedia", tabId: tab.id, url: m.url, variant });
        b.textContent = res && res.ok ? "Sent ✓" : "Failed";
        if (!(res && res.ok)) b.title = (res && res.error) || "";
        setTimeout(() => window.close(), res && res.ok ? 700 : 3000);
      };
      row.appendChild(b);
    }
    box.appendChild(row);
  }
}

init();
