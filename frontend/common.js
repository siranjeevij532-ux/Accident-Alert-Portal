/* Shared by every portal page: header/nav, server status, ALERT SYSTEM (banner, toast, sound, desktop notification). */
const $ = (s) => document.querySelector(s);
const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
const fmt = (v, d = 2) => (v === null || v === undefined ? "n/a" : typeof v === "number" ? +v.toFixed(d) : v);
const store = {
  get(k) { try { return localStorage.getItem(k); } catch { return null; } },
  set(k, v) { try { localStorage.setItem(k, v); } catch { /* private mode */ } },
};
async function api(path, opts) {
  const r = await fetch(path, opts);
  if (!r.ok) throw new Error((await r.json().catch(() => ({}))).error || r.statusText);
  return r.json();
}

/* ---------- header ---------- */
const PAGE = location.pathname.startsWith("/report/") ? "report" : location.pathname.startsWith("/reports") ? "reports" : location.pathname.startsWith("/settings") ? "settings" : "live";
document.body.insertAdjacentHTML("afterbegin", `
<header class="top">
  <div class="brand"><span class="dot"></span> Accident Alert Portal</div>
  <nav class="nav">
    <a href="/" class="${PAGE === "live" ? "on" : ""}">Live monitor</a>
    <a href="/reports" class="${PAGE === "reports" || PAGE === "report" ? "on" : ""}">Accident reports</a>
    <a href="/settings" class="${PAGE === "settings" ? "on" : ""}">Settings</a>
  </nav>
  <div class="hright">
    <button id="sndbtn" class="ghost" title="Sound and desktop notifications for accident alerts"></button>
    <span class="srv" id="srv">checking…</span>
  </div>
</header>
<div id="alertbanner"></div>`);

/* ---------- sound + desktop notification ---------- */
let audioCtx = null;
const soundOn = () => store.get("sound") !== "off";
function paintSound() { $("#sndbtn").textContent = soundOn() ? "🔔 Alerts on" : "🔕 Alerts muted"; }
function unlockAudio() {
  try { audioCtx = audioCtx || new (window.AudioContext || window.webkitAudioContext)(); audioCtx.resume(); } catch { /* no audio */ }
}
function beep() {
  if (!soundOn()) return;
  unlockAudio();
  if (!audioCtx) return;
  [0, 0.35, 0.7].forEach((t) => {
    const o = audioCtx.createOscillator(), g = audioCtx.createGain();
    o.type = "square"; o.frequency.value = 880;
    g.gain.setValueAtTime(0.0001, audioCtx.currentTime + t);
    g.gain.exponentialRampToValueAtTime(0.25, audioCtx.currentTime + t + 0.02);
    g.gain.exponentialRampToValueAtTime(0.0001, audioCtx.currentTime + t + 0.28);
    o.connect(g); g.connect(audioCtx.destination);
    o.start(audioCtx.currentTime + t); o.stop(audioCtx.currentTime + t + 0.3);
  });
}
$("#sndbtn").addEventListener("click", () => {
  store.set("sound", soundOn() ? "off" : "on");
  paintSound();
  if (soundOn()) { unlockAudio(); beep(); if ("Notification" in window && Notification.permission === "default") Notification.requestPermission(); }
});
paintSound();
document.addEventListener("click", unlockAudio, { once: true });

/* ---------- toasts ---------- */
function toast(m) {
  let box = $("#toasts");
  if (!box) { box = document.createElement("div"); box.id = "toasts"; document.body.appendChild(box); }
  const t = document.createElement("div");
  t.className = `toast ${m.level}`;
  const link = m.incident ? `<a href="/report/${esc(m.incident)}">Open accident details →</a>` : "";
  t.innerHTML = `<b>${esc(m.title)}</b><span>${esc(m.text)}</span>${link}<small>${esc(m.time)}</small>`;
  t.addEventListener("click", (e) => { if (e.target.tagName !== "A") t.remove(); });
  box.appendChild(t);
  setTimeout(() => t.remove(), m.level === "alert" ? 25000 : 9000);
}
const feed = (msgs) => (msgs || []).length
  ? `<ul class="feed">${msgs.map((m) => `<li class="${m.level}"><time>${esc(m.time)}</time><div><b>${esc(m.title)}</b><span>${esc(m.text)}</span>${
      m.incident ? ` <a href="/report/${esc(m.incident)}">details →</a>` : ""}</div></li>`).join("")}</ul>`
  : '<p class="muted">No messages yet.</p>';

/* ---------- accident banner (shown on EVERY page until dismissed) ---------- */
function dismissed() { try { return JSON.parse(sessionStorage.getItem("dismissed") || "[]"); } catch { return []; } }
function dismiss(id) { try { sessionStorage.setItem("dismissed", JSON.stringify([...dismissed(), id])); } catch { /* ignore */ } renderBanner(Alerts.snap); }
function renderBanner(snap) {
  const el = $("#alertbanner");
  if (!snap) return;
  const live = snap.incidents.filter((i) => !dismissed().includes(i.id)).slice(-1)[0];
  const age = live ? (snap.now - live.ts) / 60 : 99;
  if (!live || age > 30 || (PAGE === "report" && location.pathname.endsWith(live.id))) { el.innerHTML = ""; return; }
  el.innerHTML = `<div class="gbanner ${esc(live.severity)}" role="alert">
    <div><b>🚨 ACCIDENT DETECTED</b><span>${esc(live.summary)}</span></div>
    <div class="gact"><a class="btn primary" href="/report/${esc(live.id)}">${live.ready ? "View accident details" : "View details (report preparing…)"}</a>
    <button class="ghost light" data-dismiss="${esc(live.id)}">Dismiss</button></div></div>`;
  el.querySelector("[data-dismiss]").onclick = (e) => dismiss(e.target.dataset.dismiss);
}

/* ---------- status polling (one loop per page) ---------- */
const Alerts = { snap: null, seen: null, inc: null, listeners: [] };
function onNewAccident(i) {
  beep();
  if ("Notification" in window && Notification.permission === "granted") {
    try { const n = new Notification("🚨 Accident detected", { body: i.summary }); n.onclick = () => { focus(); location.href = `/report/${i.id}`; }; } catch { /* ignore */ }
  }
}
async function pollStatus() {
  try {
    const snap = await api("/api/live/status");
    $("#srv").className = "srv ok"; $("#srv").textContent = "server online";
    if (Alerts.seen === null) {             // first poll of this page: don't replay old messages
      Alerts.seen = snap.messages.length;
      Alerts.inc = new Set(snap.incidents.map((i) => i.id));
    } else {
      if (snap.messages.length < Alerts.seen) Alerts.seen = 0;     // new monitoring session
      snap.messages.slice(Alerts.seen).filter((m) => m.level !== "info").forEach(toast);
      Alerts.seen = snap.messages.length;
      snap.incidents.forEach((i) => { if (!Alerts.inc.has(i.id)) { Alerts.inc.add(i.id); onNewAccident(i); } });
    }
    Alerts.snap = snap;
    renderBanner(snap);
    Alerts.listeners.forEach((f) => f(snap));
  } catch {
    $("#srv").className = "srv bad"; $("#srv").textContent = "server offline - run python server.py";
  }
  setTimeout(pollStatus, 1000);
}
pollStatus();

/* ---------- helpers for report views ---------- */
function chart(tl, accidentFrame) {
  if (!tl || !tl.length) return "";
  const W = 760, H = 180, P = { l: 34, r: 8, t: 8, b: 22 };
  const maxC = Math.max(1, ...tl.map((p) => p.collisions)), maxO = Math.max(1, ...tl.map((p) => p.objects));
  const n = tl.length, x = (i) => P.l + (i / Math.max(1, n - 1)) * (W - P.l - P.r);
  const yC = (v) => H - P.b - (v / maxC) * (H - P.t - P.b), yO = (v) => H - P.b - (v / maxO) * (H - P.t - P.b);
  const bw = Math.max(1, (W - P.l - P.r) / n);
  const bars = tl.map((p, i) => p.collisions ? `<rect x="${x(i) - bw / 2}" y="${yC(p.collisions)}" width="${bw}" height="${H - P.b - yC(p.collisions)}" fill="var(--high)" opacity=".75"/>` : "").join("");
  const line = tl.map((p, i) => `${i ? "L" : "M"}${x(i).toFixed(1)},${yO(p.objects).toFixed(1)}`).join("");
  const ai = accidentFrame != null ? tl.findIndex((p) => p.f === accidentFrame) : -1;
  return `<svg class="chart" viewBox="0 0 ${W} ${H}" role="img" aria-label="Vehicles and collisions per frame">
    <line x1="${P.l}" x2="${W - P.r}" y1="${H - P.b}" y2="${H - P.b}" stroke="var(--line)"/>
    <path d="${line}" fill="none" stroke="var(--accent)" stroke-width="1.6"/>${bars}
    ${ai >= 0 ? `<line x1="${x(ai)}" x2="${x(ai)}" y1="${P.t}" y2="${H - P.b}" stroke="var(--high)" stroke-dasharray="4 3"/><text x="${x(ai) - 4}" y="${P.t + 10}" text-anchor="end">accident</text>` : ""}
    <text x="${P.l - 4}" y="${P.t + 10}" text-anchor="end">${maxO}</text><text x="${P.l - 4}" y="${H - P.b}" text-anchor="end">0</text>
    <text x="${P.l}" y="${H - 6}">frame ${tl[0].f}</text><text x="${W - P.r}" y="${H - 6}" text-anchor="end">frame ${tl[n - 1].f}</text></svg>
    <div class="legend"><span><i style="background:var(--accent)"></i>vehicles detected (peak ${maxO})</span><span><i style="background:var(--high)"></i>colliding pairs (peak ${maxC})</span></div>`;
}
