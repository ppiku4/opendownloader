"use strict";
const api = globalThis.browser ?? globalThis.chrome;
const $ = (id) => document.getElementById(id);
const send = (msg) => new Promise((res) => api.runtime.sendMessage(msg, (r) => { void api.runtime.lastError; res(r); }));
const lines = (s) => s.split(/[\s,]+/).map((x) => x.trim().toLowerCase().replace(/^\./, "")).filter(Boolean);

async function load() {
  const s = await send({ type: "getSettings" });
  for (const k of ["enabled", "syncWithApp", "captureIncognito", "showVideoPanel"]) $(k).checked = !!s[k];
  $("captureMagnets").checked = s.captureMagnets !== false;
  $("extensions").value = (s.extensions || []).join(" ");
  $("minSizeKB").value = s.minSizeKB || 0;
  $("mediaMinKB").value = s.mediaMinKB || 0;
  $("excludedSites").value = (s.excludedSites || []).join("\n");
  $("panelDisabledSites").value = (s.panelDisabledSites || []).join("\n");
  $("extensions").disabled = $("syncWithApp").checked;
  $("syncWithApp").onchange = () => { $("extensions").disabled = $("syncWithApp").checked; };
  test();
}

async function test() {
  $("statusText").textContent = "Checking connection…";
  const st = await send({ type: "status" });
  $("status").className = "dot " + (st && st.ok ? "ok" : "bad");
  $("statusText").textContent = st && st.ok ? `Connected to OpenDownloader ${st.version || ""}`
    : `Not connected: ${(st && st.error) || "unknown"}`;
  if (st && st.ok && $("syncWithApp").checked) {
    const s = await send({ type: "getSettings" });
    $("extensions").value = (s.extensions || []).join(" ");
  }
}

$("test").onclick = test;
$("save").onclick = async () => {
  await send({ type: "saveSettings", settings: {
    enabled: $("enabled").checked,
    syncWithApp: $("syncWithApp").checked,
    captureIncognito: $("captureIncognito").checked,
    showVideoPanel: $("showVideoPanel").checked,
    captureMagnets: $("captureMagnets").checked,
    extensions: lines($("extensions").value),
    minSizeKB: Number($("minSizeKB").value) || 0,
    mediaMinKB: Number($("mediaMinKB").value) || 0,
    excludedSites: lines($("excludedSites").value),
    panelDisabledSites: lines($("panelDisabledSites").value),
  } });
  $("saved").textContent = "Saved ✓";
  setTimeout(() => { $("saved").textContent = ""; }, 2000);
};
load();
