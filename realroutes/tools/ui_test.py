"""End-to-end UI test of Freight: python3 tools/ui_test.py OUTDIR  (needs microsoft-edge-stable for the screenshots)"""
import json, os, subprocess, sys, time, urllib.request
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import fake_game as FG
from cdp_shot import Browser

out = sys.argv[1] if len(sys.argv) > 1 else "/tmp/freight-ui"
os.makedirs(out, exist_ok=True)
BASE = f"http://127.0.0.1:{FG.H.HTTP_PORT}/"
h, g, srv = FG.start()
ok = lambda c, m: print(("PASS " if c else "FAIL ") + m) or (c or (_ for _ in ()).throw(SystemExit(1)))


def get(p):
    return json.loads(urllib.request.urlopen(BASE + p, timeout=10).read())


def post(p, body):
    r = urllib.request.Request(BASE + p, json.dumps(body).encode(), {"Content-Type": "application/json"})
    return json.loads(urllib.request.urlopen(r, timeout=10).read())


def wait(cond, secs=15, what=""):
    t = time.time()
    while time.time() - t < secs:
        if cond():
            return True
        time.sleep(0.15)
    print("   (timed out waiting for", what, ")")
    return False


B = Browser(1334, 800)
B.__enter__()


def shot(name, hash_="", wait_s=4):
    path = os.path.join(out, name + ".png")
    B.goto("about:blank"); B.goto(BASE + hash_); B.wait(wait_s)
    B.shot(path)
    print("   shot", path)
    if B.errors:
        print("   JS ERRORS:", B.errors[-5:]); B.errors.clear()


fr = lambda: get("api/state")["freight"]
ok(wait(lambda: get("api/state")["game"]["connected"], 15, "the fake game"), "fake game connected")
ok(wait(lambda: len(get("api/freight/board")["garage"]) > 8, 15, "truck list"), "the game's truck list reached the garage")
post("api/prefs", {"mode": "freight"})

shot("1_board_no_truck")
# the player taps their own saved config in the Garage (no API shortcut): the truck must be selected and the board must fill with jobs it can do
B.goto("about:blank"); B.goto(BASE + "#freight/garage"); B.wait(3.5)
c = B.center('.gcfg[data-k="ruck"]')
ok(c is not None, "the custom Ruck config is listed in the Garage")
B.press(c[0], c[1], 0.06); B.wait(1.2)
ok((fr()["truck"] or {}).get("config") == "ruck", "tapping the Ruck selects it")
bb = get("api/freight/board")
fitting = [o for o in bb["offers"] if o["fits"]]
ok(set(bb["caps"]) == {"drawbar", "flatbed"}, f"the Ruck is a flatbed with an eye hitch ({bb['caps']})")
ok(len(fitting) >= len(bb["offers"]) * 0.5, f"the board was rebuilt for it: {len(fitting)} of {len(bb['offers'])} jobs fit")
print("   fitting jobs:", sorted({o["title"] for o in fitting}))
shot("2a_board_ruck")
# the planner: Ottawa → Toronto with a semi cargo says what it needs and which of the player's trucks can do it
B.goto("about:blank"); B.goto(BASE + "#freight/plan"); B.wait(3.5)
B.js("(() => { const s = document.querySelector('#pCargo'); s.value = 'dryvan'; s.dispatchEvent(new Event('change')); })()"); B.wait(0.5)
c = B.center("#pGo"); B.press(c[0], c[1], 0.05); B.wait(1.2)
ok(B.js("!!document.querySelector('#pResult .swap')"), "the planner says which of the player's trucks can do a job the Ruck can't")
txt = B.js("document.querySelector('#pResult .fneed').innerText")
print("   planner says:", txt)
ok("semi tractor" in txt.lower(), "...and what the job needs")
B.shot(os.path.join(out, "4b_plan_needs.png"))
c = B.center("#pResult .swap"); B.press(c[0], c[1], 0.05); B.wait(1.8)
ok((fr()["truck"] or {}).get("config") != "ruck", "one tap switches to a truck that can do it")
ok(B.js("!!document.querySelector('#pResult .take') && !document.querySelector('#pResult .take').disabled"), "and the planned job is now ready to take")
B.shot(os.path.join(out, "4c_plan_switched.png"))
B.goto("about:blank"); B.goto(BASE + "#freight/garage"); B.wait(3)
c = B.center('.gcfg[data-k="ruck"]'); B.press(c[0], c[1], 0.06); B.wait(1.0)     # back to the Ruck for the rest of the test
b = get("api/freight/board")
perch = next(t for t in b["garage"] if t["model"] == "percheron" and "fifthwheel" in t["key"])
ok(post("api/freight", {"action": "truck", "model": perch["model"], "config": perch["key"]})["ok"], "pick the Percheron tractor")
post("api/freight", {"action": "market", "hub": "Ottawa", "refresh": True})
shot("2_board_percheron")
shot("3_garage", "#freight/garage")
shot("4_plan", "#freight/plan")

# --- take the Corridor run and drive it
b = get("api/freight/board")
offer = next(o for o in b["offers"] if o["scenario"] == "corridor_run")
r = post("api/freight", {"action": "start", "offer": offer["id"]})
ok(r["ok"], "take the corridor run")
ok(wait(lambda: fr()["state"] == "driving", 20, "the rig"), "the fake mod spawned the rig and the job is driving")
sp = [c for c in g.log if c.get("cmd") == "freightSpawn"][-1]
ok(sp["trailers"][0]["model"] == "eu_trailers" and sp["truck"]["config"] == perch["key"], "spawn asked for the Percheron + a European trailer")
g.speed_goal = 24.0
ok(wait(lambda: fr()["tel"].get("speed", 0) > 40, 25, "speed"), "truck is moving; live telemetry is flowing")
# truck controls
for n in ("retarder_up", "retarder_up", "jake_toggle", "diff_rear_lock", "cruise_on"):
    post("api/truck", {"name": n})
post("api/truck", {"name": "pto", "rpm": 1500})
time.sleep(1.2)
ok(g.ret == 2 and g.cbOn and g.dm["r"] == "locked" and g.cc and g.ptoRpm == 1500, "retarder, engine brake, diff lock, cruise and PTO commands reached the game")
t = fr()["tel"]
ok(t["ret"] == 3 and t["retS"] == 0.4 and t["cbOn"] and t["dm"]["r"] == "locked" and t["pto"] == 1500, "and their state came back to the console")
shot("5_cockpit")
shot("6_pto_slider", "#freight/pto")
post("api/truck", {"name": "pto", "rpm": 0})
time.sleep(0.5)
ok(g.ptoRpm is None, "PTO low (rpm 0) releases the engine speed")

# --- the PTO button, with a real mouse: tap = high / low, hold = the slider
B.goto("about:blank"); B.goto(BASE); B.wait(3.5)
c = B.center("#ptoBtn")
ok(c is not None, "the PTO button is on the cockpit")
B.press(c[0], c[1], 0.08); B.wait(0.8)
ok(g.ptoRpm and 1000 <= g.ptoRpm <= 2000, f"a tap sets the PTO engine speed high ({g.ptoRpm} rpm)")
B.wait(1.0)
B.press(c[0], c[1], 0.08); B.wait(0.8)
ok(g.ptoRpm is None, "a second tap sets it back to low")
ok(not B.js("document.querySelector('#fPtoPop').classList.contains('on')"), "a tap does not open the slider")
B.drag(c[0], c[1], c[0] + 360, c[1], hold=0.6)                                   # hold, then slide the same finger
B.wait(0.5)
ok(B.js("document.querySelector('#fPtoPop').classList.contains('on')"), "holding opens the slider")
ok(g.ptoRpm and g.ptoRpm >= 1800, f"sliding while holding changes the rpm ({g.ptoRpm})")
B.shot(os.path.join(out, "6b_pto_held.png"))
tr = B.center("#ptoTrack")                                                        # measured now it is open
B.press(tr[2] + 12, tr[1], 0.05); B.wait(0.6)                                      # then tap the track near its left end
ok(g.ptoRpm and g.ptoRpm <= 1000, f"tapping the slider track sets the rpm ({g.ptoRpm})")
B.shot(os.path.join(out, "6c_pto_low.png"))
if B.errors: print("   JS ERRORS:", B.errors); B.errors.clear()
post("api/truck", {"name": "pto", "rpm": 0}); B.wait(0.4)

# --- deliver every drop
job = fr()["job"]
for k in range(len(job["stops"])):
    stop = fr()["job"]["stop"]
    g.lat, g.lon, g.speed, g.speed_goal = stop["lat"] + 0.0002, stop["lon"], 0.0, 0.0
    ok(wait(lambda: fr()["state"] in ("done", "driving") and (fr()["state"] == "done" or fr()["job"]["idx"] > k + 1), 10, "delivery"), f"drop {k + 1}/{len(job['stops'])} delivered")
ok(wait(lambda: fr()["state"] == "done", 10, "done"), "job complete")
shot("7_summary")
post("api/freight", {"action": "dismiss"})
shot("8_history", "#freight/history")
shot("9_board_after")
B.__exit__()
print("\nscreenshots in", out)
