"""Accident-detection portal: live camera monitor + alert system + accident report pages.

Run:   python server.py        then open http://127.0.0.1:5000
Pages: /             live monitor (start the camera, watch the feed)
       /reports      list of every accident detected
       /report/<id>  full details of one accident (shareable link)
Only cars, bikes, buses and trucks are detected.
"""
import json
import os
import threading
import time
from datetime import datetime

from flask import Flask, Response, abort, jsonify, request, send_from_directory
from werkzeug.utils import secure_filename

import db
import live
import pipeline

BASE = os.path.dirname(os.path.abspath(__file__))
OUT = os.path.join(BASE, "outputs")
os.makedirs(OUT, exist_ok=True)

try:  # optional .env support (python-dotenv is in Requirements.txt)
    from dotenv import load_dotenv
    load_dotenv(os.path.join(BASE, ".env"))
except ImportError:
    pass

app = Flask(__name__, static_folder=os.path.join(BASE, "frontend"), static_url_path="/static")

# ---- PostgreSQL (set DATABASE_URL; on Render it comes from the linked database) ----
try:
    if db.init():
        n = db.load_settings_into_env()
        print(f"PostgreSQL connected - tables ready, {n} saved setting(s) loaded", flush=True)
    else:
        print("DATABASE_URL not set - using local outputs/ folder (reports are lost on restart)", flush=True)
except Exception as e:      # never stop the portal because the DB is down
    print("PostgreSQL unavailable, falling back to local files:", e, flush=True)

MONITOR = live.LiveMonitor(OUT)
_detector = None
_load_lock = threading.Lock()
DETECT_LOCK = threading.Lock()
VEHICLE_CONF = float(os.getenv("VEHICLE_CONF", "0.35"))   # raise to reduce false detections


def _log(msg):
    print(f"[{datetime.now().strftime('%H:%M:%S')}] {msg}", flush=True)


def get_detector():
    """Lazy-load YOLO (model chosen by RAM like main.py, or YOLO_MODEL). Returns (detect, names, weights)."""
    global _detector
    with _load_lock:
        if _detector is None:
            t0 = time.time()
            _log("Loading AI model - importing PyTorch/ultralytics (first time can take 1-2 minutes on Windows)...")
            from ultralytics import YOLO
            import psutil
            _log(f"Imports done in {time.time() - t0:.0f}s")
            ram = psutil.virtual_memory().total
            weights = os.getenv("YOLO_MODEL") or ("yolo11n.pt" if ram < 4 * 2**30 else "yolo11s.pt" if ram < 8 * 2**30 else "yolo11m.pt")
            local = os.path.join(BASE, "models", weights)
            path = local if os.path.exists(local) else weights   # models/ folder works offline
            _log(f"Loading weights '{path}' (downloads on first use - watch for a progress bar below)...")
            model = YOLO(path)
            _log(f"Model ready in {time.time() - t0:.0f}s - detecting only: {', '.join(pipeline.VEHICLE_NAMES.values())}")

            def detect(frame):
                with DETECT_LOCK:
                    r = model(frame, classes=pipeline.VEHICLE_CLASSES, conf=VEHICLE_CONF, verbose=False)[0]
                return r.boxes.data.cpu().numpy().tolist() if r.boxes is not None else []

            _detector = (detect, pipeline.VEHICLE_NAMES, weights)
        return _detector


@app.route("/healthz")
def healthz():
    return jsonify(ok=True, database=db.enabled())


# ---------- pages ----------
def _page(name):
    return send_from_directory(app.static_folder, name)


@app.route("/")
def page_live():
    return _page("live.html")


@app.route("/reports")
def page_reports():
    return _page("reports.html")


@app.route("/report/<rid>")
def page_report(rid):
    return _page("report.html")


# ---------- live camera ----------
def parse_source(raw):
    raw = str(raw if raw is not None else "0").strip() or "0"
    if raw.isdigit() or raw.lower().startswith(("rtsp://", "http://", "https://")):
        return raw
    raise ValueError("Camera source must be a camera number (0, 1, ...) or an rtsp:// / http:// stream URL")


@app.route("/api/live/start", methods=["POST"])
def live_start():
    d = request.get_json(silent=True) or {}
    try:
        source = parse_source(d.get("source", "0"))
        lat = float(d.get("lat", pipeline.ACCIDENT_LOCATION[0]))
        lon = float(d.get("lon", pipeline.ACCIDENT_LOCATION[1]))
        if not (-90 <= lat <= 90 and -180 <= lon <= 180):
            raise ValueError("Latitude must be -90..90 and longitude -180..180")
    except (ValueError, TypeError) as e:
        return jsonify(error=str(e)), 400
    try:
        MONITOR.start(get_detector, source, (lat, lon))
    except RuntimeError as e:
        return jsonify(error=str(e)), 409
    return jsonify(ok=True), 202


@app.route("/api/live/stop", methods=["POST"])
def live_stop():
    MONITOR.stop()
    return jsonify(ok=True)


@app.route("/api/live/status")
def live_status():
    return jsonify(MONITOR.snapshot())


@app.route("/api/live/stream")
def live_stream():
    def gen():
        last = None
        while MONITOR.running():
            j = MONITOR.jpeg
            if j is not None and j is not last:
                last = j
                yield (b"--frame\r\nContent-Type: image/jpeg\r\nContent-Length: " + str(len(j)).encode() + b"\r\n\r\n" + j + b"\r\n")
            time.sleep(0.03)
    return Response(gen(), mimetype="multipart/x-mixed-replace; boundary=frame", headers={"Cache-Control": "no-store"})


# ---------- accident reports ----------
def _load_file(rid):
    p = os.path.join(OUT, secure_filename(rid), "result.json")
    if os.path.exists(p):
        with open(p) as f:
            return json.load(f)
    return None


def _load(rid):
    try:
        r = db.get_incident(secure_filename(rid))
        if r:
            return r
    except Exception:
        pass
    p = os.path.join(OUT, secure_filename(rid), "result.json")
    if os.path.exists(p):
        with open(p) as f:
            return json.load(f)
    return None


@app.route("/api/incident/<rid>")
def api_incident(rid):
    r = _load(rid)
    if r:
        return jsonify({"state": "ready", "result": r})
    snap = MONITOR.snapshot()
    for i in snap["incidents"]:       # confirmed, report still being written
        if i["id"] == rid:
            return jsonify({"state": "pending", "incident": i,
                            "messages": [m for m in snap["messages"] if m.get("incident") == rid]})
    abort(404)


@app.route("/api/incidents")
def api_incidents():
    rows = []
    try:
        rows = db.list_incidents() or []
    except Exception:
        rows = []
    seen = {r["id"] for r in rows}
    for d in os.listdir(OUT):
        r = None if d in seen else _load_file(d)
        if r:
            rows.append({"id": d, "time": r["finished_at"], "severity": r["severity"]["level"], "source": r["filename"],
                         "summary": r.get("summary", ""), "vehicles": [v["type"] for v in r.get("vehicles", [])],
                         "collisions": r["metrics"]["collision_count"], "location": r["location"],
                         "ready": True, "thumb": (r.get("evidence") or {}).get("confirmed")})
    ids = {r["id"] for r in rows}
    for i in MONITOR.snapshot()["incidents"]:        # include ones still being written
        if i["id"] not in ids:
            rows.append({"id": i["id"], "time": i["time"], "severity": i["severity"], "source": "Live camera",
                         "summary": i["summary"], "vehicles": [], "collisions": None, "location": None,
                         "ready": False, "thumb": None})
    rows.sort(key=lambda r: r["time"], reverse=True)
    return jsonify(rows)


# ---------- settings (email, weather key, webhook) ----------
ENV_PATH = os.path.join(BASE, ".env")
SETTING_KEYS = ("SENDER_EMAIL", "EMAIL_PASSWORD", "RECEIVER_EMAIL", "WEATHER_API_KEY", "ALERT_WEBHOOK")
SECRET_KEYS = ("EMAIL_PASSWORD", "WEATHER_API_KEY")


def _write_env(updates):
    """Update/append KEY="value" lines in .env, keeping every other line. Also applies them immediately."""
    lines = []
    if os.path.exists(ENV_PATH):
        with open(ENV_PATH, encoding="utf-8") as f:
            lines = f.read().splitlines()
    for k, v in updates.items():
        entry = f'{k}="{v}"'
        for i, ln in enumerate(lines):
            if ln.split("=", 1)[0].strip() == k:
                lines[i] = entry
                break
        else:
            lines.append(entry)
        os.environ[k] = v
    with open(ENV_PATH, "w", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")


@app.route("/settings")
def page_settings():
    return _page("settings.html")


@app.route("/api/settings", methods=["GET", "POST"])
def api_settings():
    if request.method == "POST":
        d = request.get_json(silent=True) or {}
        updates = {}
        for k in SETTING_KEYS:
            if k in d and isinstance(d[k], str):
                v = d[k].strip().replace('"', "").replace("\n", "")
                if k in SECRET_KEYS and not v:
                    continue                      # empty secret box = keep the saved one
                if k == "EMAIL_PASSWORD":
                    v = v.replace(" ", "")
                updates[k] = v
        if updates:
            _write_env(updates)
            try:
                db.save_settings(updates)          # survives Render restarts / redeploys
            except Exception as e:
                _log(f"Could not save settings to PostgreSQL: {e}")
        return jsonify(ok=True, saved=sorted(updates))
    wk = os.getenv("WEATHER_API_KEY") or ""
    return jsonify(sender=os.getenv("SENDER_EMAIL", ""), receiver=os.getenv("RECEIVER_EMAIL", ""),
                   password_set=bool(os.getenv("EMAIL_PASSWORD")), weather_key_set=bool(wk),
                   weather_key_hint=("••••" + wk[-4:]) if len(wk) > 4 else "", webhook=os.getenv("ALERT_WEBHOOK", ""))


@app.route("/api/settings/test/<what>", methods=["POST"])
def api_settings_test(what):
    d = request.get_json(silent=True) or {}
    try:
        lat = float(d.get("lat", pipeline.ACCIDENT_LOCATION[0]))
        lon = float(d.get("lon", pipeline.ACCIDENT_LOCATION[1]))
    except (TypeError, ValueError):
        return jsonify(error="Latitude/longitude must be numbers"), 400
    if what == "email":
        return jsonify(pipeline.send_email("Accident Alert Portal - test email",
                                           "This is a test email from your Accident Alert Portal.\nIf you can read this, accident alerts will reach you.", []))
    if what == "weather":
        return jsonify(pipeline.get_weather(lat, lon))
    if what == "services":
        return jsonify(pipeline.nearest_services(lat, lon))
    abort(404)


@app.route("/media/<rid>/<path:name>")
def media(rid, name):
    try:
        m = db.get_media(secure_filename(rid), name)
    except Exception:
        m = None
    if m:
        data, ctype = m
        return Response(data, mimetype=ctype, headers={"Cache-Control": "public, max-age=86400"})
    return send_from_directory(os.path.join(OUT, secure_filename(rid)), name)


if __name__ == "__main__":
    app.run(host=os.getenv("HOST", "127.0.0.1"), port=int(os.getenv("PORT", 5000)), debug=False, threaded=True)
