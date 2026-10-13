"""Live-camera accident monitor (webcam, USB camera, IP/RTSP stream).

Only cars, bikes, buses and trucks are detected (the detector is filtered to those classes).
Each vehicle gets a tracking id, so speed is measured per vehicle.

Accident rule (same idea as main.py): vehicles whose boxes overlap (IoU > 0.2) for a prolonged
time. The counter goes UP on frames with an overlap and DOWN on clear frames, so unrelated overlaps
hours apart cannot add up to a false accident.

When an accident is confirmed it immediately raises an alert (portal banner, sound, desktop
notification, webhook), then saves evidence + a clip (seconds before and after), looks up weather
and the nearest hospital / police, sends the email and writes a report page.
"""
import json
import os
import queue
import threading
import time
import uuid
from collections import deque
from datetime import datetime

import cv2
import numpy as np
import requests

import db
import pipeline as P

COOLDOWN_S = 45          # ignore new accidents for this long after one is confirmed
PRE_ROLL_S = 4           # seconds of video kept before the accident
POST_ROLL_S = 3          # seconds recorded after it
STREAM_W = 960
MIN_TRACK_AGE = 3        # a vehicle must be seen for 3 frames before it can count in a collision
EVENT_WINDOW_FRAMES = 200   # frames kept around an accident (approach -> overlap). Was 400; halved to cut memory/CPU on Render


def _jpeg(img, q=75):
    ok, buf = cv2.imencode(".jpg", img, [cv2.IMWRITE_JPEG_QUALITY, q])
    return buf.tobytes() if ok else None


def _fit(img, w=STREAM_W):
    h0, w0 = img.shape[:2]
    return img if w0 <= w else cv2.resize(img, (w, int(h0 * w / w0)))


class BrowserCapture:
    """cv2.VideoCapture look-alike fed by frames the visitor's browser POSTs to /api/live/frame.
    Lets the deployed website use the viewer's own webcam (a cloud server has no camera)."""
    def __init__(self):
        self._q = queue.Queue(maxsize=1)      # keep only the newest frame
        self._closed = False

    def isOpened(self):
        return not self._closed

    def push(self, frame):
        if self._closed:
            return False
        try:
            self._q.get_nowait()              # drop the stale frame
        except queue.Empty:
            pass
        self._q.put_nowait(frame)
        return True

    def read(self):
        try:
            return True, self._q.get(timeout=1.0)
        except queue.Empty:
            return False, None

    def set(self, *a):
        pass

    def release(self):
        self._closed = True


class LiveMonitor:
    def __init__(self, out_root):
        self.out_root = out_root
        self.lock = threading.Lock()
        self.thread = None
        self.stop_evt = threading.Event()
        self.jpeg = None
        self.browser_cap = None
        self._reset()

    def _reset(self):
        self.state = {"running": False, "status": "idle", "error": None, "source": None, "model": None, "started": None}
        self.stats = {}
        self.messages = []
        self.incidents = []

    # ---- public ----
    def running(self):
        return bool(self.thread and self.thread.is_alive())

    def snapshot(self):
        with self.lock:
            return {"now": time.time(), "state": dict(self.state), "stats": dict(self.stats),
                    "messages": list(self.messages), "incidents": [dict(i) for i in self.incidents]}

    def start(self, detector_factory, source, location):
        if self.running():
            raise RuntimeError("Live monitoring is already running")
        with self.lock:
            self._reset()
            self.jpeg = None
            self.state.update(running=True, status="starting", source=str(source),
                              started=datetime.now().strftime("%Y-%m-%d %H:%M:%S"), started_ts=time.time())
        self.stop_evt.clear()
        # browser source: accept frames right away (kept as "newest only") so the model-loading minute doesn't 409
        self.browser_cap = BrowserCapture() if str(source).strip().lower() == "browser" else None
        self.thread = threading.Thread(target=self._run, args=(detector_factory, source, location), daemon=True)
        self.thread.start()

    def stop(self):
        self.stop_evt.set()
        if self.thread:
            self.thread.join(timeout=8)

    # ---- internals ----
    def _notify(self, level, title, text, incident=None):
        m = {"time": datetime.now().strftime("%H:%M:%S"), "level": level, "title": title, "text": text}
        if incident:
            m["incident"] = incident
        with self.lock:
            self.messages.append(m)
        return m

    def _set(self, **kw):
        with self.lock:
            self.state.update(kw)

    def push_frame(self, frame):
        cap = self.browser_cap
        return bool(cap and self.running() and cap.push(frame))

    def _open(self, source):
        if str(source).strip().lower() == "browser":
            return self.browser_cap
        src = int(source) if str(source).strip().isdigit() else source
        if isinstance(src, int) and os.name == "nt":
            cap = cv2.VideoCapture(src, cv2.CAP_DSHOW)   # faster / more reliable on Windows
        else:
            cap = cv2.VideoCapture(src)
        try:
            cap.set(cv2.CAP_PROP_BUFFERSIZE, 1)
        except Exception:
            pass
        return cap

    def _run(self, detector_factory, source, location):
        cap = None
        try:
            self._set(status="loading model")
            detector, names, model_name = detector_factory()
            self._set(model=model_name, status="opening camera")
            cap = self._open(source)
            if not cap.isOpened():
                raise RuntimeError(f"Could not open camera/source '{source}'. Is another app using the camera?")
            if str(source).lower() == "browser":
                self._set(status="waiting for browser camera")
            self._notify("ok", "Live monitoring started", f"Source: {source} · model {model_name} · detecting car, bike, bus, truck")
            self._loop(cap, detector, names, location)
        except Exception as e:
            self._set(error=str(e), status="error")
            self._notify("warn", "Live monitoring stopped", str(e))
        finally:
            if cap is not None:
                cap.release()
            self.browser_cap = None
            self._set(running=False)
            with self.lock:
                if self.state["status"] not in ("error",):
                    self.state["status"] = "stopped"

    def _loop(self, cap, detector, names, location):
        t0 = last_t = time.time()
        frame_no = 0
        fps = 15.0
        fails = 0
        pre = deque(maxlen=EVENT_WINDOW_FRAMES)   # jpeg bytes of recent annotated frames
        timeline = deque(maxlen=EVENT_WINDOW_FRAMES)
        tracker = P.SimpleTracker()
        prolonged = 0
        session_collisions = 0
        ep = None                          # current collision episode
        cooldown_until = 0.0
        rec = None                         # post-roll recording context
        det_counts = {}

        try:
            while not self.stop_evt.is_set():
                ok, frame = cap.read()
                if not ok:
                    fails += 1
                    if fails > 30:
                        raise RuntimeError("Lost the video feed (camera disconnected or stream ended).")
                    time.sleep(0.05)
                    continue
                fails = 0
                now = time.time()
                dt = max(1e-3, now - last_t)
                last_t = now
                fps = 0.9 * fps + 0.1 * (1.0 / dt)

                dets = [d for d in detector(frame) if int(d[5]) in names]      # vehicles only
                tracks = tracker.update(dets, now)
                in_cooldown = now < cooldown_until

                # ---- overlaps between tracked vehicles ----
                frame_coll, frame_peak, involved = 0, 0.0, set()
                for i in range(len(dets)):
                    for j in range(i + 1, len(dets)):
                        if tracks[i]["age"] < MIN_TRACK_AGE or tracks[j]["age"] < MIN_TRACK_AGE:
                            continue
                        iou = P.calculate_iou(dets[i][:4], dets[j][:4])
                        if iou > P.IOU_COLLISION:
                            frame_coll += 1
                            frame_peak = max(frame_peak, iou)
                            involved.update((i, j))

                # ---- draw ----
                for i, d in enumerate(dets):
                    t = tracks[i]
                    cname = names.get(t["cls"], "vehicle")
                    det_counts[cname] = det_counts.get(cname, 0) + 1
                    col = (0, 0, 255) if (i in involved or in_cooldown) else (0, 200, 0)
                    cv2.rectangle(frame, (int(d[0]), int(d[1])), (int(d[2]), int(d[3])), col, 2)
                    cv2.putText(frame, f"{cname} #{t['id']}  {t['speed']:.0f}px/s", (int(d[0]), max(15, int(d[1]) - 8)),
                                cv2.FONT_HERSHEY_SIMPLEX, 0.5, (255, 220, 0), 2)

                # ---- accident logic ----
                triggered = False
                if not in_cooldown:
                    if frame_coll:
                        prolonged = min(prolonged + 1, P.PROLONGED_COLLISION_FRAMES)
                        session_collisions += frame_coll
                        if ep is None:
                            ep = {"first_frame": frame_no, "collisions": 0, "peak_iou": 0.0, "vehicles": {},
                                  "first_img": None, "peak_img": None, "events": []}
                            ep["first_img"] = _jpeg(frame, 85)
                            ep["events"].append({"frame": frame_no, "t": round(now - t0, 2), "type": "first_collision",
                                                 "text": "First overlapping vehicles detected"})
                            self._notify("warn", "Collision suspected",
                                         f"Vehicles overlapping at {now - t0:.1f} s of monitoring. Watching for a prolonged collision.")
                        ep["collisions"] += frame_coll
                        for idx in involved:
                            t = tracks[idx]
                            v = ep["vehicles"].setdefault(t["id"], {"id": t["id"], "type": "", "speed": 0.0, "ratio": 0.0})
                            v["type"] = names.get(t["cls"], "vehicle")
                            for sp, dg in t["hist"]:
                                v["speed"] = max(v["speed"], sp)
                                v["ratio"] = max(v["ratio"], sp / dg)
                        if frame_peak > ep["peak_iou"]:
                            ep["peak_iou"] = frame_peak
                            ep["peak_img"] = _jpeg(frame, 85)
                        if prolonged >= P.PROLONGED_COLLISION_FRAMES:
                            triggered = True
                    else:
                        prolonged = max(0, prolonged - 1)
                        if prolonged == 0 and ep is not None:
                            ep = None   # false alarm, vehicles separated

                timeline.append({"f": frame_no, "objects": len(dets), "collisions": frame_coll, "iou": round(frame_peak, 3)})

                if triggered:
                    cv2.putText(frame, "ACCIDENT DETECTED", (40, 50), cv2.FONT_HERSHEY_SIMPLEX, 1.1, (0, 0, 255), 3)
                elif in_cooldown:
                    cv2.putText(frame, "ACCIDENT ALERT SENT - cooling down", (40, 50), cv2.FONT_HERSHEY_SIMPLEX, 0.8, (0, 0, 255), 2)

                jpg = _jpeg(_fit(frame), 72)
                pre.append(jpg)
                self.jpeg = jpg

                if rec is not None:
                    rec["frames"].append(jpg)
                    rec["left"] -= 1
                    if rec["left"] <= 0:
                        rec["done"].set()
                        rec = None

                if triggered:
                    rec = self._on_accident(ep, pre, fps, frame, frame_no, now - t0, timeline, location,
                                            det_counts, t0)
                    cooldown_until = now + COOLDOWN_S
                    prolonged = 0
                    ep = None

                status = ("cooldown" if in_cooldown or triggered else "collision" if prolonged > 0 else "monitoring")
                with self.lock:
                    self.state["status"] = status
                    self.stats = {"frame": frame_no, "fps": round(fps, 1), "objects": len(dets), "prolonged": prolonged,
                                  "prolonged_max": P.PROLONGED_COLLISION_FRAMES, "collisions": session_collisions,
                                  "uptime": round(now - t0), "incidents": len(self.incidents)}
                frame_no += 1
        finally:
            if rec is not None:       # stopped / feed lost while still recording post-roll
                rec["done"].set()

    # ---- accident confirmed ----
    def _on_accident(self, ep, pre, fps, frame, frame_no, acc_t, timeline, location, det_counts, t0):
        vehicles = sorted(ep["vehicles"].values(), key=lambda v: v["id"])
        types = sorted({v["type"] for v in vehicles})
        impact_ratio = max((v["ratio"] for v in vehicles), default=0.0)
        sev = P.assess_severity(len(vehicles), ep["peak_iou"], impact_ratio, None, "bike" in types)
        inc_id = "live_" + datetime.now().strftime("%Y%m%d_%H%M%S") + uuid.uuid4().hex[:3]
        stamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        summary = (f"{len(vehicles)} vehicles ({', '.join(types)}) · severity {sev['level']} · {stamp} · "
                   f"location {location[0]}, {location[1]}")
        ep["events"].append({"frame": frame_no, "t": round(acc_t, 2), "type": "accident",
                             "text": "Prolonged collision threshold reached - accident confirmed"})
        clip_fps = max(5.0, min(30.0, fps))
        rec = {"frames": list(pre)[-int(clip_fps * PRE_ROLL_S):], "left": int(clip_fps * POST_ROLL_S),
               "done": threading.Event(), "fps": clip_fps}
        ctx = {"id": inc_id, "ep": ep, "rec": rec, "confirmed_img": _jpeg(frame, 90), "frame_no": frame_no, "acc_t": acc_t,
               "timeline": list(timeline), "fps": fps, "location": location, "objects": dict(det_counts),
               "model": self.state.get("model"), "summary": summary, "stamp": stamp, "vehicles": vehicles}
        with self.lock:
            self.incidents.append({"id": inc_id, "time": stamp, "ts": time.time(), "severity": sev["level"],
                                   "summary": summary, "ready": False})
        self._notify("alert", "ACCIDENT DETECTED",
                     f"{summary}. Confirmed at {acc_t:.1f} s of monitoring (frame {frame_no}); "
                     f"{ep['collisions']} collision frames, peak overlap {ep['peak_iou']:.2f}.", incident=inc_id)
        threading.Thread(target=self._finalize, args=(ctx,), daemon=True).start()
        return rec

    def _finalize(self, ctx):
        """Own thread: the video loop never waits on the network."""
        ep, rec, loc, inc_id = ctx["ep"], ctx["rec"], ctx["location"], ctx["id"]
        out_dir = os.path.join(self.out_root, inc_id)
        os.makedirs(out_dir, exist_ok=True)
        started = time.time()
        msgs = []

        def note(level, title, text):
            msgs.append(self._notify(level, title, text, incident=inc_id))

        webhook = os.getenv("ALERT_WEBHOOK")
        if webhook:
            try:
                requests.post(webhook, timeout=8, json={
                    "event": "accident_detected", "incident": inc_id, "time": ctx["stamp"], "summary": ctx["summary"],
                    "location": {"lat": loc[0], "lon": loc[1]}, "report_path": f"/report/{inc_id}"})
                note("ok", "Webhook sent", "Accident alert posted to ALERT_WEBHOOK")
            except Exception as e:
                note("warn", "Webhook failed", str(e)[:120])

        evidence = {}
        for key, name, data in (("first", "evidence_first_collision.jpg", ep["first_img"]),
                                ("peak", "evidence_peak_impact.jpg", ep["peak_img"]),
                                ("confirmed", "evidence_accident_confirmed.jpg", ctx["confirmed_img"])):
            if data:
                with open(os.path.join(out_dir, name), "wb") as f:
                    f.write(data)
                evidence[key] = name

        weather = P.get_weather(*loc)
        services = P.nearest_services(*loc)
        note("info", "Weather checked", f"{weather['description']}, {weather['temperature']:.1f} °C"
             if weather["temperature"] is not None else f"unavailable ({weather['error']})")
        for label, key in (("Nearest hospital", "hospital"), ("Nearest police station", "police")):
            sv = services[key]
            note("info", label, f"{sv['name']} - {sv['distance_km']} km away" if sv
                 else f"none found within 40 km{' (' + services['error'] + ')' if services['error'] else ''}")

        rec["done"].wait(timeout=POST_ROLL_S * 4 + 10)
        clip = None
        try:
            frames = [cv2.imdecode(np.frombuffer(b, "uint8"), cv2.IMREAD_COLOR) for b in rec["frames"]]
            frames = [f for f in frames if f is not None]
            if frames:
                h, w = frames[0].shape[:2]
                raw = os.path.join(out_dir, "accident_clip.mp4.raw.mp4")
                vw = cv2.VideoWriter(raw, cv2.VideoWriter_fourcc(*"mp4v"), rec["fps"], (w, h))
                for f in frames:
                    vw.write(f)
                vw.release()
                clip = os.path.basename(P.transcode(raw, os.path.join(out_dir, "accident_clip.mp4")))
        except Exception as e:
            note("warn", "Clip not saved", str(e)[:120])

        vehicles = [{"id": v["id"], "type": v["type"], "impact_speed_px_s": round(v["speed"], 1),
                     "impact_speed_ratio": round(v["ratio"], 2)} for v in ctx["vehicles"]]
        types = sorted({v["type"] for v in vehicles})
        impact_ratio = max((v["impact_speed_ratio"] for v in vehicles), default=0.0)
        impact_px = max((v["impact_speed_px_s"] for v in vehicles), default=0.0)
        sev = P.assess_severity(len(vehicles), ep["peak_iou"], impact_ratio, weather["description"], "bike" in types)
        note("alert", f"Severity: {sev['level']}", f"{sev['message']} - " + "; ".join(sev["factors"]))

        result = {
            "id": inc_id, "filename": "Live camera", "finished_at": ctx["stamp"],
            "accident_detected": True, "location": {"lat": loc[0], "lon": loc[1]},
            "video": {"fps": round(ctx["fps"], 2), "frames_total": ctx["frame_no"] + 1, "frames_processed": ctx["frame_no"] + 1,
                      "duration_s": round(ctx["acc_t"], 2)},
            "metrics": {"collision_count": ep["collisions"], "max_speed": round(impact_px, 2), "impact_speed_ratio": impact_ratio,
                        "peak_iou": round(ep["peak_iou"], 3), "first_collision_frame": ep["first_frame"],
                        "accident_frame": ctx["frame_no"], "accident_time_s": round(ctx["acc_t"], 2),
                        "thresholds": {"iou": P.IOU_COLLISION, "prolonged_frames": P.PROLONGED_COLLISION_FRAMES,
                                       "speed": P.SPEED_THRESHOLD}},
            "severity": sev, "weather": weather, "services": services,
            "vehicles": vehicles, "vehicle_events": [], "vehicle_events_simulated": True,
            "objects_detected": ctx["objects"], "events": ep["events"], "timeline": ctx["timeline"],
            "evidence": evidence, "clip": clip, "processing_s": round(time.time() - started, 1),
            "model": ctx["model"], "summary": ctx["summary"],
        }
        result["report_text"] = P.build_report(result)
        imgs = [os.path.join(out_dir, v) for v in evidence.values()]
        result["email"] = P.send_email("Accident Detection System - LIVE camera alert", result["report_text"], imgs,
                                       os.path.join(out_dir, clip) if clip else None)
        note("ok" if result["email"]["sent"] else "warn", "Emergency email", result["email"]["detail"])
        result["messages"] = msgs
        with open(os.path.join(out_dir, "result.json"), "w") as f:
            json.dump(result, f)
        try:                                   # keep the report + evidence + clip in PostgreSQL (survives Render restarts)
            if db.save_incident(result, out_dir):
                note("ok", "Saved to database", "Report, evidence frames and clip stored in PostgreSQL")
        except Exception as e:
            note("warn", "Database save failed", str(e)[:120])
        with self.lock:
            for i in self.incidents:
                if i["id"] == inc_id:
                    i.update(severity=sev["level"], ready=True)
        self._notify("ok", "Report ready", "Full accident report is ready - open it from the alert banner or Accident reports.",
                     incident=inc_id)
