/* Accident report page: /report/<id> */
const RID = decodeURIComponent(location.pathname.split("/").pop());
const root = $("#rep");
let timer = null;

async function load() {
  let j;
  try { j = await api(`/api/incident/${encodeURIComponent(RID)}`); }
  catch { root.innerHTML = `<div class="empty"><h1>Report not found</h1><p>No accident with id <code>${esc(RID)}</code>. <a href="/reports">See all reports</a></p></div>`; return; }
  if (j.state === "ready") { render(j.result); return; }
  root.innerHTML = `
    <div class="crumbs"><a href="/reports">← All accident reports</a></div>
    <div class="banner ${esc(j.incident.severity)}"><div><h1>Accident detected</h1><p>${esc(j.incident.summary)}</p></div>
      <div class="sev"><b>${esc(j.incident.severity)}</b>provisional severity</div></div>
    <div class="card"><h2>Full report is being prepared…</h2>
      <p class="muted">Saving evidence frames and the clip, checking weather and the nearest hospital and police. This page updates by itself.</p>
      ${feed(j.messages)}</div>`;
  timer = setTimeout(load, 1500);
}

function svcRow(label, s) {
  return `<div class="svc"><div><small class="muted">${label}</small><b>${s ? esc(s.name) : "Not found within 40 km"}</b>${s ? `<small class="muted">${s.lat.toFixed(4)}, ${s.lon.toFixed(4)}</small>` : ""}</div>${s ? `<em>${s.distance_km} km</em>` : ""}</div>`;
}

function render(r) {
  document.title = `Accident ${r.finished_at} - Accident Alert Portal`;
  const m = r.metrics, sev = r.severity, loc = r.location, veh = r.vehicles || [];
  const bbox = [loc.lon - 0.01, loc.lat - 0.007, loc.lon + 0.01, loc.lat + 0.007].join("%2C");
  const frames = Object.entries(r.evidence || {});
  const names = { first: "First collision", peak: "Peak impact", confirmed: "Accident confirmed" };
  const alertMsg = (r.messages || []).find((x) => x.level === "alert");
  root.innerHTML = `
  <div class="crumbs noprint"><a href="/reports">← All accident reports</a> · <a href="/">Live monitor</a></div>

  <div class="banner ${esc(sev.level)}">
    <div><h1>Accident detected</h1><p>${esc(r.finished_at)} · ${esc(r.filename)}${r.model ? " · " + esc(r.model) : ""}</p></div>
    <div class="sev"><b>${esc(sev.level)}</b>${esc(sev.message)}</div>
  </div>

  <div class="card alertcard"><h2>Alert message</h2>
    <p class="alerttext"><b>🚨 ACCIDENT DETECTED</b> — ${esc(r.summary || "")}</p>
    <div class="row">
      <span class="tag">Portal alert: sent</span>
      <span class="tag ${r.email.sent ? "" : "warn"}">Email: ${r.email.sent ? "sent" : "not sent"} (${esc(r.email.detail)})</span>
    </div>
  </div>

  <div class="grid stats">
    <div class="stat"><small>Vehicles involved</small><b>${veh.length}</b><span>${[...new Set(veh.map((v) => v.type))].map(esc).join(", ") || "n/a"}</span></div>
    <div class="stat"><small>Collision frames</small><b>${m.collision_count}</b><span>overlapping pairs, summed over frames</span></div>
    <div class="stat"><small>Peak overlap (IoU)</small><b>${fmt(m.peak_iou, 2)}</b><span>threshold ${m.thresholds.iou}</span></div>
    <div class="stat"><small>Speed before impact</small><b>${fmt(m.impact_speed_ratio, 1)}</b><span>body-lengths/s (${fmt(m.max_speed, 0)} px/s)</span></div>
    <div class="stat"><small>Confirmed at</small><b>${m.accident_time_s != null ? m.accident_time_s + " s" : "—"}</b><span>of monitoring · frame ${m.accident_frame}</span></div>
  </div>

  <div class="grid two">
    <div class="card"><h2>Why this severity</h2>
      ${(sev.factors || []).length ? `<ul class="why">${sev.factors.map((f) => `<li>${esc(f)}</li>`).join("")}</ul>
        <p class="muted">Score ${sev.score}: 4+ = High, 2-3 = Medium, below 2 = Low. These thresholds are a starting point - tune them on your own footage.</p>`
        : `<p class="muted">${esc(sev.message)}</p>`}
    </div>
    <div class="card"><h2>Vehicles involved</h2>
      ${veh.length ? `<table class="tbl"><thead><tr><th>Vehicle</th><th>Type</th><th>Speed before impact</th></tr></thead><tbody>
        ${veh.map((v) => `<tr><td>#${v.id}</td><td>${esc(v.type)}</td><td>${fmt(v.impact_speed_ratio, 2)} body-lengths/s <span class="muted">(${fmt(v.impact_speed_px_s, 0)} px/s)</span></td></tr>`).join("")}</tbody></table>`
        : '<p class="muted">Not tracked.</p>'}
    </div>
  </div>

  ${(r.timeline || []).length ? `<div class="card full" style="margin-top:16px"><h2>Detection timeline</h2>${chart(r.timeline, m.accident_frame)}</div>` : ""}

  <div class="grid two" style="margin-top:16px">
    <div class="card"><h2>Incident</h2>
      <dl class="kv"><dt>Source</dt><dd>${esc(r.filename)}</dd>
        <dt>Detected at</dt><dd>${esc(r.finished_at)}</dd>
        <dt>Location</dt><dd>${loc.lat}, ${loc.lon} <a href="https://www.openstreetmap.org/?mlat=${loc.lat}&mlon=${loc.lon}#map=16/${loc.lat}/${loc.lon}" target="_blank" rel="noopener">open map</a></dd>
        <dt>First collision</dt><dd>${m.first_collision_frame != null ? "frame " + m.first_collision_frame : "n/a"}</dd></dl>
      <iframe class="map" loading="lazy" title="Incident location" src="https://www.openstreetmap.org/export/embed.html?bbox=${bbox}&layer=mapnik&marker=${loc.lat}%2C${loc.lon}"></iframe>
    </div>
    <div class="card"><h2>Weather at location</h2>
      <dl class="kv"><dt>Conditions</dt><dd>${esc(r.weather.description)}</dd>
        <dt>Temperature</dt><dd>${r.weather.temperature != null ? fmt(r.weather.temperature, 1) + " °C" : "n/a"}</dd>
        ${r.weather.humidity != null ? `<dt>Humidity</dt><dd>${r.weather.humidity}%</dd>` : ""}
        ${r.weather.wind != null ? `<dt>Wind</dt><dd>${r.weather.wind} m/s</dd>` : ""}</dl>
      ${r.weather.error ? `<p class="muted">Weather lookup: ${esc(r.weather.error)}</p>` : ""}
      <h2 style="margin-top:20px">Nearest emergency services</h2>
      ${svcRow("Hospital", r.services.hospital)}${svcRow("Police station", r.services.police)}
      ${r.services.error ? `<p class="muted">Lookup: ${esc(r.services.error)}</p>` : ""}
    </div>
    <div class="card"><h2>Vehicles seen during monitoring</h2>
      ${Object.entries(r.objects_detected || {}).sort((a, b) => b[1] - a[1]).map(([k, v]) => `<span class="tag">${esc(k)} · ${v}</span>`).join("") || '<p class="muted">None.</p>'}
      <h2 style="margin-top:20px">Event log</h2>
      ${(r.events || []).length ? `<ul class="log">${r.events.map((e) => `<li><time>${e.t}s · f${e.frame}</time><span>${esc(e.text)}</span></li>`).join("")}</ul>` : '<p class="muted">No events.</p>'}
    </div>
    <div class="card" style="grid-column:1/-1"><h2>Messages sent</h2>${feed(r.messages)}</div>
  </div>

  ${frames.length ? `<div class="card full" style="margin-top:16px"><h2>Evidence frames</h2><div class="frames">
    ${frames.map(([k, f]) => `<figure><img src="/media/${esc(RID)}/${esc(f)}" alt="${esc(names[k] || k)}"><figcaption>${esc(names[k] || k)}</figcaption></figure>`).join("")}</div></div>` : ""}

  ${r.clip ? `<div class="card full noprint"><h2>Accident clip</h2><video controls src="/media/${esc(RID)}/${esc(r.clip)}"></video>
    <p><a href="/media/${esc(RID)}/${esc(r.clip)}" download>Download clip</a></p></div>` : ""}

  <div class="card full"><h2>Full report</h2><pre>${esc(r.report_text)}</pre>
    <div class="row noprint"><button id="copy">Copy</button><button id="dl">Download .txt</button><button id="prt" class="primary">Print / Save as PDF</button>
      <a class="btn" href="/api/incident/${encodeURIComponent(RID)}" target="_blank" rel="noopener">Raw JSON</a></div></div>`;

  $("#copy").onclick = () => navigator.clipboard.writeText(r.report_text);
  $("#prt").onclick = () => window.print();
  $("#dl").onclick = () => {
    const a = document.createElement("a");
    a.href = URL.createObjectURL(new Blob([r.report_text], { type: "text/plain" }));
    a.download = `accident-report-${RID}.txt`;
    a.click();
  };
}
root.addEventListener("click", (e) => {
  const img = e.target.closest(".frames img");
  if (img) { $("#lightbox img").src = img.src; $("#lightbox").hidden = false; }
});
$("#lightbox").addEventListener("click", () => ($("#lightbox").hidden = true));
load();
