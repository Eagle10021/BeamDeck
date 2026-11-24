"""Quick design screenshots of the Freight console with a fake game and a real hub (about a minute, not the full UI test).
   python3 tools/design_shots.py OUTDIR [width height]
Takes: a_board, b_multi, c_garage, d_history, then joins a friend's convoy with a real click and takes e_convoy (the cockpit)."""
import importlib.util, json, os, sys, tempfile, threading, time, urllib.request
HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import fake_game as FG
from cdp_shot import Browser

out = sys.argv[1]
W, H = (int(sys.argv[2]), int(sys.argv[3])) if len(sys.argv) > 3 else (1334, 800)
os.makedirs(out, exist_ok=True)
BASE = f"http://127.0.0.1:{FG.H.HTTP_PORT}/"
h, g, srv = FG.start()
get = lambda p: json.loads(urllib.request.urlopen(BASE + p, timeout=10).read())
post = lambda p, b: json.loads(urllib.request.urlopen(urllib.request.Request(BASE + p, json.dumps(b).encode(), {"Content-Type": "application/json"}), timeout=10).read())
for _ in range(100):
    if get("api/state")["game"]["connected"] and len(get("api/freight/board")["garage"]) > 8:
        break
    time.sleep(0.15)
post("api/prefs", {"mode": "freight"})
post("api/freight", {"action": "truck", "model": "percheron", "config": "xl500_6x4_fifthwheel_m"})

spec = importlib.util.spec_from_file_location("hub", os.path.join(HERE, "..", "..", "relay", "freight-hub.py"))
hub = importlib.util.module_from_spec(spec); os.environ["BEAMDECK_HUB_DATA"] = tempfile.mkdtemp(); spec.loader.exec_module(hub)
hs = hub.ThreadingHTTPServer(("127.0.0.1", 0), hub.Handler); hs.daemon_threads = True
threading.Thread(target=hs.serve_forever, daemon=True).start()
HUB = f"127.0.0.1:{hs.server_address[1]}"
hp = lambda path, b: json.loads(urllib.request.urlopen(urllib.request.Request(f"http://{HUB}{path}", json.dumps(b).encode(), {"Content-Type": "application/json"}), timeout=10).read())
post("api/prefs", {"mpHub": HUB, "mpName": "Eagle"})
post("api/prefs", {"mpOn": True})
for _ in range(100):
    st = get("api/state")["mp"]
    if st.get("connected") and st["shared"] >= 4:
        break
    time.sleep(0.2)
pick = next(o for o in get("api/freight/board")["offers"] if o.get("shared") and o["fits"])
FR = (("f1111111-1111", "Skynet", "Gavril T-Series", {"title": pick["title"], "phase": "driving", "pct": 62, "offer": pick["id"], "to": pick["stops"][-1]["name"]}),
      ("f2222222-2222", "Balony", "Cherrier Percheron", {}))


def ping():                                          # the friends stay online while the shots are taken
    for pid, name, truck, job in FR:
        hp("/api/presence", {"pid": pid, "name": name, "lat": 45.2, "lon": -75.6, "kmh": 74 if job else 0, "truck": {"name": truck}, "job": job})


ping()
threading.Thread(target=lambda: [(ping(), time.sleep(5)) for _ in range(80)], daemon=True).start()
hp("/api/convoy", {"pid": "f1111111-1111", "action": "create", "offer": pick})
for who, txt in (("f1111111-1111", "bring snacks"), ("f2222222-2222", "401 is clear past Cobourg")):
    hp("/api/chat", {"pid": who, "text": txt}); time.sleep(1.1)
hp("/api/done", {"pid": "f1111111-1111", "pay": 5200, "km": 430, "stars": 4.8, "title": "Steel"})
time.sleep(2.5)

B = Browser(W, H); B.__enter__()
for name, hsh, wait in (("a_board", "#freight", 5), ("b_multi", "#freight/multi", 4), ("c_garage", "#freight/garage", 4), ("d_history", "#freight/history", 4)):
    B.goto("about:blank"); B.goto(BASE + hsh); B.wait(wait)
    B.shot(os.path.join(out, name + ".png"))
    print("shot", name, "| js errors:", B.errors[-3:]); B.errors.clear()
B.goto("about:blank"); B.goto(BASE + "#freight/multi"); B.wait(3.5)
c = B.center("[data-join]")
if c:
    B.press(c[0], c[1], 0.06)
    for _ in range(40):
        if get("api/state")["freight"].get("job"):
            break
        time.sleep(0.25)
    B.wait(3)
    print("joined; job:", (get("api/state")["freight"].get("job") or {}).get("title"), "| convoy:", get("api/state")["mp"]["convoy"])
    B.shot(os.path.join(out, "e_convoy.png"))
B.__exit__()
