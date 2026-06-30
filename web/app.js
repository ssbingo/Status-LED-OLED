"use strict";

const REFRESH_MS = 2000;
const C = 2 * Math.PI * 52;   // Umfang der Gauge-Kreise (r = 52)
const WARN_STATES = ["backup_failed", "network_down", "smart_warn", "fan_warn", "diskspace_low"];
let lastState = null;
let controlEnabled = false;
let reconnecting = false;
let prevTs = null, staleCount = 0;   // Liveness ueber "Daten laufen weiter" (unabhaengig von der Uhrzeit)

const $ = (id) => document.getElementById(id);

// ---- Hilfsfunktionen --------------------------------------------------------
function fmtRate(bps) {
  const u = ["B/s", "K/s", "M/s", "G/s"];
  let i = 0, v = bps || 0;
  while (v >= 1024 && i < u.length - 1) { v /= 1024; i++; }
  return (i === 0 ? v.toFixed(0) : v.toFixed(1)) + " " + u[i];
}
function fmtUptime(s) {
  s = Math.floor(s || 0);
  const d = Math.floor(s / 86400), h = Math.floor((s % 86400) / 3600), m = Math.floor((s % 3600) / 60);
  if (d) return `${d}d ${h}h`;
  if (h) return `${h}h ${m}m`;
  return `${m}m`;
}
// Farbe nach Fuellstand (gut -> kritisch)
function fillColor(pct) {
  if (pct >= 90) return getCss("--red");
  if (pct >= 75) return getCss("--amber");
  return getCss("--green");
}
function getCss(name) {
  return getComputedStyle(document.documentElement).getPropertyValue(name).trim();
}
// Lesbare Textfarbe (schwarz/weiss) zur Hintergrundfarbe
function idealText(hex) {
  const c = (hex || "").replace("#", "");
  if (c.length < 6) return "#06121f";
  const r = parseInt(c.substr(0, 2), 16), g = parseInt(c.substr(2, 2), 16), b = parseInt(c.substr(4, 2), 16);
  return (0.299 * r + 0.587 * g + 0.114 * b) > 140 ? "#06121f" : "#ffffff";
}

function setGauge(arcId, numId, pct, label, color) {
  pct = Math.max(0, Math.min(100, pct || 0));
  const arc = $(arcId);
  arc.style.strokeDashoffset = C * (1 - pct / 100);
  arc.style.stroke = color;
  $(numId).textContent = label;
}

function setSparkline(id, arr, maxOverride) {
  const el = $(id);
  if (!arr || arr.length < 2) { el.setAttribute("points", ""); return; }
  const vb = el.closest("svg").viewBox.baseVal;
  const w = vb.width, h = vb.height;
  const max = maxOverride != null ? maxOverride : Math.max(...arr, 1);
  const min = 0;
  const n = arr.length;
  const pts = arr.map((v, i) => {
    const x = (i / (n - 1)) * w;
    const y = h - ((v - min) / (max - min || 1)) * (h - 2) - 1;
    return `${x.toFixed(1)},${y.toFixed(1)}`;
  });
  el.setAttribute("points", pts.join(" "));
}

// ---- Aktualisierung der Oberflaeche ----------------------------------------
function setLive(online) {
  const el = $("live");
  el.classList.toggle("online", online);
  el.classList.toggle("offline", !online);
  $("live-text").textContent = online ? "live" : "offline";
}

function render(s) {
  lastState = s;
  // Kopf
  $("host").textContent = s.host.name || "–";
  $("ip").textContent = s.host.ip || "–";
  $("version").textContent = "v" + s.version;
  $("uptime").textContent = fmtUptime(s.host.uptime_s);

  // LED-Symbol (Kopf, auf allen Tabs) + Status-Badge spiegeln die LED-Farbe/Muster
  const pulse = s.status.name === "backup_running";
  const blink = WARN_STATES.includes(s.status.name);
  const led = $("led");
  led.style.setProperty("--led", s.status.color);
  led.classList.toggle("pulse", pulse);
  led.classList.toggle("blink", blink);

  const badge = $("status-badge");
  badge.style.background = s.status.color;
  badge.style.color = idealText(s.status.color);
  badge.style.boxShadow = `0 0 16px -2px ${s.status.color}`;
  $("status-text").textContent = s.status.text;
  badge.classList.toggle("pulse", pulse);
  badge.classList.toggle("blink", blink);
  $("backup-state").textContent = ({ ok: "OK", running: "läuft…", failed: "Fehler" })[s.backup.state] || s.backup.state;

  // CPU
  const tMax = (s.cpu.threshold_c || 80) * 1.15;
  const cpuColor = s.cpu.overtemp ? getCss("--red")
    : (s.cpu.temp_c >= s.cpu.threshold_c ? getCss("--amber") : getCss("--green"));
  setGauge("cpu-arc", "cpu-num", (s.cpu.temp_c / tMax) * 100, Math.round(s.cpu.temp_c), cpuColor);
  $("cpu-load").textContent = `${s.cpu.load1.toFixed(2)} / ${s.cpu.cores}`;
  $("cpu-ymax").textContent = Math.round(tMax) + "°";

  // RAM
  setGauge("ram-arc", "ram-num", s.ram.percent, Math.round(s.ram.percent), fillColor(s.ram.percent));
  $("ram-mb").textContent = `${s.ram.used_mb} / ${s.ram.total_mb} MB`;

  // Speicher
  if (s.disk.enabled) {
    setGauge("disk-arc", "disk-num", s.disk.used_percent, Math.round(s.disk.used_percent), fillColor(s.disk.used_percent));
    $("disk-free").textContent = s.disk.free_percent.toFixed(0) + " %";
  } else {
    setGauge("disk-arc", "disk-num", 0, "–", getCss("--muted"));
    $("disk-free").textContent = "—";
  }
  $("disk-temp").textContent = (s.disk.temp_c != null) ? s.disk.temp_c + " °C" : "—";

  // Lüfter
  const fan = $("fan-svg"), rot = $("fan-rot");
  if (s.fan.rpm != null) {
    $("fan-val").textContent = s.fan.rpm + " rpm";
    const spinning = s.fan.rpm > 0;
    fan.classList.toggle("idle", !spinning);
    if (spinning) rot.style.animationDuration = Math.max(0.25, 2000 / s.fan.rpm) + "s";
  } else if (s.fan.level != null) {
    $("fan-val").textContent = `Stufe ${s.fan.level}/${s.fan.max_level}`;
    const spinning = s.fan.level > 0;
    fan.classList.toggle("idle", !spinning);
    if (spinning) rot.style.animationDuration = Math.max(0.4, 2.2 - s.fan.level * 0.4) + "s";
  } else {
    $("fan-val").textContent = "n/a";
    fan.classList.add("idle");
  }

  // Netzwerk
  $("net-rx").textContent = fmtRate(s.net.rx_bps);
  $("net-tx").textContent = fmtRate(s.net.tx_bps);
  $("net-iface").textContent = s.net.iface || "–";
  $("net-mac").textContent = s.net.mac || "–";

  // Verlaufsgrafiken
  const hist = s.history || {};
  const netMax = Math.max(1, ...(hist.net_rx || []), ...(hist.net_tx || []));
  setSparkline("net-spark-rx", hist.net_rx, netMax);
  setSparkline("net-spark-tx", hist.net_tx, netMax);
  setSparkline("cpu-spark", hist.cpu_temp, tMax);
  setSparkline("ram-spark", hist.ram_pct, 100);
}

// ---- Polling ----------------------------------------------------------------
async function poll() {
  try {
    const r = await fetch("api/state", { cache: "no-store" });
    if (!r.ok) throw new Error("HTTP " + r.status);
    const s = await r.json();
    if (s.error) throw new Error(s.error);
    try { render(s); } catch (e) { console.error("render", e); }   // ein Fehler darf das Polling nicht stoppen
    // online, solange die Statusdatei weiterlaeuft (ts aendert sich); 3 Polls
    // (~6 s) ohne Aenderung -> Hauptdienst steht -> offline. Keine Uhrzeit-Annahme.
    if (prevTs !== null && s.ts === prevTs) staleCount++; else staleCount = 0;
    prevTs = s.ts;
    setLive(staleCount < 3);
  } catch (e) {
    setLive(false);
  }
}

// ---- Backup-Tab -------------------------------------------------------------
function fmtAgo(epoch) {
  if (!epoch) return "—";
  const d = epoch - Date.now() / 1000;            // > 0 = Zukunft
  const a = Math.abs(d);
  const s = a < 90 ? `${Math.round(a)} s`
    : a < 5400 ? `${Math.round(a / 60)} min`
      : a < 172800 ? `${Math.round(a / 3600)} h`
        : `${Math.round(a / 86400)} d`;
  return d >= 0 ? `in ${s}` : `vor ${s}`;
}

async function loadBackup() {
  let b;
  try { b = await (await fetch("api/backup", { cache: "no-store" })).json(); }
  catch (e) { return; }
  const none = $("bk-none"), content = $("bk-content");
  if (!b.configured) { none.hidden = false; content.hidden = true; return; }
  none.hidden = true; content.hidden = false;
  const LABEL = { ok: "OK", running: "läuft…", failed: "Fehler" };
  const COLOR = { ok: "#30d158", running: "#32d6d6", failed: "#ff2d95" };
  const st = b.state || "ok";
  const badge = $("bk-badge");
  badge.textContent = "Backup: " + (LABEL[st] || st);
  badge.style.background = COLOR[st] || "#888";
  badge.style.color = idealText(COLOR[st] || "#888");
  badge.classList.toggle("pulse", st === "running");
  badge.classList.toggle("blink", st === "failed");
  $("bk-schedule").textContent = b.schedule || "—";
  $("bk-next").textContent = fmtAgo(b.next_run);
  $("bk-last").textContent = b.last_start || "—";
  $("bk-result").textContent = b.last_result ? ({ success: "erfolgreich" }[b.last_result] || b.last_result) : "—";
  $("bk-log").textContent = b.log || "(kein Log-Zugriff – auf dem Pi 'sudo status-led web-setup' erneut ausführen, um Journal-Rechte zu setzen)";
}

// ---- Steuer-Aktionen (Update / Neustart) ------------------------------------
async function doAction(action) {
  const r = await fetch("api/action", {
    method: "POST",
    headers: { "Content-Type": "application/json", "X-Status-LED-Action": "1" },
    body: JSON.stringify({ action })
  });
  const j = await r.json().catch(() => ({}));
  if (!r.ok || !j.ok) throw new Error(j.error || ("HTTP " + r.status));
  return true;
}

// Wartet, bis der Server wieder antwortet (nach Web-Neustart / Update) -> Reload
function waitReconnect(msgEl) {
  if (reconnecting) return;
  reconnecting = true;
  let tries = 0;
  const iv = setInterval(async () => {
    tries++;
    try {
      const r = await fetch("api/state", { cache: "no-store" });
      if (r.ok) { clearInterval(iv); location.reload(); return; }
    } catch (e) { /* noch offline */ }
    if (tries > 40) {
      clearInterval(iv); reconnecting = false;
      if (msgEl) msgEl.textContent = "Zeitüberschreitung – bitte die Seite manuell neu laden.";
    }
  }, 3000);
}

const SVC_ACTION = {
  "status-led.service": "restart:status-led",
  "status-led-web.service": "restart:web",
  "status-led-backup.timer": "restart:backup",
};

async function onRestart(svc, action, btn) {
  if (!confirm(`${svc.name} wirklich neu starten?`)) return;
  btn.disabled = true; btn.textContent = "…";
  // Neustart des Web-Dienstes kappt die eigene Verbindung -> als Erfolg behandeln
  if (action === "restart:web") { doAction(action).catch(() => {}); waitReconnect(); return; }
  try {
    await doAction(action);
    btn.textContent = "OK";
    setTimeout(loadSystem, 2500);
    setTimeout(() => { btn.disabled = false; btn.textContent = "Neustart"; }, 3000);
  } catch (e) {
    btn.textContent = "Fehler"; btn.disabled = false;
    alert("Neustart fehlgeschlagen: " + e.message);
    setTimeout(() => btn.textContent = "Neustart", 2000);
  }
}

// ---- System-Tab -------------------------------------------------------------
async function loadSystem() {
  let d;
  try { d = await (await fetch("api/system", { cache: "no-store" })).json(); }
  catch (e) { return; }
  controlEnabled = !!d.control;
  const el = $("sys-services");
  el.textContent = "";
  (d.services || []).forEach(s => {
    const cls = s.active === "active" ? "on" : (s.active === "not-installed" ? "off" : "warn");
    const row = document.createElement("div");
    row.className = "svc";
    const dot = document.createElement("span"); dot.className = "svc-dot " + cls;
    const name = document.createElement("span"); name.className = "svc-name"; name.textContent = s.name;
    const st = document.createElement("span"); st.className = "svc-state";
    st.textContent = s.active + (s.sub ? " · " + s.sub : "");
    row.append(dot, name, st);
    if (controlEnabled && SVC_ACTION[s.unit] && s.active !== "not-installed") {
      const b = document.createElement("button");
      b.className = "btn small"; b.textContent = "Neustart";
      b.addEventListener("click", () => onRestart(s, SVC_ACTION[s.unit], b));
      row.append(b);
    }
    el.appendChild(row);
  });
}

// ---- Wartungs-Tab -----------------------------------------------------------
async function loadMaint() {
  let d;
  try { d = await (await fetch("api/system", { cache: "no-store" })).json(); }
  catch (e) { d = {}; }
  const on = !!d.control;
  controlEnabled = on;
  $("maint-disabled").hidden = on;
  $("maint-actions").hidden = !on;
  $("maint-version").textContent = lastState ? ("v" + lastState.version) : "–";
}

async function onUpdate() {
  if (!confirm("Update jetzt ausführen? Der Dienst startet dabei neu.")) return;
  const btn = $("btn-update"), msg = $("maint-msg");
  btn.disabled = true;
  msg.textContent = "Update gestartet… die Seite verbindet sich neu, sobald es fertig ist.";
  doAction("update").catch(() => {});   // Verbindung kann durch den Neustart abbrechen
  waitReconnect(msg);
}

// ---- Tabs -------------------------------------------------------------------
let activeTab = "overview";
function selectTab(name) {
  activeTab = name;
  document.querySelectorAll(".tab").forEach(t => t.classList.toggle("active", t.dataset.tab === name));
  document.querySelectorAll(".tabpanel").forEach(p => p.classList.toggle("active", p.id === "tab-" + name));
  if (name === "backup") loadBackup();
  else if (name === "system") loadSystem();
  else if (name === "maint") loadMaint();
}
document.querySelectorAll(".tab").forEach(t =>
  t.addEventListener("click", () => { location.hash = t.dataset.tab; }));
window.addEventListener("hashchange", () => selectTab(location.hash.slice(1) || "overview"));
$("btn-update").addEventListener("click", onUpdate);
selectTab(location.hash.slice(1) || "overview");

poll();
setInterval(poll, REFRESH_MS);
setInterval(() => { if (activeTab === "backup") loadBackup(); else if (activeTab === "system") loadSystem(); }, 15000);
