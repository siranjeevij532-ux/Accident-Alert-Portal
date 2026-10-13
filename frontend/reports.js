/* Accident reports list page */
let rows = [], filter = "ALL";
function paint() {
  const counts = { ALL: rows.length, HIGH: 0, MEDIUM: 0, LOW: 0 };
  rows.forEach((r) => { counts[r.severity] = (counts[r.severity] || 0) + 1; });
  $("#chips").innerHTML = ["ALL", "HIGH", "MEDIUM", "LOW"].map((k) =>
    `<button class="chip ${filter === k ? "on" : ""}" data-f="${k}">${k === "ALL" ? "All" : k[0] + k.slice(1).toLowerCase()} <b>${counts[k] || 0}</b></button>`).join("");
  const shown = rows.filter((r) => filter === "ALL" || r.severity === filter);
  $("#list").innerHTML = shown.length ? shown.map((r) => `
    <a class="rrow" href="/report/${esc(r.id)}">
      ${r.thumb ? `<img src="/media/${esc(r.id)}/${esc(r.thumb)}" alt="" loading="lazy">` : `<div class="nothumb">${r.ready ? "no image" : "preparing…"}</div>`}
      <div class="rmain"><div class="rtop"><b>Accident · ${esc(r.time)}</b><span class="pill ${esc(r.severity)}">${esc(r.severity)}</span></div>
        <div class="muted">${esc(r.summary || "")}</div>
        <div>${r.vehicles.map((v) => `<span class="tag">${esc(v)}</span>`).join("")}${r.collisions != null ? `<span class="tag">${r.collisions} collision frames</span>` : ""}
          ${r.ready ? "" : '<span class="tag warn">report being prepared</span>'}</div></div>
      <span class="go">Open →</span></a>`).join("")
    : `<div class="empty"><h1>${rows.length ? "No reports match this filter" : "No accidents detected yet"}</h1><p>${rows.length ? "" : 'Start the camera on the <a href="/">Live monitor</a>. Every confirmed accident appears here with its own details page.'}</p></div>`;
}
$("#chips").addEventListener("click", (e) => { const b = e.target.closest("[data-f]"); if (b) { filter = b.dataset.f; paint(); } });
async function load() { try { rows = await api("/api/incidents"); paint(); } catch { /* offline */ } }
load(); setInterval(load, 4000);
