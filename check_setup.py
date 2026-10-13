"""Run this first:  python check_setup.py
Tests each step the live camera needs and says which one is slow or broken."""
import os
import sys
import time

BASE = os.path.dirname(os.path.abspath(__file__))
t0 = time.time()


def step(name):
    print(f"\n[{time.time() - t0:5.0f}s] {name} ...", flush=True)


step("Python"); print("   ", sys.version.split()[0])

step("import cv2 (OpenCV)")
import cv2
print("    OpenCV", cv2.__version__)

step("import torch (slow the first time - can take 1-2 minutes)")
import torch
print("    torch", torch.__version__, "| GPU available:", torch.cuda.is_available())

step("import ultralytics")
from ultralytics import YOLO

weights = os.getenv("YOLO_MODEL", "yolo11n.pt")
local = os.path.join(BASE, "models", weights)
step(f"load weights {weights} (downloads ~5 MB the first time; a progress bar should appear)")
model = YOLO(local if os.path.exists(local) else weights)
print("    model loaded")

step("open camera 0")
cap = cv2.VideoCapture(0, cv2.CAP_DSHOW) if os.name == "nt" else cv2.VideoCapture(0)
if not cap.isOpened():
    print("    FAILED: camera 0 could not be opened. Close Zoom/Teams/Camera app, check Windows camera privacy settings,")
    print("    or try camera number 1 in the dashboard.")
    sys.exit(1)
ok = False
for _ in range(10):
    ok, frame = cap.read()
    if ok:
        break
    time.sleep(0.2)
cap.release()
if not ok:
    print("    FAILED: camera opened but gave no image.")
    sys.exit(1)
print("    camera OK, frame size", frame.shape[1], "x", frame.shape[0])

step("run detection on one camera frame")
t1 = time.time()
r = model(frame, verbose=False)[0]
print(f"    {len(r.boxes)} objects found in {time.time() - t1:.2f}s")
print("\nALL GOOD - now run: python server.py")
