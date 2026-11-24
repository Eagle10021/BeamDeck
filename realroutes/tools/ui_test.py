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
ok(len(fitting) >= len(bb["offers"]) * 0.4, f"the board was rebuilt for it: {len(fitting)} of {len(bb['offers'])} jobs fit")
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
ok(wait(lambda: fr()["state"] == "to_pickup", 20, "the truck"), "the fake mod spawned the truck and the job asks for a drive to the pickup")
sp = [c for c in g.log if c.get("cmd") == "freightSpawn"][-1]
ok(sp["trailers"][0]["model"] == "eu_trailers" and sp["truck"]["config"] == perch["key"], "spawn asked for the Percheron + a European trailer, to be hooked on at the pickup")
ok(not [c for c in g.log if c.get("cmd") == "freightLoad"], "no trailer or load before the pickup")
shot("4d_to_pickup")
pk = fr()["job"]["pickup"]
ok(pk["dist_km"] is not None and pk["dist_km"] > 1, f"the pickup is {pk['dist_km']} km away")
g.lat, g.lon, g.speed, g.speed_goal = pk["lat"] + 0.0001, pk["lon"], 0.0, 0.0                   # arrive and stop
ok(wait(lambda: fr()["state"] == "loading", 10, "loading"), "stopping at the pickup starts loading")
shot("4e_loading", wait_s=2.5)
ok(wait(lambda: fr()["state"] == "driving", 40, "the load"), "the load is on and the drive begins")
ok([c for c in g.log if c.get("cmd") == "freightLoad"], "the game was asked to hook the trailer on at the pickup")
g.speed_goal = 24.0
ok(wait(lambda: fr()["tel"].get("speed", 0) > 40, 25, "speed"), "truck is moving; live telemetry is flowing")
# truck controls
for n in ("retarder_up", "retarder_up", "jake_toggle", "diff_rear_lock", "cruise_on"):
    post("api/truck", {"name": n})
post("api/truck", {"name": "pto", "rpm": 1500})
time.sleep(1.2)
ok(g.ret == 2 and g.cbOn and g.dm["r"] == "locked" and g.cc and g.ptoRpm == 1500, "retarder, engine brake, diff lock, cruise and PTO commands reached the game")
t = fr()["tel"]
ok(t["retL"] == "40%" and t["cbOn"] and t["dm"]["r"] == "locked" and t["pto"] == 1500, "and their state came back to the console")
shot("5_cockpit")
B.goto("about:blank"); B.goto(BASE); B.wait(3.5)
h_open = B.js("document.querySelector('#fStrip').getBoundingClientRect().height")
cock_open = B.js("document.querySelector('#fDrive .cockpit').getBoundingClientRect().height")
c = B.center("#fStripToggle"); B.press(c[0], c[1], 0.05); B.wait(1.0)
h_shut = B.js("document.querySelector('#fStrip').getBoundingClientRect().height")
cock_shut = B.js("document.querySelector('#fDrive .cockpit').getBoundingClientRect().height")
ok(h_shut < h_open * 0.5 and cock_shut > cock_open + 60, f"collapsing the job card gives the controls more room (card {h_open:.0f} → {h_shut:.0f} px, controls {cock_open:.0f} → {cock_shut:.0f} px)")
ok("left" in B.js("document.querySelector('#fStrip').innerText") and "cargo" in B.js("document.querySelector('#fStrip').innerText"), "the collapsed line still shows time left, distance and cargo")
B.shot(os.path.join(out, "5b_cockpit_collapsed.png"))
B.goto("about:blank"); B.goto(BASE); B.wait(3.0)
ok(B.js("document.querySelector('#fStrip').classList.contains('collapsed')"), "the collapsed state is remembered")
c = B.center("#fStripToggle"); B.press(c[0], c[1], 0.05); B.wait(0.8)               # open it again for the rest of the test
ok(not B.js("document.querySelector('#fStrip').classList.contains('collapsed')"), "and it opens again")
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
    ok(wait(lambda: fr()["state"] in ("done", "driving") and (fr()["state"] == "done" or fr()["job"]["idx"] > k + 1), 20, "delivery"), f"drop {k + 1}/{len(job['stops'])} delivered (after unloading)")
ok(wait(lambda: fr()["state"] == "done", 10, "done"), "job complete")
shot("7_summary")
post("api/freight", {"action": "dismiss"})
shot("8_history", "#freight/history")
shot("9_board_after")

# --- multiplayer: the console against a real hub, with a second player driven over HTTP
import importlib.util, tempfile, threading
spec = importlib.util.spec_from_file_location("hub", os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..", "relay", "freight-hub.py"))
hub = importlib.util.module_from_spec(spec); os.environ["BEAMDECK_HUB_DATA"] = tempfile.mkdtemp(); spec.loader.exec_module(hub)
hsrv = hub.ThreadingHTTPServer(("127.0.0.1", 0), hub.Handler); hsrv.daemon_threads = True
threading.Thread(target=hsrv.serve_forever, daemon=True).start()
HUB = f"127.0.0.1:{hsrv.server_address[1]}"


def hubpost(path, body):
    r = urllib.request.Request(f"http://{HUB}{path}", json.dumps(body).encode(), {"Content-Type": "application/json"})
    return json.loads(urllib.request.urlopen(r, timeout=10).read())


shot("10_multi_off", "#freight/multi")
print("   DEBUG status:", repr(B.js("document.getElementById('fMpStatus').textContent")), "| tab on:", B.js("document.querySelector('#fTabs .on') && document.querySelector('#fTabs .on').dataset.v"), "| errors:", B.errors[-3:], "| hash:", B.js("location.hash"), "| mp:", get("api/state").get("mp"))
ok("off" in B.js("document.getElementById('fMpStatus').textContent").lower(), "the Multiplayer tab says it is off until switched on")
ok(B.js("getComputedStyle(document.getElementById('fSharedChip')).display") == "none", "no Shared filter while multiplayer is off")
post("api/prefs", {"mpHub": HUB, "mpName": "Eagle"})                      # the test hub first, so nothing is ever sent elsewhere
c = B.center('#fMpOn button[data-v="1"]')
B.press(c[0], c[1], 0.06); B.wait(0.6)                                  # a real click on On
ok(wait(lambda: get("api/state")["mp"].get("connected"), 15, "the hub"), "switching it on connects to the hub")
good = wait(lambda: get("api/state")["mp"]["shared"] >= 4, 15, "shared jobs")
if not good:
    st = get("api/state"); print("   MP:", {k: st["mp"].get(k) for k in ("connected", "error", "shared")}, "| market hub:", st["freight"].get("hub"), "| hub offers:", len(hub.offers))
ok(good, "the hub's board fills with shared jobs")
board = get("api/freight/board")
shared = [o for o in board["offers"] if o.get("shared")]
ok(len(shared) >= 4, "shared jobs appear on this player's board")
def fitting():
    return next((o for o in get("api/freight/board")["offers"] if o.get("shared") and o["fits"]), None)
ok(wait(lambda: fitting() is not None, 40, "a shared job this truck can carry"), "a shared job this truck can carry is on the board")
pick = fitting()
FID = "ffffffff-9999"
fjob = {}


def friend(**kw):                                   # the friend's BeamDeck: its job is remembered between pings
    if "job" in kw:
        fjob.clear(); fjob.update(kw["job"])
    return hubpost("/api/presence", {"pid": FID, "name": "Skynet", "lat": 45.5, "lon": -75.6, "kmh": 70,
                                     "truck": {"name": "Gavril T-Series", "label": "Tractor"}, "job": fjob} | {k: v for k, v in kw.items() if k != "job"})


friend()
threading.Thread(target=lambda: [(friend(), time.sleep(5)) for _ in range(150)], daemon=True).start()   # a real client pings every 2 s
code = hubpost("/api/convoy", {"pid": FID, "action": "create", "offer": pick})["code"]
hubpost("/api/chat", {"pid": FID, "text": "bring snacks"})
ok(wait(lambda: any(p["name"] == "Skynet" for p in get("api/state")["mp"].get("players", [])), 10, "the friend"), "the friend shows as a driver")
shot("11_multi_on", "#freight/multi", 4)
page = lambda: B.js("document.getElementById('fpMulti').innerText") or ""
good = wait(lambda: all(w in page() for w in ("Skynet", code, "bring snacks")), 15, "the multiplayer page to fill")
if not good:
    print("   page text:", page()[:900].replace("\n", " | "))
    print("   hub offers (id, seconds to expiry):", [(o["id"], round(o["expires"] - time.time()) if o.get("expires") else None) for o in hub.offers.values()])
    print("   hub chat:", [m["text"] for m in hub.chat][-4:], "| helper chat:", [m["text"] for m in get("api/state")["mp"].get("chat", [])][-4:])
ok(good, "the tab lists the friend, the open convoy's code and the chat")
B.goto("about:blank"); B.goto(BASE + "#freight"); B.wait(3.5)
ok(B.js("getComputedStyle(document.getElementById('fSharedChip')).display") != "none", "the Shared filter appears when multiplayer is on")
c = B.center('#fFilters button[data-f="shared"]'); B.press(c[0], c[1], 0.06)
cards = lambda: B.js("document.querySelectorAll('#fJobsList .offer').length") or 0
good = wait(lambda: cards() >= 4, 10, "the shared cards")
if not good: print("   cards:", cards(), "| list:", (B.js("document.getElementById('fJobsList').innerText") or "")[:200].replace("\n", " | "))
ok(good and "Shared" in B.js("document.getElementById('fJobsList').innerText"), "the Shared filter shows the shared jobs, tagged")
ok(B.js("document.querySelectorAll('#fJobsList .convoy').length") >= 1, "fitting jobs offer a Convoy button")
B.goto("about:blank"); B.goto(BASE + "#freight/multi"); B.wait(3.5)
wait(lambda: B.center('[data-join]') is not None, 12, "the Join button")
c = B.center('[data-join]'); ok(c is not None, "the friend's convoy has a Join button")
B.press(c[0], c[1], 0.06)                                               # a real click: joins and takes the same job
ok(wait(lambda: fr()["job"] and fr()["job"]["id"] == pick["id"], 15, "the convoy job"), "joining takes the convoy's job")
ok(get("api/state")["mp"]["convoy"] == code, "and this player is in the convoy")
friend(job={"title": pick["title"], "phase": "driving", "pct": 35, "offer": pick["id"], "to": "Somewhere"})
B.wait(3.0)
ok(wait(lambda: B.js("document.getElementById('fConvoyBox').classList.contains('on')"), 10, "convoy box"), "the convoy box shows over the map while driving")
box = B.js("document.getElementById('fConvoyBox').innerText")
ok(code in box and "Skynet" in box and "35%" in box, f"it lists the drivers and their progress ({box!r})")
B.shot(os.path.join(out, "12_convoy_driving.png"))
post("api/freight", {"action": "cancel"}); B.wait(1.0)
B.goto("about:blank"); B.goto(BASE + "#freight/multi"); B.wait(3.5)
B.js("document.getElementById('fMpText').value = 'on my way'")
c = B.center('#fMpSend'); B.press(c[0], c[1], 0.06)
ok(wait(lambda: any(m["text"] == "on my way" for m in hubpost("/api/presence", {"pid": FID, "name": "Skynet"})["chat"]), 10, "the chat message"), "a chat message typed here reaches the other player")
c = B.center('#fMpLeave'); ok(c is not None and B.js("getComputedStyle(document.getElementById('fMpLeave')).display") != "none", "the convoy page has a Leave button in view")
B.press(c[0], c[1], 0.06)
ok(wait(lambda: get("api/state")["mp"]["convoy"] == "", 10, "leave"), "leaving the convoy works")
post("api/prefs", {"mpOn": False})
ok(wait(lambda: not get("api/state")["mp"]["on"] and not [o for o in get("api/freight/board")["offers"] if o.get("shared")], 10, "multiplayer to switch off"), "switching it off drops the shared jobs")
if B.errors: print("   JS ERRORS:", B.errors[-5:]); B.errors.clear()
B.__exit__()
print("\nscreenshots in", out)
