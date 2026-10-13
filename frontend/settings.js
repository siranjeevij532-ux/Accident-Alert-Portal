/* Settings page */
const post = (url, body) => api(url, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body || {}) });
const show = (id, ok, text) => { const e = $(id); e.className = "result " + (ok ? "ok" : "bad"); e.textContent = text; };
const loc = () => ({ lat: $("#s_lat").value, lon: $("#s_lon").value });

async function loadSettings() {
  try {
    const s = await api("/api/settings");
    $("#e_sender").value = s.sender; $("#e_to").value = s.receiver; $("#h_url").value = s.webhook;
    $("#e_pwset").textContent = s.password_set ? "(saved - leave empty to keep)" : "";
    $("#w_set").textContent = s.weather_key_set ? `(saved ${s.weather_key_hint} - leave empty to keep)` : "";
  } catch (e) { show("#e_res", false, "Could not load settings: " + e.message); }
}
async function save(resId, data, okText) {
  try { await post("/api/settings", data); show(resId, true, okText); loadSettings(); }
  catch (e) { show(resId, false, "Could not save: " + e.message); }
}
$("#e_save").onclick = () => save("#e_res", { SENDER_EMAIL: $("#e_sender").value, EMAIL_PASSWORD: $("#e_pw").value, RECEIVER_EMAIL: $("#e_to").value }, "Saved. Now press “Send test email”.").then(() => ($("#e_pw").value = ""));
$("#w_save").onclick = () => save("#w_res", { WEATHER_API_KEY: $("#w_key").value }, "Saved. Now press “Test weather”.").then(() => ($("#w_key").value = ""));
$("#h_save").onclick = () => save("#h_res", { ALERT_WEBHOOK: $("#h_url").value }, "Saved.");

$("#e_test").onclick = async () => {
  show("#e_res", true, "Sending…");
  try { const r = await post("/api/settings/test/email"); show("#e_res", r.sent, (r.sent ? "✔ " : "✖ ") + r.detail); }
  catch (e) { show("#e_res", false, e.message); }
};
$("#w_test").onclick = async () => {
  show("#w_res", true, "Checking…");
  try {
    const r = await post("/api/settings/test/weather", loc());
    r.error ? show("#w_res", false, "✖ " + r.error) : show("#w_res", true, `✔ ${r.description}, ${r.temperature} °C at ${loc().lat}, ${loc().lon}`);
  } catch (e) { show("#w_res", false, e.message); }
};
$("#s_test").onclick = async () => {
  show("#s_res", true, "Searching OpenStreetMap… (can take up to a minute if the map server is busy)");
  try {
    const r = await post("/api/settings/test/services", loc());
    if (r.error) return show("#s_res", false, "✖ " + r.error);
    const f = (n, x) => `${n}: ${x ? `${x.name} - ${x.distance_km} km away` : "none found within 40 km"}`;
    show("#s_res", !!(r.hospital || r.police), `${f("Hospital", r.hospital)}   |   ${f("Police station", r.police)}`);
  } catch (e) { show("#s_res", false, e.message); }
};
$("#s_geo").onclick = () => {
  if (!navigator.geolocation) return show("#s_res", false, "Location is not available in this browser.");
  navigator.geolocation.getCurrentPosition((p) => { $("#s_lat").value = p.coords.latitude.toFixed(5); $("#s_lon").value = p.coords.longitude.toFixed(5); store.set("lat", $("#s_lat").value); store.set("lon", $("#s_lon").value); },
    () => show("#s_res", false, "Could not read your location - type it in."));
};
for (const [id, k] of [["#s_lat", "lat"], ["#s_lon", "lon"]]) {
  const v = store.get(k); if (v) $(id).value = v;
  $(id).addEventListener("input", () => store.set(k, $(id).value));
}
loadSettings();
