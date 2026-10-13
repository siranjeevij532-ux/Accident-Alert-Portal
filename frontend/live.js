/* Live monitor page */
const STATUS_TEXT = { idle: "Idle", starting: "Starting…", "loading model": "Loading model…", "opening camera": "Opening camera…", "waiting for browser camera": "Waiting for browser camera…",
  monitoring: "Monitoring - all clear", collision: "Collision suspected", cooldown: "ACCIDENT - alert sent", stopped: "Stopped", error: "Error" };
const pillClass = (st) => ({ collision: "collision", cooldown: "cooldown", error: "error", monitoring: "" }[st] ?? "starting");
let streamOn = false;

// ---- browser camera: capture frames here, send them to the server for detection ----
let camStream = null, sending = false;
const SEND_W = 640, SEND_GAP_MS = 120;
async function startBrowserCamera() {
  if (sending) return;
  if (!navigator.mediaDevices || !navigator.mediaDevices.getUserMedia)
    throw new Error("This browser cannot access a camera here (the site must be opened over https).");
  camStream = await navigator.mediaDevices.getUserMedia({ video: { facingMode: "environment", width: { ideal: 1280 } }, audio: false });
  const v = $("#lvcam"); v.srcObject = camStream; await v.play();
  sending = true;
  const c = $("#lvcanvas"), ctx = c.getContext("2d");
  while (sending) {
    const t0 = performance.now();
    try {
      c.width = SEND_W; c.height = Math.round(SEND_W * v.videoHeight / v.videoWidth) || 360;
      ctx.drawImage(v, 0, 0, c.width, c.height);
      const blob = await new Promise((r) => c.toBlob(r, "image/jpeg", 0.7));
      const res = await fetch("/api/live/frame", { method: "POST", body: blob });
      if (res.status === 409) await new Promise((r) => setTimeout(r, 500));   // server not ready / model still loading
    } catch (e) { await new Promise((r) => setTimeout(r, 1000)); }
    await new Promise((r) => setTimeout(r, Math.max(0, SEND_GAP_MS - (performance.now() - t0))));
  }
}
function stopBrowserCamera() {
  sending = false;
  if (camStream) { camStream.getTracks().forEach((t) => t.stop()); camStream = null; }
}

function render(snap) {
  const { state: st, stats: s, messages, incidents } = snap;
  const run = st.running;
  if (!run && sending) stopBrowserCamera();
  if (run && st.source === "browser" && !sending && !camStream)   // page reloaded while monitoring
    startBrowserCamera().catch((e) => ($("#lmsg").textContent = e.message));
  $("#lstart").hidden = run; $("#lstop").hidden = !run;
  $("#livebadge").textContent = run ? "on" : "off";
  $("#livebadge").style.color = run ? "var(--low)" : "";
  ["#lsrc", "#llat", "#llon"].forEach((q) => ($(q).disabled = run));

  $("#lvpill").textContent = STATUS_TEXT[st.status] || st.status;
  $("#lvpill").className = "livepill " + pillClass(st.status);
  $("#lvsub").textContent = run ? `Source ${st.source} · ${st.model || "loading model"} · started ${st.started}` : (st.error || "Camera is off - press Start live camera.");
  $("#lvbanner").className = "banner " + (st.status === "cooldown" ? "HIGH" : st.status === "collision" ? "MEDIUM" : "NONE");
  $("#lvstatus").textContent = STATUS_TEXT[st.status] || st.status;
  $("#lvobj").textContent = s.objects ?? "-";
  $("#lvpro").textContent = s.prolonged != null ? `${s.prolonged} / ${s.prolonged_max}` : "-";
  $("#lvbar").style.width = s.prolonged != null ? (100 * s.prolonged / s.prolonged_max) + "%" : "0";
  $("#lvcol").textContent = s.collisions ?? "-";
  $("#lvfps").textContent = s.fps != null ? s.fps + " fps" : "-";
  $("#lvup").textContent = s.uptime != null ? s.uptime + " s" : "-";
  $("#lvinc").textContent = incidents.length;
  $("#lvfeed").innerHTML = feed(messages.slice().reverse());
  $("#recent").innerHTML = incidents.length
    ? incidents.slice().reverse().slice(0, 5).map((i) => `<a class="hrow" href="/report/${esc(i.id)}"><b>Accident ${esc(i.time.slice(11))}</b><span class="pill ${esc(i.severity)}">${esc(i.severity)}</span><small>${esc(i.summary)}</small></a>`).join("")
    : '<p class="muted">None yet.</p>';

  const ph = $("#lvph"), img = $("#lvimg");
  if (run && s.frame === undefined) {
    streamOn = false; img.hidden = true; ph.hidden = false;
    const secs = Math.max(0, Math.round(snap.now - st.started_ts));
    ph.innerHTML = `<div><b>${esc(STATUS_TEXT[st.status] || st.status)}</b> (${secs} s)` +
      (st.status === "loading model" ? `<br><small>First start imports PyTorch and may download the YOLO model - this can take 1-2 minutes. Watch the terminal where server.py runs.</small>` : "") + `</div>`;
  } else if (run) {
    if (!streamOn) { streamOn = true; img.src = "/api/live/stream?t=" + Date.now(); }
  } else {
    streamOn = false; img.hidden = true; ph.hidden = false; ph.textContent = st.error || "Camera is off";
  }
}
$("#lvimg").addEventListener("load", () => { $("#lvimg").hidden = false; $("#lvph").hidden = true; });
Alerts.listeners.push(render);

if (["127.0.0.1", "localhost"].includes(location.hostname)) $("#lsrc").value = "0";
for (const [id, k] of [["#llat", "lat"], ["#llon", "lon"]]) {
  const v = store.get(k); if (v) $(id).value = v;
  $(id).addEventListener("input", () => store.set(k, $(id).value));
}
$("#lstart").addEventListener("click", async () => {
  unlockAudio();
  if ("Notification" in window && Notification.permission === "default") Notification.requestPermission();
  $("#lmsg").textContent = "Starting…";
  try {
    await api("/api/live/start", { method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ source: $("#lsrc").value, lat: $("#llat").value, lon: $("#llon").value }) });
    $("#lmsg").textContent = "Started. The first start can take a minute while the AI model loads.";
    if ($("#lsrc").value.trim().toLowerCase() === "browser") {
      $("#lmsg").textContent = "Allow camera access when your browser asks. The AI model may take a minute to load.";
      await startBrowserCamera();
    }
  } catch (e) {
    if (e.name === "NotAllowedError") e = new Error("Camera permission was blocked - allow it in the browser's address bar and press Start again.");
    else if (e.name === "NotFoundError") e = new Error("No camera found on this device.");
    api("/api/live/stop", { method: "POST" }).catch(() => {}); stopBrowserCamera(); $("#lmsg").textContent = e.message; }
});
$("#lstop").addEventListener("click", async () => { stopBrowserCamera(); await api("/api/live/stop", { method: "POST" }); $("#lmsg").textContent = "Stopped."; });
$("#lgeo").addEventListener("click", () => {
  if (!navigator.geolocation) { $("#lmsg").textContent = "Location is not available in this browser."; return; }
  navigator.geolocation.getCurrentPosition((p) => {
    $("#llat").value = p.coords.latitude.toFixed(5); $("#llon").value = p.coords.longitude.toFixed(5);
    store.set("lat", $("#llat").value); store.set("lon", $("#llon").value);
  }, () => ($("#lmsg").textContent = "Could not read your location - type it in."));
});
