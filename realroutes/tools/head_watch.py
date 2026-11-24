"""Watches head tracking without driving it: python3 tools/head_watch.py [seconds] [profile text]
Run it while the BeamDeck window is tracking your head. Every second it reports the poses a second the helper sends the game, what the
game's head pose is (and its camera), the integrated GPU's busy percentage (amdgpu's own counter), and the browser's processor use; at
the end it says whether the browser touched the NVIDIA card. Numbers only: nothing from the camera is read or saved."""
import glob, json, subprocess, sys, time, urllib.request

secs = int(sys.argv[1]) if len(sys.argv) > 1 else 30
profile = sys.argv[2] if len(sys.argv) > 2 else "beamdeck/browser-profile"
URL = "http://127.0.0.1:47130/routes/api/head/probe"


def sh(cmd):
    return subprocess.run(cmd, shell=True, capture_output=True, text=True).stdout


def amd_busy():
    for f in glob.glob("/sys/class/drm/card*/device/gpu_busy_percent"):
        try:
            return int(open(f).read())
        except (OSError, ValueError):
            pass
    return None


def browser_pids():
    return [int(x) for x in sh(f"pgrep -f 'user-data-dir=.*{profile}'").split()]


def probe():
    try:
        return json.loads(urllib.request.urlopen(URL, timeout=2).read())
    except Exception:
        return {}


rows, nv_seen = [], set()
print(f"watching for {secs} s (browser profile: ...{profile})")
for i in range(secs):
    time.sleep(1)
    p = probe()
    pids = browser_pids()
    cpu = sum(float(x) for x in sh(f"ps -o pcpu= -p {','.join(map(str, pids))}").split()) if pids else 0.0
    nv = sh("nvidia-smi pmon -c 1 2>/dev/null")
    for l in nv.splitlines():
        parts = l.split()
        if parts and parts[0].isdigit() and int(parts[0]) in pids:
            nv_seen.add(l.strip())
    rows.append((p.get("sent_per_s", 0) or 0, amd_busy(), cpu, p.get("firstPerson"), p.get("cam")))
    cur = p.get("cur", {})
    print(f"  {i + 1:2d}s  sending {p.get('sent_per_s', 0):>4} poses/s  game head yaw {cur.get('yaw', 0):+6.1f} pitch {cur.get('pitch', 0):+6.1f}  "
          f"cam {p.get('cam')} (first person: {p.get('firstPerson')})  iGPU {rows[-1][1]}%  browser CPU {cpu:.0f}%")
n = max(1, len(rows))
print("\nsummary")
print(f"  poses sent a second: {sum(r[0] for r in rows) / n:.1f}")
print(f"  integrated GPU busy: {sum(r[1] or 0 for r in rows) / n:.0f}% on average (peak {max((r[1] or 0) for r in rows)}%)")
print(f"  browser processor: {sum(r[2] for r in rows) / n:.0f}% of one core on average (peak {max(r[2] for r in rows):.0f}%)  [{len(browser_pids())} browser processes]")
print("  browser processes on the NVIDIA card:", sorted(nv_seen) if nv_seen else "none")
print("  NVIDIA card now:", sh("nvidia-smi --query-gpu=utilization.gpu,memory.used --format=csv,noheader").strip())
