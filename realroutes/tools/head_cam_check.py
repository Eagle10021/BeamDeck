"""Head tracking with the real webcam, and what it costs: python3 tools/head_cam_check.py [seconds] [fps] [size]
Starts the page in a real (visible) browser window on the real graphics, starts the tracker, and for the given time reports the frames
a second, the time the face model takes, whether a face was found, the load on the integrated GPU (amdgpu's own counter), whether the
browser touches the NVIDIA card at all, and the browser's processor use. Prints numbers only: no picture is saved."""
import glob, json, os, subprocess, sys, tempfile, time, urllib.request
HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import fake_game as FG
from cdp_shot import Browser

secs = int(sys.argv[1]) if len(sys.argv) > 1 else 25
fps = sys.argv[2] if len(sys.argv) > 2 else "30"
size = sys.argv[3] if len(sys.argv) > 3 else "320"
data = os.path.expanduser("~/.cache/beamdeck-headtest")
os.makedirs(data, exist_ok=True)
h, g, srv = FG.start(data)
BASE = f"http://127.0.0.1:{FG.H.HTTP_PORT}/"


def sh(cmd):
    return subprocess.run(cmd, shell=True, capture_output=True, text=True).stdout


def amd_busy():
    for f in glob.glob("/sys/class/drm/card*/device/gpu_busy_percent"):
        try:
            return int(open(f).read())
        except (OSError, ValueError):
            pass
    return None


def browser_pids(profile):
    return [int(x) for x in sh(f"pgrep -f 'user-data-dir={profile}'").split()]


B = Browser(960, 720, camera="real", headless=False, real_gpu=True)
B.__enter__()
prof = B.dir
B.goto(BASE + "#freight/head"); B.wait(3)
B.js(f"localStorage.setItem('rrHead', JSON.stringify({{res: {{size: {size}, fps: {fps}, proc: 'GPU', eco: true, show: false, device: ''}}}}))")
B.goto("about:blank"); B.goto(BASE + "#freight/head"); B.wait(3)
c = B.center("#hFindCams"); B.press(c[0], c[1], 0.06); B.wait(2)
print("camera:", B.js("document.getElementById('hCam').selectedOptions[0] && document.getElementById('hCam').selectedOptions[0].textContent"))
c = B.center("#hStart"); B.press(c[0], c[1], 0.06)
t = time.time()
while time.time() - t < 150 and B.js("headTracker.isRunning()") is not True:
    time.sleep(0.5)
print("started after %.0f s:" % (time.time() - t), B.js("document.getElementById('hStatus').textContent"))
time.sleep(3)
rows = []
for i in range(secs):
    time.sleep(1)
    s = json.loads(B.js("JSON.stringify(headTracker.state().stats)") or "{}")
    pids = browser_pids(prof)
    cpu = sum(float(x) for x in sh(f"ps -o pcpu= -p {','.join(map(str, pids))}").split()) if pids else 0.0
    rows.append((s.get("fps", 0), s.get("ms", 0), s.get("face", False), amd_busy(), cpu))
    if i % 5 == 0:
        print(f"  t+{i:2d}s  {rows[-1][0]:>2} fps  model {rows[-1][1]:5.1f} ms  face={rows[-1][2]}  iGPU {rows[-1][3]}%  browser CPU {rows[-1][4]:.0f}%")
pids = set(browser_pids(prof))
nv = sh("nvidia-smi pmon -c 1 2>/dev/null")
on_nv = [l for l in nv.splitlines() if l.strip() and not l.startswith("#") and l.split()[0].isdigit() and int(l.split()[0]) in pids]
print("\nsummary over", secs, "seconds")
n = len(rows)
print(f"  frames a second: {sum(r[0] for r in rows) / n:.1f}   model time: {sum(r[1] for r in rows) / n:.1f} ms   face found in {100 * sum(1 for r in rows if r[2]) // n}% of the samples")
print(f"  integrated GPU busy: {sum(r[3] or 0 for r in rows) / n:.0f}% on average   browser CPU: {sum(r[4] for r in rows) / n:.0f}% of one core on average (peak {max(r[4] for r in rows):.0f}%)")
print("  browser processes seen on the NVIDIA card:", on_nv if on_nv else "none")
print("  NVIDIA card:", sh("nvidia-smi --query-gpu=utilization.gpu,memory.used --format=csv,noheader").strip())
B.js("headTracker.stop()")
B.__exit__()
