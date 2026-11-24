"""Freight multiplayer: two Freight instances against a real hub (relay/freight-hub.py) run in this process.
   python3 tools/test_mp.py"""
import importlib.util, os, sys, tempfile, threading, time
here = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(here, ".."))
import freight as F, freight_mp as M

spec = importlib.util.spec_from_file_location("hub", os.path.join(here, "..", "..", "relay", "freight-hub.py"))
hub = importlib.util.module_from_spec(spec)
os.environ["BEAMDECK_HUB_DATA"] = tempfile.mkdtemp()
spec.loader.exec_module(hub)
srv = hub.ThreadingHTTPServer(("127.0.0.1", 0), hub.Handler); srv.daemon_threads = True
threading.Thread(target=srv.serve_forever, daemon=True).start()
URL = f"127.0.0.1:{srv.server_address[1]}"
bad = []
def ok(c, m):
    print(("PASS " if c else "FAIL ") + m)
    if not c: bad.append(m)

models = [{"model": "percheron", "brand": "C", "name": "Percheron", "type": "Truck",
           "configs": [{"key": "xl500_6x4_fifthwheel_m", "name": "x", "specs": {}}, {"key": "s310_4x2_flatbed_M", "name": "f", "specs": {}}]}]

def driver(name, hubaddr=URL, on=True):
    sent = []
    fr = F.Freight(lambda c: sent.extend(c), tempfile.mkdtemp())
    fr._sent = sent
    for i, md in enumerate(models):
        fr.on_vehicles({"type": "vehicles", "first": i == 0, "last": i == len(models) - 1, "model": md})
    fr.select_truck("percheron", "xl500_6x4_fifthwheel_m")
    fr.on_hello({"type": "hello", "rr": F.MOD_REQUIRED, "player": "GameNick"})
    fr.update({"type": "pose", "lat": 45.4215, "lon": -75.6972, "speed": 0, "gx": 0, "gy": 0, "heading": 90})
    prefs = {"mpOn": on, "mpHub": hubaddr, "mpName": name}
    mp = M.MP(fr, lambda: prefs, tempfile.mkdtemp())
    fr.mp = mp
    return fr, mp, prefs

# --- the hub address and the name
ok(M.normalize_hub("100.64.1.2") == "http://100.64.1.2:47141", "a bare address gets the hub port")
ok(M.normalize_hub("ws://100.64.1.2:47140") == "http://100.64.1.2:47141", "the music relay's address points at the hub next to it")
ok(M.normalize_hub("http://h:9000/") == "http://h:9000", "an explicit port is kept")
ok(M.normalize_hub("") == "", "no address stays empty")

a, mpa, pa = driver("Eagle")
b, mpb, pb = driver("Eagle")
ok(mpa.sync() is None and mpb.sync() is None, "both drivers reach the hub")
mpa.sync()
ok(sorted(p["name"] for p in mpa.view()["players"]) == ["Eagle", "Eagle 2"], "two drivers with one name are told apart")
ok(any(p["me"] for p in mpa.view()["players"]), "each driver can tell which one is them")
c, _, pc = driver("")
ok(mpa.my_name() == "Eagle" and M.MP(c, lambda: {}, tempfile.mkdtemp()).my_name() == "GameNick", "with no name set the game's BeamMP nickname is used")

# --- the shared board
ok(mpa.view()["shared"] >= 4, "a driver tops the shared board up with fresh jobs")
mpb.sync()
shared = [o for o in b.board()["offers"] if o.get("shared")]
ok(len(shared) >= 4 and all("fits" in o and "why" in o for o in shared), "the other driver sees them on their board with fit info")
ok(all(isinstance(o["needs"], str) and o["needs"] for o in shared), "and each still says what truck it needs")
mine = [o for o in b.board()["offers"] if not o.get("shared")]
ok(len(mine) > 0, "their own market is still there beside the shared jobs")
fit = next(o for o in shared if o["fits"])

# --- a job from a player on different data does not break the board
mpb.snap["offers"].append({"id": "obad00001", "cargo": "no-such-cargo", "title": "x", "icon": "", "load": "", "kind": "local",
                           "from": {}, "stops": [], "km": 1, "mins": 1, "pay": 1, "weight": 1, "needs": "box"})
mpb.snap["offers"].append({"id": "obad00002", "cargo": fit["cargo"]})
ok(b.board() and all(o["id"] not in ("obad00001", "obad00002") for o in b.board()["offers"]), "jobs with an unknown cargo or missing fields are left out")

# --- convoy
ok_, r = a.act({"action": "mp_create", "offer": fit["id"]})
ok(ok_ and len(r["code"]) == 4 and a.state == "resolving" and a.job["id"] == fit["id"], f"a driver opens a convoy on a shared job and takes it ({r})")
ok(not a.act({"action": "mp_create", "offer": fit["id"]})[0], "a second convoy while busy is refused")
mpb.sync()
cv = next(c for c in mpb.view()["convoys"] if c["code"] == r["code"])
ok(cv["host"] == "Eagle" and cv["title"] and cv["to"], "the other driver sees the convoy on the list")
ok_, rr = b.act({"action": "mp_join", "code": r["code"].lower()})
ok(ok_ and b.state == "resolving" and b.job["id"] == fit["id"], "a friend joins by code and takes the same job")
for _ in range(2):
    mpa.sync(); mpb.sync()
ok(mpa.convoy_size_on(fit["id"]) == 2 and mpb.convoy_size_on(fit["id"]) == 2, "both see two drivers of the convoy on the job")
for who in (a, b):
    who.job["started"] = time.time()
sc = a._score(a.job)
ok(sc["convoy_n"] == 2 and sc["convoy_f"] == 1.05, f"hauling together pays {int((sc['convoy_f']-1)*100)}% more")
ok(abs(sc["pay"] - fit["pay"] * sc["cond_f"] * sc["time_f"] * 1.05) <= 1, "the bonus reaches the pay")
mpb.convoy_leave(); mpb.sync(); mpa.sync()
ok(a._score(a.job)["convoy_n"] == 0 or a._score(a.job)["convoy_f"] == 1.0, "no bonus once alone")
ok_, rr = b.act({"action": "mp_join", "code": "ZZZZ"})
ok(not ok_ and "convoy" in rr.lower(), f"a wrong code says so ({rr})")

# --- chat and the leaderboard
ok(a.act({"action": "mp_chat", "text": "on my way"})[0], "chat goes out")
time.sleep(0.2); mpb.sync()
ok(any(m["text"] == "on my way" for m in mpb.view()["chat"]), "and reaches the other driver")
a.job["dist_m"] = 90000; a.job["cond"] = 100.0
a.job["offer"]["cargo"] = "logs"                                          # a delivery that leaves something behind
a._finish()
mpa.flush()
mpb.sync()
ok(any("delivered" in m["text"] for m in mpb.view()["chat"]), "finishing a job announces the delivery on the radio")
chain_jobs = [o for o in b.board()["offers"] if o.get("chain")]
ok(chain_jobs and chain_jobs[0]["chain"]["from"] == "Eagle" and chain_jobs[0]["chain"]["step"] == 2, "the delivery's follow-up job reaches the other driver's board as a supply chain job")
ok(any("is ready there" in m["text"] for m in mpb.view()["chat"]), "and the radio says what was left behind")
ok("leaderboard" not in mpb.view(), "and there is no leaderboard")

# --- a job a driver takes goes on the shared board, so a friend can take the same one
c3, mpc3, _ = driver("Pal"); c3.mp.sync()
c3b, mpc3b, _ = driver("Mate"); mpc3b.sync()
j3 = next(o for o in c3.board()["offers"] if not o.get("shared") and o["fits"])
c3.start(j3["id"]); mpc3.flush(); mpc3b.sync()
ok(j3["id"] in {o["id"] for o in c3b.board()["offers"] if o.get("shared")}, "a job one driver takes appears on the other driver's board")
ok(c3b.start(j3["id"])[0] and c3b.job["id"] == j3["id"], "and the other driver can take the same job")

# --- a route to a friend, to meet up
mpc3b.sync(); mpc3.sync()
ok(not c3b.act({"action": "mp_route", "name": "Pal"})[0], "no route to a friend while on a job (the GPS follows the job)")
c3b.cancel()
c3b._sent.clear()
ok(c3b.act({"action": "mp_route", "name": "Pal"})[0] and any(c.get("cmd") == "gpsTo" and abs(c["lat"] - 45.4215) < 1e-6 for c in c3b._sent), "a driver can route to a friend's position")
ok(not c3b.act({"action": "mp_route", "name": "Nobody"})[0], "an unknown driver is refused")
c3b._sent.clear(); c3b.act({"action": "mp_route", "clear": True})
ok(any(c.get("cmd") == "gpsClear" for c in c3b._sent), "and the route can be cleared")

# --- the job shown to other drivers while driving
a2, mp2, _ = driver("Third")
a2.start(next(o for o in a2.board()["offers"] if o["fits"])["id"])
a2.job["dist_m"] = 30000
js = a2.mp_state()["job"]
ok(js["phase"] == "resolving" and js["offer"] and js["to"] and js["pct"] == 0, "a driver on the way to the pickup shows as 0% done")
a2.state = "driving"
js = a2.mp_state()["job"]
ok(0 < js["pct"] < 100, f"a driver on the road shows their progress ({js['pct']}%)")
a.pose["_at"] = time.time()
st = a.mp_state()
ok(st["truck"]["name"].endswith("Percheron") and abs(st["lat"] - 45.4215) < 1e-6, "presence carries the truck and position")

# --- switched off, and a hub that is down
pb["mpOn"] = False
ok(not mpb.view()["on"] and mpb.offers() == [] and not [o for o in b.board()["offers"] if o.get("shared")], "with multiplayer off there is no shared board and no calls")
ok(not b.act({"action": "mp_chat", "text": "hi"})[0], "and chat says to turn it on")
d, mpd, pd = driver("Down", hubaddr="127.0.0.1:1")
try:
    mpd.sync(); ok(False, "an unreachable hub raises")
except OSError as e:
    ok("reachable" in str(e), f"an unreachable hub gives a plain message ({e})")
ok(not mpd.active() and d.board()["offers"], "and the player's own board still works")
d2, mp_nohub, _ = driver("N", hubaddr="")
try:
    mp_nohub.sync(); ok(False, "no hub address raises")
except OSError as e:
    ok("hub address" in str(e), "a missing hub address says where to set it")

print("\nall multiplayer tests passed" if not bad else f"\n{len(bad)} FAILED")
sys.exit(1 if bad else 0)
