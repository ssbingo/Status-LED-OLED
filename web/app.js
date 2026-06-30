"use strict";

const REFRESH_MS = 2000;
const C = 2 * Math.PI * 52;   // Umfang der Gauge-Kreise (r = 52)
const WARN_STATES = ["backup_failed", "network_down", "smart_warn", "fan_warn", "diskspace_low"];

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
  $("status-dot").style.setProperty("--led", s.status.color);
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
    render(s);
    const ageOk = (Date.now() / 1000 - s.ts) < (REFRESH_MS / 1000) * 3;
    setLive(ageOk);
  } catch (e) {
    setLive(false);
  }
}

// ---- Tabs -------------------------------------------------------------------
function selectTab(name) {
  document.querySelectorAll(".tab").forEach(t => t.classList.toggle("active", t.dataset.tab === name));
  document.querySelectorAll(".tabpanel").forEach(p => p.classList.toggle("active", p.id === "tab-" + name));
}
document.querySelectorAll(".tab").forEach(t =>
  t.addEventListener("click", () => { location.hash = t.dataset.tab; }));
window.addEventListener("hashchange", () => selectTab(location.hash.slice(1) || "overview"));
selectTab(location.hash.slice(1) || "overview");

poll();
setInterval(poll, REFRESH_MS);
