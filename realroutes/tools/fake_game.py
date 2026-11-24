"""A fake BeamNG for testing the Freight console end to end: the real helper and console, on spare ports, with a game that
speaks the mod's protocol. Used by ui_test.py; can also be run alone to poke at the console in a browser:
    python3 tools/fake_game.py          # console on http://127.0.0.1:47252/
"""
import json, math, os, random, socket, sys, tempfile, threading, time
HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, ".."))
import realroutes_helper as H

H.MY_PORT, H.GAME_PORT, H.HTTP_PORT = 47251, 47250, 47252


def model(m, brand, name, keys, typ="Truck", power=420):
    return {"model": m, "brand": brand, "name": name, "type": typ,
            "configs": [{"key": k, "name": k.replace("_", " "), "specs": {"power": power, "torque": power * 4.1, "weight": 8200, "top": 105,
                                                                           "drive": "RWD", "trans": "Automatic (12-speed)", "body": "Cab-Over Truck"}} for k in keys]}


def custom(m, brand, name, key, parts):
    d = model(m, brand, name, [key])
    d["configs"][0].update(source="Custom", parts=parts)
    return d


MODELS = [
    model("percheron", "Cherrier", "Percheron", ["xl500_6x4_fifthwheel_m", "xl450_4x2_fifthwheel_M", "s310_fifthwheel_m", "s310_4x2_flatbed_M",
          "l310_6x4_flatbed_M", "xl500_6x4_box_M", "s410_6x4_tanker_M", "s310_6x6_mixer_A"], power=500),
    model("us_semi", "Gavril", "T-Series", ["t83_longhaul", "t83_sleeper", "tc83_base", "t83s_flatbed", "t83s_dump", "t83s_tanker"], power=450),
    model("md_series", "Gavril", "MD-Series", ["md_70_fifthwheel", "md_60_cargobox", "md_70_dump"], power=280),
    model("dumptruck", "Hirochi", "Dump Truck", ["quarry", "loaded"], typ="Heavy Machinery", power=380),
    model("van", "Gavril", "H-Series", ["deliverytruck", "h15_ext_vanster"], power=190),
    model("pickup", "Gavril", "D-Series", ["d10_4wd_A", "d15_423sport_A"], power=300),
]
# the player's saved configs share the model entry of their truck, like the game's own list
_by = {m["model"]: m for m in MODELS}
_by["percheron"]["configs"] += [c for md in (custom("percheron", "", "", "ruck", ["percheron_trailerhitch", "percheron_upfit_platform_980_flatbed"]),
                                              custom("percheron", "", "", "bof road", ["percheron_upfit_cargobox_clearance_F"])) for c in md["configs"]]
_by["us_semi"]["configs"] += [c for md in (custom("us_semi", "", "", "Long", ["conventional_interior_fifthwheel", "us_semi_fifthwheel_top"]),
                                           custom("us_semi", "", "", "MudRunner", ["cabover_interior_fifthwheel", "us_semi_fifthwheel_top"])) for c in md["configs"]]
NAMES = ["Costco Wholesale", "Canadian Tire", "Loblaws", "Shell", "Petro-Canada", "Home Hardware", "Tim Hortons Depot", "Metro", "Walmart Supercentre",
         "Giant Tiger", "Rona", "Independent Grocer", "Esso", "Sobeys"]


LOADS = {"pipes": "10000kg Steel Pipes", "wood": "8400kg Wood Planks", "marble": "19000kg Marble Block", "boxes": "10x Cargo Box XL Slot",
         "pallets": "20x Pallets with Boxes", "rocks": "15T Rocks & Boulders"}


class FakeGame:
    def __init__(self, helper):
        self.h = helper
        self.rx = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.rx.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.rx.bind(("127.0.0.1", H.GAME_PORT))
        self.tx = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.lat, self.lon, self.heading = 45.4215, -75.6972, 250.0
        self.speed = 0.0                       # m/s
        self.target = None                     # (lat, lon, name)
        self.speed_goal = 0.0
        self.rig = None
        self.fuel = 0.82
        self.route_v = 0
        self.has = {}
        self.dm = {"r": "unlocked", "f": "unlocked", "i": "unlocked", "t": "2hi"}
        self.ret, self.cbOn, self.cc, self.ccT, self.pb, self.pto = 0, False, False, None, 0, None
        self.ret_auto = False
        self.ptoRpm = None
        self.spawn_at = None
        self.pending = None
        self.model = "percheron"
        self.tr = 0
        self.log = []
        self.alive = True
        threading.Thread(target=self.listen, daemon=True).start()
        threading.Thread(target=self.loop, daemon=True).start()

    def send(self, m):
        self.tx.sendto(json.dumps(m).encode(), ("127.0.0.1", H.MY_PORT))

    def listen(self):
        while self.alive:
            try:
                data, _ = self.rx.recvfrom(65536)
            except OSError:
                return
            try:
                self.command(json.loads(data))
            except Exception as e:
                print("fake game: command failed:", e)

    def command(self, c):
        k = c.get("cmd")
        self.log.append(c)
        if k == "vehicles":
            for i, md in enumerate(MODELS):
                self.send({"type": "vehicles", "first": i == 0, "last": i == len(MODELS) - 1, "model": md})
        elif k == "places":
            rnd = random.Random(c["id"])
            out = [{"id": f"p{i}", "name": rnd.choice(NAMES), "category": "shop", "lat": c["lat"] + rnd.uniform(-.006, .006),
                    "lon": c["lon"] + rnd.uniform(-.006, .006), "confidence": 0.9, "open": "open"} for i in range(4)]
            self.send({"type": "places", "id": c["id"], "places": out, "complete": True, "pending": False})
        elif k == "freightSpawn":
            self.pending = c
            self.spawn_at = time.time() + 1.2
            self.send({"type": "freight", "id": c["id"], "event": "stage", "text": "Moving to the pickup (the world has to load)…"})
        elif k == "freightLoad":
            self.load_cmd = c
            self.load_at = time.time() + 1.5
        elif k == "freightClear":
            self.tr = 0
        elif k == "gpsTo":
            self.target = (c["lat"], c["lon"], c["name"])
            self.route_v += 1
            self.send_route()
        elif k == "gpsClear":
            self.target = None
        elif k == "truck":
            n = c["name"]
            if n == "retarder_up": self.ret = min(5, self.ret + 1); self.ret_auto = False
            elif n == "retarder_down": self.ret = max(0, self.ret - 1); self.ret_auto = False
            elif n == "retarder_auto": self.ret_auto = True
            elif n == "retarder_off": self.ret, self.ret_auto = 0, False
            elif n == "jake_toggle": self.cbOn = not self.cbOn
            elif n == "park_on": self.pb = 1
            elif n == "park_off": self.pb = 0
            elif n == "cruise_on": self.cc, self.ccT = True, max(self.speed, 22.0)
            elif n == "cruise_off": self.cc = False
            elif n.startswith("cruise_up") or n.startswith("cruise_down"):
                step = (1.389 if n in ("cruise_up", "cruise_down") else 0.2778) * (1 if "up" in n else -1)
                if self.cc and self.ccT: self.ccT += step
                elif self.speed > 8.3: self.cc, self.ccT = True, self.speed       # the first press engages at the current speed
            elif n.startswith("diff_"):
                _, which, state = n.split("_")
                self.dm[{"rear": "r", "front": "f", "inter": "i"}[which]] = "locked" if state == "lock" else "unlocked"
            elif n.startswith("tc_"): self.dm["t"] = n[3:]
        elif k == "pto":
            r = c.get("rpm") or 0
            self.ptoRpm = int(r) if r else None

    def send_route(self):
        la, lo, name = self.target
        pts = [[self.lat + (la - self.lat) * i / 24, self.lon + (lo - self.lon) * i / 24] for i in range(25)]
        d = math.hypot((la - self.lat) * 110574, (lo - self.lon) * 80000)
        steps = [{"type": "depart", "modifier": "", "name": "Highway 416", "location": {"lat": pts[0][0], "lon": pts[0][1]}, "distance": d * .6,
                  "duration": d / 25 * .6, "offset": 0, "leg": 1},
                 {"type": "turn", "modifier": "slight left", "name": "Highway 401 West", "location": {"lat": pts[15][0], "lon": pts[15][1]},
                  "distance": d * .4, "duration": d / 25 * .4, "offset": d * .6, "leg": 1},
                 {"type": "arrive", "modifier": "", "name": name, "location": {"lat": la, "lon": lo}, "distance": 0, "duration": 0, "offset": d, "leg": 1}]
        self.send({"type": "route", "version": self.route_v, "points": pts, "dist": d, "dur": d / 25, "steps": steps})

    def loop(self):
        t_hello = 0
        while self.alive:
            time.sleep(0.1)
            now = time.time()
            if now - t_hello > 4:
                t_hello = now
                self.send({"type": "hello", "rr": 6, "earth": "1.8.2", "mapPort": None})
            if self.pending and now >= self.spawn_at:
                c, self.pending = self.pending, None
                at = c["at"]
                self.lat, self.lon, self.speed = at["lat"], at["lon"], 0.0
                self.model = c["truck"]["model"]
                self.spawn_cmd = c
                self.tr = 0
                self.has = {"ret": True, "cb": True, "la": self.model == "us_semi", "dr": True, "df": self.model in ("percheron",),
                            "di": True, "tc": self.model == "percheron", "fw": c["needs"] == "tractor", "mix": False, "pto": self.model in ("dumptruck", "us_semi")}
                self.send({"type": "freight", "id": c["id"], "event": "ready"})                    # the truck alone: the load comes at the pickup
            if getattr(self, "load_cmd", None) and now >= self.load_at:
                c, self.load_cmd = self.load_cmd, None
                tr = c["trailers"][0] if c["trailers"] else None
                self.tr = 1 if tr else 0
                self.send({"type": "freight", "id": c["id"], "event": "loaded", "coupled": True, "trailer": tr and tr["config"],
                           "trailerName": tr and f"{tr['model']} {tr['config']}",
                           "loadName": LOADS.get(c.get("kind")), "loadKg": 10000 if c.get("kind") == "pipes" else None,
                           "hitch": "fifthwheel_v2" if c["needs"] == "tractor" else "pintle" if tr else None})
            # drive towards the target
            if self.target and self.speed_goal:
                la, lo, _ = self.target
                dy, dx = (la - self.lat) * 110574, (lo - self.lon) * 80000
                d = math.hypot(dx, dy)
                self.heading = (math.degrees(math.atan2(dx, dy)) + 360) % 360
                sp = min(self.speed_goal, max(0.0, d / 8))
                self.speed += (sp - self.speed) * 0.2
                step = self.speed * 0.1
                if d > 0.5:
                    f = min(1.0, step / d)
                    self.lat += (la - self.lat) * f; self.lon += (lo - self.lon) * f
                if d < 20: self.speed = 0.0
            else:
                self.speed *= 0.9
            self.fuel = max(0.02, self.fuel - self.speed * 0.1 * 1e-6)
            sp = self.speed
            la_t = self.target
            left = math.hypot((la_t[0] - self.lat) * 110574, (la_t[1] - self.lon) * 80000) if la_t else None
            rpm = (self.ptoRpm or 650) if sp < 1 else 1000 + sp * 38
            self.send({"type": "pose", "lat": self.lat, "lon": self.lon, "heading": self.heading, "speed": sp, "limit": 27.7, "ready": True,
                       "gpsLeft": left, "gpsEta": left / 22 if left else None, "gpsName": la_t[2] if la_t else None, "model": self.model,
                       "place": {"street": "Highway 401", "place": "Kingston", "region": "Ontario"}, "earth": "1.8.2",
                       "gx": 0.3, "gy": 0.2, "rpm": rpm + random.uniform(-15, 15), "gear": 9 if sp > 12 else 3 if sp > 1 else "N", "fuel": self.fuel,
                       "fuelL": self.fuel * 450, "fuelCap": 450, "wT": 86 + min(14, sp * 0.1), "oT": 97 + min(20, sp * 0.3), "odo": 482310000 + self.lat * 0,
                       "trip": 183400, "pb": self.pb, "load": 0.2 + min(0.6, sp * .02), "eng": 1, "mass": 31800 if self.tr else 9400, "tr": self.tr,
                       "has": self.has, "dm": self.dm, "ret": 2 + self.ret, "retL": "Auto" if self.ret_auto else "Off" if self.ret == 0 else f"{self.ret * 20}%",
                       "retA": 0.0 if self.ret_auto else self.ret * 0.2, "retAuto": True, "cb": 3, "cbL": "100%", "cbOn": self.cbOn, "cc": self.cc, "ccT": self.ccT, "ptoRpm": self.ptoRpm})

    def stop(self):
        self.alive = False
        self.rx.close()


def start(data_dir=None):
    from http.server import ThreadingHTTPServer
    H.beamdeck_relay_hint = lambda: ""          # tests never read this machine's own BeamDeck config: that would reach the real freight hub
    h = H.Helper(data_dir or tempfile.mkdtemp())
    srv = ThreadingHTTPServer(("127.0.0.1", H.HTTP_PORT), H.make_handler(h))
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return h, FakeGame(h), srv


if __name__ == "__main__":
    h, g, srv = start()
    g.speed_goal = float(os.environ.get("SPEED", "22"))
    print(f"console on http://127.0.0.1:{H.HTTP_PORT}/  (fake game running; Ctrl+C to stop)")
    while True:
        time.sleep(1)
