#!/usr/bin/env python3
"""Head tracking on this computer, for any screen: a native tracker BeamDeck starts when a tablet or browser picks a
camera under "This computer". No window, so nothing pauses it while the game covers the desktop (a browser page did
pause: Edge's tab sleeping is not controlled by Chromium's flags).

It does what the page tracker (head.js + headfilter.js) does, with the same maths: MediaPipe's face landmarker finds
the face, the facial transformation matrix gives the head pose, a One Euro filter per axis smooths it, and the response
curve turns it into the view's offset, sent to the game through the helper (api/head). Every half second it reports its
status, the face markers and a small preview (api/headhost/report) and gets back the screen's commands (start with a
camera name and the settings, stop, recenter, settings).

Runs in BeamDeck's head tracking environment (<data>/headtrack: mediapipe, opencv-python-headless):
    python headtrack.py --helper http://127.0.0.1:47130/routes
"""
import argparse
import base64
import glob
import json
import math
import os
import sys
import threading
import time
import urllib.request

import cv2
import mediapipe as mp
import numpy as np
from mediapipe.tasks.python import vision
from mediapipe.tasks.python.core import base_options

R2D = 180 / math.pi


# ---------------------------------------------------------------- the maths (as headfilter.js)
class OneEuro:
    def __init__(self, min_cutoff, beta, d_cutoff=1.0):
        self.min_cutoff, self.beta, self.d_cutoff = min_cutoff, beta, d_cutoff
        self.reset()

    def reset(self):
        self.x, self.dx, self.t = None, 0.0, None

    @staticmethod
    def alpha(cutoff, dt):
        tau = 1 / (2 * math.pi * cutoff)
        return 1 / (1 + tau / dt)

    def filter(self, v, t):
        if self.x is None or t <= self.t:
            self.x, self.dx, self.t = v, 0.0, t
            return v
        dt = min(t - self.t, 0.25)
        dv = (v - self.x) / dt
        self.dx += OneEuro.alpha(self.d_cutoff, dt) * (dv - self.dx)
        cutoff = self.min_cutoff + self.beta * abs(self.dx)
        self.x += OneEuro.alpha(cutoff, dt) * (v - self.x)
        self.t = t
        return self.x


def pose_from_matrix(m):
    """m: the 4x4 facial transformation matrix (row-major here; headfilter.js reads the column-major copy)."""
    r02, r12, r22, r10, r11 = m[0][2], m[1][2], m[2][2], m[1][0], m[1][1]
    s = math.sqrt(r02 * r02 + r12 * r12 + r22 * r22) or 1.0
    return {"yaw": -math.atan2(r02, r22) * R2D, "pitch": math.asin(max(-1.0, min(1.0, r12 / s))) * R2D,
            "roll": math.atan2(r10, r11) * R2D, "x": -m[0][3], "y": m[1][3], "z": m[2][3]}


AXES = ["yaw", "pitch", "roll", "x", "y", "z"]
ROT = {"yaw", "pitch", "roll"}
DEFAULTS = {"yaw": {"gain": 2.0, "invert": False}, "pitch": {"gain": 1.5, "invert": False},
            "roll": {"gain": 0.5, "invert": False}, "x": {"gain": 1.0, "invert": False},
            "y": {"gain": 1.0, "invert": False}, "z": {"gain": 1.0, "invert": False},
            "curve": 0.6, "deadzone": 1.0, "smoothness": 0.5, "quick": 0.5,
            "maxYaw": 120, "maxPitch": 60, "maxRoll": 45, "maxLean": 30}
REF = {"yaw": 30, "pitch": 30, "roll": 30, "x": 10, "y": 10, "z": 10}


def merge_settings(saved):
    s = json.loads(json.dumps(DEFAULTS))
    for k, v in (saved or {}).items():
        if k in AXES and isinstance(v, dict):
            if isinstance(v.get("gain"), (int, float)):
                s[k]["gain"] = max(0.0, min(8.0, float(v["gain"])))
            s[k]["invert"] = bool(v.get("invert"))
        elif k in s and isinstance(v, (int, float)) and not isinstance(v, bool):
            s[k] = v
    return s


def respond(axis, v, s):
    dz = s["deadzone"] if axis in ROT else s["deadzone"] / 3
    a = max(0.0, abs(v) - dz) * (1 if v > 0 else -1 if v < 0 else 0)
    out = s[axis]["gain"] * a * (1 + s["curve"] * abs(a) / REF[axis])
    lim = {"yaw": s["maxYaw"], "pitch": s["maxPitch"], "roll": s["maxRoll"]}.get(axis, s["maxLean"])
    return max(-lim, min(lim, out)) * (-1 if s[axis]["invert"] else 1)


cutoff_of = lambda smoothness: 4.0 * math.pow(0.1, smoothness)
beta_of = lambda quick: quick * 0.05


class Filter:
    def __init__(self, settings):
        self.s = merge_settings(settings)
        self.f = {a: OneEuro(cutoff_of(self.s["smoothness"]), beta_of(self.s["quick"])) for a in AXES}
        self.center, self.recent = None, []

    def set_settings(self, settings):
        self.s = merge_settings(settings)
        for a in AXES:
            self.f[a].min_cutoff, self.f[a].beta = cutoff_of(self.s["smoothness"]), beta_of(self.s["quick"])

    def lost(self):
        for a in AXES:
            self.f[a].reset()
        self.recent = []

    def recenter(self, raw=None):
        pool = self.recent or ([raw] if raw else [])
        if not pool:
            return False
        self.center = {a: sum(p[a] for p in pool) / len(pool) for a in AXES}
        for a in AXES:
            self.f[a].reset()
        return True

    def update(self, raw, t):
        self.recent.append(dict(raw, t=t))
        while self.recent and t - self.recent[0]["t"] > 0.5:
            self.recent.pop(0)
        if not self.center and len(self.recent) >= 10:
            self.recenter()
        if not self.center:
            return None
        rel = {a: self.f[a].filter(raw[a] - self.center[a], t) for a in AXES}
        return {"out": {a: respond(a, rel[a], self.s) for a in AXES}, "rel": rel}


# ---------------------------------------------------------------- the cameras
def cameras():
    """(name, how to open it) for each camera: Linux by the kernel's name of each /dev/videoN picture node."""
    out = []
    if sys.platform.startswith("linux"):
        for d in sorted(glob.glob("/sys/class/video4linux/video*"), key=lambda p: int(p.rsplit("video", 1)[1])):
            try:
                name = open(os.path.join(d, "name")).read().strip()
                idx = int(open(os.path.join(d, "index")).read().strip() or 0)
            except (OSError, ValueError):
                continue
            if idx == 0:
                out.append((name, int(d.rsplit("video", 1)[1])))
    else:                                              # Windows: no names from OpenCV; the first few that open
        for i in range(4):
            c = cv2.VideoCapture(i, cv2.CAP_DSHOW)
            if c.isOpened():
                out.append((f"Camera {i + 1}", i))
            c.release()
    return out


def open_camera(name, size, fps):
    cams = cameras()
    key = str(name or "").split(":")[0].strip().lower()
    pick = next((c for c in cams if key and key in c[0].lower()), cams[0] if cams else None)
    if not pick:
        return None, None
    cap = cv2.VideoCapture(pick[1], cv2.CAP_V4L2 if sys.platform.startswith("linux") else cv2.CAP_DSHOW)
    cap.set(cv2.CAP_PROP_FRAME_WIDTH, size)
    cap.set(cv2.CAP_PROP_FRAME_HEIGHT, round(size * 3 / 4))
    cap.set(cv2.CAP_PROP_FPS, fps)
    cap.set(cv2.CAP_PROP_BUFFERSIZE, 1)                # the newest frame, not a queue of old ones
    if not cap.isOpened():
        return None, pick[0]
    return cap, pick[0]


# ---------------------------------------------------------------- the tracker
class Tracker:
    def __init__(self, helper):
        self.helper = helper.rstrip("/")
        self.settings = {"sens": merge_settings(None), "res": {"size": 640, "fps": 30, "eco": True}}
        self.filter = Filter(self.settings["sens"])
        self.lock = threading.Lock()
        self.cmds = []
        self.cap, self.cam_name, self.running = None, None, False
        self.msg, self.bad = "Waiting for a screen to start it", False
        self.face, self.fps, self.ms, self.sent, self.err = False, 0, 0.0, 0, 0
        self.last = {"raw": None, "out": None, "rel": None, "lm": None}
        self.frame = None
        self.model = None
        self.still = 0
        self.prev = None

    def post(self, path, body, timeout=2.0):
        req = urllib.request.Request(self.helper + path, data=json.dumps(body).encode(), method="POST",
                                     headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.loads(r.read() or b"{}")

    def say(self, text, bad=False):
        self.msg, self.bad = text, bad

    def load_model(self):
        if self.model:
            return self.model
        self.say("Loading the face model…")
        with urllib.request.urlopen(self.helper + "/head/asset/face_landmarker.task", timeout=120) as r:
            data = r.read()
        opts = vision.FaceLandmarkerOptions(base_options=base_options.BaseOptions(model_asset_buffer=data),
                                            running_mode=vision.RunningMode.VIDEO, num_faces=1,
                                            output_facial_transformation_matrixes=True)
        self.model = vision.FaceLandmarker.create_from_options(opts)
        return self.model

    def start(self, camera):
        self.stop(quiet=True)
        res = self.settings["res"]
        self.say("Starting the camera…")
        cap, name = open_camera(camera, int(res.get("size") or 640), int(res.get("fps") or 30))
        if not cap:
            self.say(f"Could not open the camera {name or ''}: is something else using it?".replace("  ", " "), True)
            return
        try:
            self.load_model()
        except Exception as e:
            cap.release()
            self.say(f"Could not load the face model: {e}", True)
            return
        self.cap, self.cam_name, self.running = cap, name, True
        self.filter = Filter(self.settings["sens"])
        self.face, self.still, self.prev = False, 0, None
        self.say("Looking for a face… sit as you do when you drive.")

    def stop(self, quiet=False):
        if self.cap:
            self.cap.release()
        self.cap, self.running, self.frame = None, False, None
        self.last = {"raw": None, "out": None, "rel": None, "lm": None}
        try:
            self.post("/api/head", {"on": True})     # a centred pose, then silence: the view eases back
        except OSError:
            pass
        if not quiet:
            self.say("Tracking is off.")

    def send(self, out):
        try:
            self.post("/api/head", dict(out, on=True), timeout=0.5)
            self.sent += 1
        except OSError:
            self.err += 1

    def run(self):
        t_ms, frames, t0 = 0, 0, time.monotonic()
        while True:
            with self.lock:
                cmds, self.cmds = self.cmds, []
            for c in cmds:
                if c.get("action") == "start":
                    self.start(c.get("camera"))
                elif c.get("action") == "stop":
                    self.stop()
                elif c.get("action") == "recenter" and self.running and self.last["raw"]:
                    self.filter.recenter(self.last["raw"])
                    self.say("Centred on where you are now.")
            if not self.running:
                time.sleep(0.1)
                continue
            res = self.settings["res"]
            fps = 10 if res.get("eco", True) and self.still > 45 else int(res.get("fps") or 30)
            t_start = time.monotonic()
            ok, img = self.cap.read()
            if not ok:
                self.say("The camera stopped giving pictures.", True)
                self.stop(quiet=True)
                continue
            rgb = cv2.cvtColor(img, cv2.COLOR_BGR2RGB)
            t_ms = max(t_ms + 1, int(time.monotonic() * 1000))
            a = time.monotonic()
            r = self.model.detect_for_video(mp.Image(image_format=mp.ImageFormat.SRGB, data=rgb), t_ms)
            self.ms += ((time.monotonic() - a) * 1000 - self.ms) * 0.1
            self.frame = img
            frames += 1
            if time.monotonic() - t0 >= 2:
                self.fps, frames, t0 = round(frames / (time.monotonic() - t0)), 0, time.monotonic()
            mats = getattr(r, "facial_transformation_matrixes", None)
            if mats:
                if not self.face:
                    self.say("Tracking " + (self.cam_name or ""))
                self.face = True
                raw = pose_from_matrix(np.asarray(mats[0]).tolist())
                self.last["raw"] = raw
                self.last["lm"] = [(p.x, p.y) for i, p in enumerate(r.face_landmarks[0]) if i in (1, 33, 263, 61, 291, 199)] if r.face_landmarks else None
                u = self.filter.update(raw, time.monotonic())
                if u:
                    self.last["out"], self.last["rel"] = u["out"], u["rel"]
                    self.send(u["out"])
                    p = self.prev
                    mv = (abs(u["rel"]["yaw"] - p["yaw"]) + abs(u["rel"]["pitch"] - p["pitch"]) + abs(u["rel"]["x"] - p["x"])
                          + abs(u["rel"]["y"] - p["y"])) if p else 9
                    self.still = self.still + 1 if mv < 0.6 else 0
                    self.prev = u["rel"]
            elif self.face:
                self.face = False
                self.filter.lost()
                self.last["out"], self.last["lm"], self.still = None, None, 0
                self.say("No face in the picture.")
            wait = 1 / fps - (time.monotonic() - t_start)
            if wait > 0:
                time.sleep(wait)

    def preview(self):
        img = self.frame
        if img is None:
            return None
        h, w = img.shape[:2]
        small = cv2.resize(cv2.flip(img, 1), (256, round(256 * h / w)))      # a mirror, as on the page tracker
        if self.last["lm"]:
            for x, y in self.last["lm"]:
                cv2.circle(small, (round((1 - x) * small.shape[1]), round(y * small.shape[0])), 3, (52, 177, 242), -1)
        ok, jpg = cv2.imencode(".jpg", small, [cv2.IMWRITE_JPEG_QUALITY, 55])
        return "data:image/jpeg;base64," + base64.b64encode(jpg.tobytes()).decode() if ok else None

    def report_loop(self):
        while True:
            status = {"running": self.running, "camera": self.cam_name, "message": self.msg, "bad": self.bad,
                      "cameras": [c[0] for c in cameras()], "fps": self.fps if self.running else 0, "ms": round(self.ms),
                      "face": self.face, "sent": self.sent, "err": self.err, "raw": self.last["raw"], "out": self.last["out"],
                      "rel": self.last["rel"], "preview": self.preview() if self.running else None, "native": True}
            try:
                r = self.post("/api/headhost/report", {"status": status})
                st = r.get("settings")
                if st and st.get("sens"):
                    self.settings = {"sens": merge_settings(st["sens"]), "res": dict(self.settings["res"], **(st.get("res") or {}))}
                    self.filter.set_settings(self.settings["sens"])
                with self.lock:
                    self.cmds += r.get("cmds") or []
            except OSError:
                pass
            time.sleep(0.5)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--helper", default="http://127.0.0.1:47130/routes")
    a = ap.parse_args()
    t = Tracker(a.helper)
    threading.Thread(target=t.report_loop, daemon=True).start()
    t.run()


if __name__ == "__main__":
    main()
