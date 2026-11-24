"""Head tracking, page to game: needs BeamNG running (any level with a vehicle) with the BeamDeck mod.
   python3 tools/test_head_ui.py [OUTDIR]
Opens the Head page in a test browser with a made-up camera, checks the model files come from the helper verified, sweeps a pretend head
through the real filters into the real game at two sensitivities, and starts and stops the face tracker."""
import hashlib, json, os, sys, time, urllib.error, urllib.request
HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import fake_game as FG
import head_probe as HP
from cdp_shot import Browser

out = sys.argv[1] if len(sys.argv) > 1 else None
data = os.path.expanduser("~/.cache/beamdeck-headtest")            # the downloaded model files stay between runs
os.makedirs(data, exist_ok=True)
h, g, srv = FG.start(data)
BASE = f"http://127.0.0.1:{FG.H.HTTP_PORT}/"
bad = []


def ok(c, m):
    print(("PASS " if c else "FAIL ") + m)
    if not c:
        bad.append(m)


def fetch(path):
    try:
        with urllib.request.urlopen(BASE + path, timeout=120) as r:
            return r.status, r.read(), r.headers
    except urllib.error.HTTPError as e:
        return e.code, e.read(), e.headers


def wait(cond, secs, what=""):
    t = time.time()
    while time.time() - t < secs:
        if cond():
            return True
        time.sleep(0.2)
    print("   (timed out waiting for", what, ")")
    return False


p = HP.probe()
ok(bool(p and p.get("cam")), f"the game is running with the mod (camera: {p and p.get('cam')})")
if not p:
    sys.exit(1)
HP.ask({"type": "cam", "name": "chase"}); time.sleep(1.0)

# ---- the helper's side
code, body, hdr = fetch("head/asset/face_landmarker.task")
ok(code == 200 and hashlib.sha256(body).hexdigest() == "64184e229b263107bc2b804c6625db1341ff2bb731874b0bcc2fe6544e0bc9ff", "the face model is served, and it is the pinned file")
ok("max-age" in (hdr.get("Cache-Control") or ""), "and the browser may keep it")
code, body, _ = fetch("head/asset/vision_wasm_internal.wasm")
ok(code == 200 and body[:4] == b"\0asm", "the model's engine is served with its real content")
ok(fetch("head/asset/nothing.bin")[0] == 404 and fetch("head/asset/..%2f..%2fprefs.json")[0] == 404, "only the known files are served")
tampered = os.path.join(data, "head", "face_landmarker.task")
good = open(tampered, "rb").read(); open(tampered, "wb").write(b"not the model")
code, body, _ = fetch("head/asset/face_landmarker.task")
ok(code == 200 and hashlib.sha256(body).hexdigest() == "64184e229b263107bc2b804c6625db1341ff2bb731874b0bcc2fe6544e0bc9ff", "a damaged local copy is replaced by a verified download")
r = json.loads(fetch("api/head/probe")[1])
ok(r.get("ok") and r.get("cam") == "chase", "the helper can ask the game what its camera is doing")

# ---- the page
B = Browser(1334, 800, camera="fake")
B.__enter__()
B.goto(BASE + "#head"); B.wait(3)
ok(B.js("document.getElementById('head').classList.contains('on')") is True, "the Head page opens from its link, in Bus mode (it is not part of Freight)")
ok(B.js("document.querySelectorAll('.hslide').length") == 12, "all twelve sensitivity sliders are there")
ok(B.js("document.querySelectorAll('#hPresets button').length") == 3, "with three presets")
c = B.center("#hFindCams"); B.press(c[0], c[1], 0.06)
ok(wait(lambda: (B.js("document.getElementById('hCam').options.length") or 0) >= 1 and "Camera" not in (B.js("document.getElementById('hCam').selectedOptions[0].textContent") or "Camera"), 10, "the camera names"),
   f"cameras are listed by name ({B.js('document.getElementById(\"hCam\").selectedOptions[0].textContent')})")
ok(wait(lambda: "listening" in (B.js("document.getElementById('hGame').textContent") or ""), 8, "the game line"), f"the page says the game is listening ({B.js('document.getElementById(\"hGame\").textContent')})")
if out:
    os.makedirs(out, exist_ok=True); B.shot(os.path.join(out, "head_page.png"))

# ---- the sensitivity, in the real game
def peak_during(secs):
    ys, end = [], time.time() + secs
    while time.time() < end:
        r = HP.probe()
        if r:
            ys.append(r["cur"]["yaw"])
        time.sleep(0.1)
    return (max(ys) if ys else 0), (min(ys) if ys else 0)

B.js("headTracker.set('yaw.gain', 2); headTracker.set('curve', 0.6); headTracker.set('deadzone', 1)")
c = B.center("#hSweep"); B.press(c[0], c[1], 0.06)
hi, lo = peak_during(9)
ok(hi > 60 and lo < -60, f"a pretend head sweeping through the filters turns the real view both ways: {lo:+.0f} to {hi:+.0f} degrees")
B.js("headTracker.set('yaw.gain', 0.5)")
time.sleep(1.0)
hi2, lo2 = peak_during(9)
ok(0 < hi2 < hi / 2.5 and lo2 > lo / 2.5, f"turning the gain down to 0.5 shrinks the sweep to {lo2:+.0f} to {hi2:+.0f} (it was {lo:+.0f} to {hi:+.0f})")
c = B.center("#hSweep"); B.press(c[0], c[1], 0.06)
time.sleep(2.5)
r = HP.probe()
ok(r and abs(r["cur"]["yaw"]) < 3 and abs(r["cur"]["pitch"]) < 3, f"stopping the sweep lets the view ease back to the centre ({r['cur']['yaw']:+.1f})")
if out:
    B.shot(os.path.join(out, "head_page_tuned.png"))

# ---- the face tracker (a made-up camera has no face, so this checks everything up to finding one)
B.js("headTracker.set('yaw.gain', 2)")
c = B.center("#hStart"); B.press(c[0], c[1], 0.06)
started = wait(lambda: B.js("headTracker.isRunning()") is True, 120, "the tracker to start (the model loads)")
status = B.js("document.getElementById('hStatus').textContent")
ok(started and "Could not start" not in status, f"the camera and the face model start: {status!r}")
time.sleep(4)
st = B.js("JSON.stringify(headTracker.state().stats)")
s = json.loads(st) if st else {}
ok(s.get("frames", 0) > 0 or s.get("ms", 0) > 0, f"frames are going through the face model ({s.get('ms', 0):.0f} ms each on this software renderer)")
ok("No face" in (B.js("document.getElementById('hStatus').textContent") or ""), "and a picture with no face says so")
ok(B.js("document.body.classList.contains('headon')") is True and B.js("getComputedStyle(document.getElementById('headCenterBtn')).display") != "none"
   and B.js("document.getElementById('headBtn').classList.contains('live')") is True, "the music bar shows tracking is on and offers a recenter button")
c = B.center("#hStart"); B.press(c[0], c[1], 0.06)
ok(wait(lambda: B.js("headTracker.isRunning()") is False, 10, "stop"), "stopping works")
ok("off" in (B.js("document.getElementById('hStatus').textContent") or "").lower(), "and the page says tracking is off")
ok(B.js("getComputedStyle(document.getElementById('headCenterBtn')).display") == "none", "and the recenter button goes away")

# ---- it is reachable from every mode, with the button in the music bar
def open_from(mode):
    post = urllib.request.Request(BASE + "api/prefs", json.dumps({"mode": mode}).encode(), {"Content-Type": "application/json"})
    urllib.request.urlopen(post, timeout=10).read()
    B.goto("about:blank"); B.goto(BASE); B.wait(2.5)
    shown_before = B.js("document.getElementById('head').classList.contains('on')")
    c = B.center("#headBtn"); B.press(c[0], c[1], 0.06); B.wait(0.8)
    opened = B.js("document.getElementById('head').classList.contains('on')")
    c = B.center("#hDone"); B.press(c[0], c[1], 0.06); B.wait(0.8)
    closed = not B.js("document.getElementById('head').classList.contains('on')")
    return (not shown_before) and opened and closed
for mode in ("bus", "rides", "freight"):
    ok(open_from(mode), f"in {mode} mode the music bar button opens the Head page and Done closes it")
errs = [e for e in B.errors if "favicon" not in e]
ok(not errs, f"no JavaScript errors ({errs[:2]})")
B.__exit__()
print("\nall head tracking page tests passed" if not bad else f"\n{len(bad)} FAILED")
sys.exit(1 if bad else 0)
