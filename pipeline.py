"""Accident-detection pipeline, refactored from main.py so a web UI can consume it.

Same detection logic as main.py (YOLO boxes -> pairwise IoU -> prolonged-collision
counter -> accident), but:
  * no cv2.imshow window; progress is reported through a callback
  * results are returned as a JSON-serialisable dict instead of only an email
  * weather / nearest-services lookups run once, at the moment of the accident
  * secrets come from environment variables, never from source
"""
import os
import random
import re
import shutil
import smtplib
import subprocess
import time
from datetime import datetime
from email.mime.application import MIMEApplication
from email.mime.image import MIMEImage
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from math import atan2, cos, radians, sin, sqrt

import cv2
import requests

# ---- thresholds (identical to main.py) ------------------------------------
SPEED_THRESHOLD = 5.0
PROLONGED_COLLISION_FRAMES = 40
IOU_COLLISION = 0.2
ACCIDENT_LOCATION = (19.070, 72.877)
WEATHER_URL = "https://api.openweathermap.org/data/2.5/weather"


# ---- helpers ----------------------------------------------------------------
def haversine(lat1, lon1, lat2, lon2):
    R = 6371.0
    lat1, lon1, lat2, lon2 = map(radians, [lat1, lon1, lat2, lon2])
    a = sin((lat2 - lat1) / 2) ** 2 + cos(lat1) * cos(lat2) * sin((lon2 - lon1) / 2) ** 2
    return R * 2 * atan2(sqrt(a), sqrt(1 - a))


def calculate_iou(b1, b2):
    x1, y1 = max(b1[0], b2[0]), max(b1[1], b2[1])
    x2, y2 = min(b1[2], b2[2]), min(b1[3], b2[3])
    inter = max(0, x2 - x1) * max(0, y2 - y1)
    a1 = (b1[2] - b1[0]) * (b1[3] - b1[1])
    # NOTE: main.py used (box2[3] - box1[1]) here, which skews the IoU. Fixed.
    a2 = (b2[2] - b2[0]) * (b2[3] - b2[1])
    union = a1 + a2 - inter
    return inter / union if union > 0 else 0


def calculate_speed(b1, b2, dt=1):
    cx1, cy1 = (b1[0] + b1[2]) / 2, (b1[1] + b1[3]) / 2
    cx2, cy2 = (b2[0] + b2[2]) / 2, (b2[1] + b2[3]) / 2
    return sqrt((cx2 - cx1) ** 2 + (cy2 - cy1) ** 2) / dt


def estimate_severity(collision_count, max_speed, weather):
    w = (weather or "").lower()
    if collision_count > 5 or max_speed > SPEED_THRESHOLD * 1.5 or "rain" in w or "fog" in w:
        return "HIGH", "Immediate emergency response required"
    if collision_count > 3 or max_speed > SPEED_THRESHOLD:
        return "MEDIUM", "Urgent medical attention recommended"
    return "LOW", "Standard medical check recommended"


# ---- vehicle-only detection ---------------------------------------------------
# COCO class ids -> label shown in the portal. Only these are detected.
VEHICLE_NAMES = {2: "car", 3: "bike", 5: "bus", 7: "truck"}
VEHICLE_CLASSES = list(VEHICLE_NAMES)


class SimpleTracker:
    """Greedy IoU / centre-distance tracker (no extra dependencies).
    Gives every vehicle an id that survives between frames, so speed is measured per vehicle
    (main.py compared boxes by list position, which mixes up vehicles and gives huge fake speeds)."""

    def __init__(self, max_missed=8):
        self.tracks = {}
        self.next_id = 1
        self.max_missed = max_missed

    @staticmethod
    def _c(b):
        return (b[0] + b[2]) / 2, (b[1] + b[3]) / 2

    @staticmethod
    def _diag(b):
        return max(1.0, sqrt((b[2] - b[0]) ** 2 + (b[3] - b[1]) ** 2))

    def update(self, dets, now):
        """dets: [[x1,y1,x2,y2,conf,cls],...] -> list of track dicts aligned with dets."""
        cand = []
        for tid, t in self.tracks.items():
            for di, d in enumerate(dets):
                iou = calculate_iou(t["box"], d[:4])
                (tx, ty), (dx, dy) = self._c(t["box"]), self._c(d[:4])
                dist = sqrt((tx - dx) ** 2 + (ty - dy) ** 2) / self._diag(t["box"])
                if iou > 0.1 or dist < 0.6:
                    cand.append((iou - 0.3 * dist, tid, di))
        cand.sort(reverse=True)
        used_t, used_d, assigned = set(), set(), {}
        for _, tid, di in cand:
            if tid in used_t or di in used_d:
                continue
            used_t.add(tid)
            used_d.add(di)
            assigned[di] = tid
        out = []
        for di, d in enumerate(dets):
            box, cls = list(d[:4]), int(d[5])
            if di in assigned:
                t = self.tracks[assigned[di]]
                dt = now - t["last_t"]
                if dt > 0:
                    (ox, oy), (nx, ny) = self._c(t["box"]), self._c(box)
                    inst = sqrt((nx - ox) ** 2 + (ny - oy) ** 2) / dt
                    t["speed"] = inst if t["age"] < 2 else 0.6 * t["speed"] + 0.4 * inst
                t.update(box=box, last_t=now, missed=0, age=t["age"] + 1)
            else:
                t = {"id": self.next_id, "box": box, "last_t": now, "missed": 0, "age": 1, "speed": 0.0,
                     "votes": {}, "hist": []}
                self.tracks[t["id"]] = t
                self.next_id += 1
            t["votes"][cls] = t["votes"].get(cls, 0) + 1
            t["cls"] = max(t["votes"], key=t["votes"].get)       # majority class (car/truck flicker)
            t["hist"].append((t["speed"], self._diag(box)))
            del t["hist"][:-30]                                   # ~ last second
            out.append(t)
        new_ids = {t["id"] for t in out}
        for tid in list(self.tracks):
            if tid not in used_t and tid not in new_ids:
                self.tracks[tid]["missed"] += 1
                if self.tracks[tid]["missed"] > self.max_missed:
                    del self.tracks[tid]
        return out


def assess_severity(n_vehicles, peak_iou, impact_ratio, weather, has_bike):
    """Points-based severity with the reasons listed. Thresholds are heuristics - tune them on real footage."""
    score, factors = 0, []
    if n_vehicles >= 3:
        score += 2; factors.append(f"{n_vehicles} vehicles involved (+2)")
    else:
        score += 1; factors.append(f"{n_vehicles} vehicles involved (+1)")
    if peak_iou >= 0.5:
        score += 2; factors.append(f"heavy overlap, peak IoU {peak_iou:.2f} (+2)")
    elif peak_iou >= 0.3:
        score += 1; factors.append(f"clear overlap, peak IoU {peak_iou:.2f} (+1)")
    if impact_ratio >= 1.5:
        score += 2; factors.append(f"high speed before impact, {impact_ratio:.1f} body-lengths/s (+2)")
    elif impact_ratio >= 0.6:
        score += 1; factors.append(f"moderate speed before impact, {impact_ratio:.1f} body-lengths/s (+1)")
    if has_bike:
        score += 1; factors.append("a bike (rider unprotected) is involved (+1)")
    w = (weather or "").lower()
    if any(k in w for k in ("rain", "fog", "mist", "snow", "storm", "drizzle", "thunder")):
        score += 1; factors.append(f"bad weather: {weather} (+1)")
    if score >= 4:
        level, msg = "HIGH", "Immediate emergency response required"
    elif score >= 2:
        level, msg = "MEDIUM", "Urgent medical attention recommended"
    else:
        level, msg = "LOW", "Standard medical check recommended"
    return {"level": level, "message": msg, "score": score, "factors": factors}


def simulated_vehicle_telemetry():
    """main.py generates random telemetry (no real sensor). Kept, but flagged as simulated."""
    speed = random.randint(0, 120)
    accel = random.uniform(0, 5)
    braking = random.choice([True, False])
    events = []
    if speed > 100:
        events.append("Overspeeding")
    if accel > 3:
        events.append("Heavy acceleration")
    if braking:
        events.append("Hard braking")
    return events


def get_weather(lat, lon):
    key = (os.getenv("WEATHER_API_KEY") or "").strip()
    if not key:
        return {"description": "Unknown", "temperature": None, "error": "No weather key - add it on the Settings page"}
    try:
        r = requests.get(WEATHER_URL, params={"lat": lat, "lon": lon, "appid": key, "units": "metric"}, timeout=10)
        if r.status_code == 200:
            d = r.json()
            return {"description": d["weather"][0]["description"], "temperature": d["main"]["temp"],
                    "humidity": d["main"].get("humidity"), "wind": d.get("wind", {}).get("speed"), "error": None}
        if r.status_code == 401:
            err = "OpenWeather rejected the key (wrong key, or a new key that is not active yet - can take up to 2 hours)"
        else:
            err = f"OpenWeather returned HTTP {r.status_code}"
        return {"description": "Unknown", "temperature": None, "error": err}
    except Exception as e:  # network / DNS
        return {"description": "Unknown", "temperature": None, "error": "No internet or OpenWeather unreachable: " + str(e)[:80]}


OVERPASS_URLS = ["https://overpass-api.de/api/interpreter",
                 "https://overpass.kumi.systems/api/interpreter",
                 "https://maps.mail.ru/osm/tools/overpass/api/interpreter"]


def nearest_services(lat, lon):
    """Nearest police station and hospital from OpenStreetMap (free, no key). Tries 5, 15 then 40 km.
    Looks at points AND building outlines (many hospitals are mapped as buildings), and falls back
    to mirror servers because the main Overpass server is often busy."""
    out = {"police": None, "hospital": None, "error": None}
    last_err = None
    for radius in (5000, 15000, 40000):
        query = (f'[out:json][timeout:25];(nwr["amenity"="police"](around:{radius},{lat},{lon});'
                 f'nwr["amenity"="hospital"](around:{radius},{lat},{lon}););out center;')
        data = None
        for url in OVERPASS_URLS:
            try:
                r = requests.post(url, data={"data": query}, headers={"User-Agent": "AccidentAlertPortal/1.0"}, timeout=30)
                if r.status_code == 200:
                    data = r.json()
                    break
                last_err = f"{url.split('/')[2]} answered HTTP {r.status_code} (busy)"
            except Exception as e:
                last_err = f"{url.split('/')[2]}: {str(e)[:70]}"
        if data is None:
            out["error"] = (last_err or "map server unreachable") + " - check your internet and try again"
            return out
        for el in data.get("elements", []):
            tags = el.get("tags") or {}
            kind = tags.get("amenity")
            if kind not in ("police", "hospital"):
                continue
            la = el.get("lat", (el.get("center") or {}).get("lat"))
            lo = el.get("lon", (el.get("center") or {}).get("lon"))
            if la is None or lo is None:
                continue
            d = round(haversine(lat, lon, la, lo), 3)
            cur = out[kind]
            if cur is None or d < cur["distance_km"]:
                out[kind] = {"name": tags.get("name") or tags.get("name:en") or "Unnamed", "lat": la, "lon": lo,
                             "distance_km": d, "phone": tags.get("phone") or tags.get("contact:phone")}
        if out["police"] and out["hospital"]:
            break
    out["error"] = None
    return out


def email_config():
    sender = (os.getenv("SENDER_EMAIL") or "").strip()
    password = (os.getenv("EMAIL_PASSWORD") or "").replace(" ", "")      # Gmail shows app passwords with spaces
    to = [x for x in re.split(r"[,;\s]+", os.getenv("RECEIVER_EMAIL") or "") if x]
    return sender, password, to


def send_email(subject, report, images, clip=None):
    """Gmail SMTP. images / clip are file paths."""
    sender, password, to = email_config()
    if not (sender and password and to):
        return {"sent": False, "detail": "Email not set up - add it on the Settings page"}
    msg = MIMEMultipart()
    msg["From"], msg["To"], msg["Subject"] = sender, ", ".join(to), subject
    msg.attach(MIMEText(report, "plain"))
    for p in (images or [])[:3]:
        try:
            with open(p, "rb") as f:
                msg.attach(MIMEImage(f.read(), name=os.path.basename(p)))
        except OSError:
            pass
    if clip and os.path.exists(clip) and os.path.getsize(clip) < 15 * 2**20:      # Gmail limit is 25 MB
        with open(clip, "rb") as f:
            part = MIMEApplication(f.read(), _subtype="mp4")
        part.add_header("Content-Disposition", "attachment", filename=os.path.basename(clip))
        msg.attach(part)
    try:
        with smtplib.SMTP("smtp.gmail.com", 587, timeout=25) as s:
            s.starttls()
            s.login(sender, password)
            s.send_message(msg, to_addrs=to)
        return {"sent": True, "detail": "Sent to " + ", ".join(to)}
    except smtplib.SMTPAuthenticationError:
        return {"sent": False, "detail": "Gmail rejected the login. Use a 16-letter App Password (not your normal Gmail password); "
                                         "2-Step Verification must be ON for that Google account."}
    except Exception as e:
        return {"sent": False, "detail": "Send failed: " + str(e)[:120]}


def write_clip(video_path, start, end, out_path):
    """Cut [start, end] frames to an H.264 mp4 that browsers can play."""
    cap = cv2.VideoCapture(video_path)
    fps = cap.get(cv2.CAP_PROP_FPS) or 30
    w, h = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH)), int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT))
    raw = out_path + ".raw.mp4"
    out = cv2.VideoWriter(raw, cv2.VideoWriter_fourcc(*"mp4v"), fps, (w, h))
    cap.set(cv2.CAP_PROP_POS_FRAMES, start)
    for _ in range(start, end + 1):
        ok, frame = cap.read()
        if not ok:
            break
        out.write(frame)
    cap.release()
    out.release()
    return transcode(raw, out_path)


def transcode(raw, out_path):
    """mp4v -> H.264 (plays in browsers) when ffmpeg exists; otherwise keep the mp4v file."""
    if shutil.which("ffmpeg"):
        r = subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-i", raw, "-vcodec", "libx264", "-pix_fmt", "yuv420p",
                            "-movflags", "+faststart", out_path], capture_output=True)
        if r.returncode == 0:
            os.remove(raw)
            return out_path
    os.replace(raw, out_path)
    return out_path


def build_report(r):
    sev, w, svc = r["severity"], r["weather"], r["services"]
    m = r["metrics"]

    def line(x):
        return f"{x['name']} ({x['distance_km']} km)" if x else "Not available"

    veh = r.get("vehicles") or []
    vlines = "\n".join(f"  - {v['type']} #{v['id']}: speed before impact {v['impact_speed_ratio']:.2f} body-lengths/s"
                       f" ({v['impact_speed_px_s']:.0f} px/s)" for v in veh) or "  (not tracked)"
    factors = "\n".join(f"  - {f}" for f in sev.get("factors", []))
    return f"""Accident Detection System - Report
==================================
Source: {r['filename']}
Date/Time: {r['finished_at']}
Location: {r['location']['lat']}, {r['location']['lon']}

Accident detected: {'YES' if r['accident_detected'] else 'NO'}
Severity: {sev['level']} - {sev['message']}
{('Why:' + chr(10) + factors) if factors else ''}

Vehicles involved ({len(veh)}):
{vlines}

Collision frames: {m['collision_count']}
Peak overlap (IoU): {m.get('peak_iou', 0):.2f}
Accident confirmed at frame {m['accident_frame']} ({m['accident_time_s']} s)

Weather: {w['description']}, {w['temperature'] if w['temperature'] is not None else 'n/a'} C

Nearest police: {line(svc['police'])}
Nearest hospital: {line(svc['hospital'])}

Automated report. Confirm details on site."""


# ---- main entry ---------------------------------------------------------------
def analyze_video(video_path, out_dir, detector, class_names=None, location=ACCIDENT_LOCATION, progress=None, filename=None):
    """detector(frame) -> list of [x1, y1, x2, y2, conf, cls].
    progress(dict) is called with live stats; returns the result dict."""
    os.makedirs(out_dir, exist_ok=True)
    progress = progress or (lambda _d: None)
    class_names = class_names or {}
    cap = cv2.VideoCapture(video_path)
    if not cap.isOpened():
        raise ValueError("Could not open video")
    fps = cap.get(cv2.CAP_PROP_FPS) or 30
    total = int(cap.get(cv2.CAP_PROP_FRAME_COUNT)) or 0
    started = time.time()

    frame_no = collisions = prolonged = 0
    max_speed = 0.0
    last_boxes = []
    accident = False
    first_collision_frame = accident_frame = peak_frame = None
    peak_iou = 0.0
    timeline, events, vehicle_events = [], [], set()
    detections_seen = {}
    evidence = {}

    messages = []

    def notify(level, title, text):
        """level: alert | warn | info | ok. Pushed live to the dashboard and kept in the result."""
        m = {"time": datetime.now().strftime("%H:%M:%S"), "level": level, "title": title, "text": text}
        messages.append(m)
        progress({"message": m})

    def save(name, frame):
        p = os.path.join(out_dir, name)
        cv2.imwrite(p, frame)
        return name

    while True:
        ok, frame = cap.read()
        if not ok:
            break
        vehicle_events.update(simulated_vehicle_telemetry())
        dets = detector(frame)
        boxes = [d[:4] for d in dets]
        frame_collisions, frame_peak = 0, 0.0

        for i, d in enumerate(dets):
            x1, y1, x2, y2, conf, cls = d
            cname = class_names.get(int(cls), f"class {int(cls)}")
            detections_seen[cname] = detections_seen.get(cname, 0) + 1
            col = (0, 0, 255) if accident else (0, 200, 0)
            cv2.rectangle(frame, (int(x1), int(y1)), (int(x2), int(y2)), col, 2)
            label = f"{cname} {conf:.2f}"
            if i < len(last_boxes):
                sp = calculate_speed(last_boxes[i], d[:4])
                max_speed = max(max_speed, sp)
                label += f" | {sp:.1f}px/s"
            cv2.putText(frame, label, (int(x1), max(15, int(y1) - 8)), cv2.FONT_HERSHEY_SIMPLEX, 0.5, (255, 220, 0), 2)

        for i in range(len(boxes)):
            for j in range(i + 1, len(boxes)):
                iou = calculate_iou(boxes[i], boxes[j])
                if iou > IOU_COLLISION:
                    frame_collisions += 1
                    collisions += 1
                    frame_peak = max(frame_peak, iou)
                    prolonged = min(prolonged + 1, PROLONGED_COLLISION_FRAMES)
                    if first_collision_frame is None:
                        first_collision_frame = frame_no
                        events.append({"frame": frame_no, "t": round(frame_no / fps, 2), "type": "first_collision",
                                       "text": "First overlapping vehicles detected"})
                        evidence["first"] = save("evidence_first_collision.jpg", frame)
                        notify("warn", "Collision suspected",
                               f"Vehicles overlapping at {frame_no / fps:.2f} s (frame {frame_no}). Watching for a prolonged collision.")
                    if frame_peak > peak_iou:
                        peak_iou, peak_frame = frame_peak, frame_no
                        evidence["peak"] = save("evidence_peak_impact.jpg", frame)
                    if prolonged >= PROLONGED_COLLISION_FRAMES and not accident:
                        accident = True
                        accident_frame = frame_no
                        events.append({"frame": frame_no, "t": round(frame_no / fps, 2), "type": "accident",
                                       "text": "Prolonged collision threshold reached - accident confirmed"})

        timeline.append({"f": frame_no, "objects": len(dets), "collisions": frame_collisions, "iou": round(frame_peak, 3)})
        last_boxes = boxes

        if accident:
            cv2.putText(frame, "ACCIDENT DETECTED", (40, 50), cv2.FONT_HERSHEY_SIMPLEX, 1.1, (0, 0, 255), 3)
            evidence["confirmed"] = save("evidence_accident_confirmed.jpg", frame)
            h, w = frame.shape[:2]
            save("live.jpg", cv2.resize(frame, (640, int(640 * h / w))))
            notify("alert", "ACCIDENT DETECTED",
                   f"Confirmed at {accident_frame / fps:.2f} s (frame {accident_frame}) in {filename or os.path.basename(video_path)}. "
                   f"{collisions} collision frames, max speed {max_speed:.1f} px/s, peak overlap {peak_iou:.2f}. "
                   f"Location {location[0]}, {location[1]}. Detected at {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}.")
            break  # same as main.py: stop once the accident is confirmed

        if frame_no % 5 == 0:
            h, w = frame.shape[:2]
            small = cv2.resize(frame, (640, int(640 * h / w)))
            save("live.jpg", small)
            progress({"frame": frame_no, "total": total, "collisions": collisions,
                      "max_speed": round(max_speed, 2), "objects": len(dets),
                      "prolonged": prolonged, "elapsed": round(time.time() - started, 1)})
        frame_no += 1

    cap.release()

    weather = {"description": "n/a", "temperature": None, "error": None}
    services = {"police": None, "hospital": None, "error": None}
    clip = None
    email = {"sent": False, "detail": "No accident - no alert sent"}
    if accident:
        progress({"stage": "Fetching weather and nearest services"})
        weather = get_weather(*location)
        services = nearest_services(*location)
        wtxt = (f"{weather['description']}, {weather['temperature']:.1f} °C" if weather["temperature"] is not None
                else f"unavailable ({weather['error']})")
        notify("info", "Weather checked", wtxt)
        for label, key in (("Nearest hospital", "hospital"), ("Nearest police station", "police")):
            sv = services[key]
            notify("info", label, f"{sv['name']} - {sv['distance_km']} km away" if sv
                   else f"none found within 5 km{' (' + services['error'] + ')' if services['error'] else ''}")
        start = max(0, (first_collision_frame or accident_frame) - int(fps))
        end = accident_frame + int(fps)
        try:
            clip = os.path.basename(write_clip(video_path, start, end, os.path.join(out_dir, "accident_clip.mp4")))
        except Exception:
            clip = None

    level, msg = estimate_severity(collisions, max_speed, weather["description"]) if accident else ("NONE", "No accident detected")
    result = {
        "filename": filename or os.path.basename(video_path),
        "finished_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "accident_detected": accident,
        "location": {"lat": location[0], "lon": location[1]},
        "video": {"fps": round(fps, 2), "frames_total": total, "frames_processed": frame_no + 1 if accident else frame_no,
                  "duration_s": round(total / fps, 2) if total else None},
        "metrics": {"collision_count": collisions, "max_speed": round(max_speed, 2), "peak_iou": round(peak_iou, 3),
                    "first_collision_frame": first_collision_frame, "accident_frame": accident_frame,
                    "accident_time_s": round(accident_frame / fps, 2) if accident_frame is not None else None,
                    "thresholds": {"iou": IOU_COLLISION, "prolonged_frames": PROLONGED_COLLISION_FRAMES,
                                   "speed": SPEED_THRESHOLD}},
        "severity": {"level": level, "message": msg},
        "weather": weather,
        "services": services,
        "vehicle_events": sorted(vehicle_events),
        "vehicle_events_simulated": True,
        "objects_detected": detections_seen,
        "events": events,
        "timeline": timeline,
        "evidence": evidence,
        "clip": clip,
        "processing_s": round(time.time() - started, 1),
    }
    if not accident:
        notify("ok", "Analysis complete", "No accident detected in this video.")
    else:
        notify("alert", f"Severity: {level}", msg)
    result["messages"] = messages
    result["report_text"] = build_report(result)
    if accident:
        imgs = [os.path.join(out_dir, v) for v in evidence.values()]
        result["email"] = send_email("Accident Detection System - Critical Report", result["report_text"], imgs, clip)
        notify("ok" if result["email"]["sent"] else "warn", "Emergency email", result["email"]["detail"])
    else:
        result["email"] = email
    return result
