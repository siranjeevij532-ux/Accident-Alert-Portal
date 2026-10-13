FROM python:3.11-slim

# ffmpeg -> browser-playable accident clips; libglib/libgl -> OpenCV runtime
RUN apt-get update && apt-get install -y --no-install-recommends ffmpeg libglib2.0-0 libgl1 \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt \
    # ultralytics drags in the GUI build of OpenCV; keep only the headless one (no display on a server)
    && pip uninstall -y opencv-python \
    && pip install --no-cache-dir --force-reinstall --no-deps opencv-python-headless

# Download the YOLO weights at build time so the first request is not slow
RUN mkdir -p models && cd models && python -c "from ultralytics import YOLO; YOLO('yolo11n.pt')"

COPY . .
ENV YOLO_MODEL=yolo11n.pt HOST=0.0.0.0 PYTHONUNBUFFERED=1

# ONE worker on purpose: the live monitor keeps its state in memory. Threads handle parallel requests.
CMD ["sh", "-c", "gunicorn server:app --bind 0.0.0.0:${PORT:-10000} --workers 1 --threads 8 --timeout 180"]
