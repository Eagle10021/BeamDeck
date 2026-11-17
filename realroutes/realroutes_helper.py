#!/usr/bin/env python3
"""Real Routes helper: runs a bus shift on a real route between BeamNG (the mod) and the tablet console.

  game mod  --UDP 127.0.0.1:47151-->  helper   (pose, speed, doors, g-forces at 10 Hz)
  game mod  <--UDP 127.0.0.1:47150--  helper   (sign, stops to show, GPS route, doors ...)
  tablet    <--HTTP :47152-->         helper   (console page, route picker, live state, buttons)

All the shift logic lives here (stops, schedule, passengers, comfort, score) so it can be tested without the
game: `--sim 111-1-4` drives a simulated bus along the route.
Route packs come from bake_routes.py (~/.local/share/realroutes/<agency>/). Standard library only.
"""
import argparse, bisect, ipaddress, json, math, os, random, re, socket, threading, time
from datetime import datetime, date, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

HERE = os.path.dirname(os.path.abspath(__file__))
GAME_PORT, MY_PORT, HTTP_PORT = 47150, 47151, 47152

# Stop and dwell rules
AT_STOP_M = 30          # along the route, either side of the stop
STOPPED_MS = 0.8        # m/s: counts as standing
PASSED_M = 25           # past the stop by this much: the bus has left it
BOARD_S, ALIGHT_S, DOOR_MIN_S = 2.5, 1.5, 3.0
OFF_ROUTE_M = 120
JOLT, THROW = 3.0, 5.5  # m/s² horizontal: uncomfortable / standing passengers thrown


def log(*a):
    print(time.strftime("%H:%M:%S"), *a, flush=True)


def hhmm(sec):
    sec = int(sec) % 86400
    h, m = divmod(sec // 60, 60)
    return f"{(h % 12) or 12}:{m:02d}{'am' if h < 12 else 'pm'}"


# ---------- route data ----------

class Data:
    def __init__(self, root):
        self.root = root
        self.index = json.load(open(os.path.join(root, "index.json")))
        self.cal = json.load(open(os.path.join(root, "calendar.json")))
        self._routes = {}

    def route(self, name):
        if name not in self._routes:
            self._routes[name] = json.load(open(os.path.join(self.root, "routes", f"{name}.json")))
        return self._routes[name]

    def pattern(self, pid):
        r = self.route(pid.split("-")[0])
        return r, next(p for p in r["patterns"] if p["id"] == pid)

    def services_on(self, d):
        ds, wd = d.strftime("%Y%m%d"), d.weekday()
        on = set()
        for sid, c in self.cal.items():
            if (c["days"][wd] == "1" and c["start"] <= ds <= c["end"] and ds not in c["remove"]) or ds in c["add"]:
                on.add(sid)
        return on

    def trips(self, pid, d):
        r, p = self.pattern(pid)
        svc = self.services_on(d)
        return [{"trip": t[0], "start": t[2], "time": hhmm(t[2]), "profile": t[3]}
                for t in p["trips"] if t[1] in svc]


# ---------- geometry (local metres round the pattern's first stop) ----------

class Line:
    def __init__(self, pts, lat0, lon0):
        self.kx, self.ky = 111320 * math.cos(math.radians(lat0)), 110574
        self.lat0, self.lon0 = lat0, lon0
        self.xy = [self.to_xy(la, lo) for la, lo in pts]
        self.s = [0.0]
        for (x1, y1), (x2, y2) in zip(self.xy, self.xy[1:]):
            self.s.append(self.s[-1] + math.hypot(x2 - x1, y2 - y1))
        self.length = self.s[-1]

    def to_xy(self, lat, lon):
        return ((lon - self.lon0) * self.kx, (lat - self.lat0) * self.ky)

    def to_geo(self, x, y):
        return self.lat0 + y / self.ky, self.lon0 + x / self.kx

    def project(self, x, y, lo=0.0, hi=None):
        """Nearest point on the line between distances lo..hi: (s, lateral distance)."""
        hi = self.length if hi is None else hi
        i0 = max(0, bisect.bisect_right(self.s, lo) - 1)
        i1 = min(len(self.xy) - 1, bisect.bisect_right(self.s, hi))
        best = (lo, float("inf"))
        for i in range(i0, max(i0 + 1, i1)):
            if i + 1 >= len(self.xy):
                break
            (ax, ay), (bx, by) = self.xy[i], self.xy[i + 1]
            dx, dy = bx - ax, by - ay
            L2 = dx * dx + dy * dy or 1e-9
            t = max(0.0, min(1.0, ((x - ax) * dx + (y - ay) * dy) / L2))
            px, py = ax + t * dx, ay + t * dy
            d = math.hypot(x - px, y - py)
            if d < best[1]:
                best = (self.s[i] + t * math.sqrt(L2), d)
        return best

    def point_at(self, s):
        i = max(0, min(len(self.s) - 2, bisect.bisect_right(self.s, s) - 1))
        seg = (self.s[i + 1] - self.s[i]) or 1e-9
        t = max(0.0, min(1.0, (s - self.s[i]) / seg))
        (ax, ay), (bx, by) = self.xy[i], self.xy[i + 1]
        return ax + t * (bx - ax), ay + t * (by - ay), math.degrees(math.atan2(bx - ax, by - ay)) % 360


# ---------- a shift: one trip on one pattern ----------

STATION = re.compile(r"\b(station|stn|terminal|centre|center|park and ride|p\+r)\b|\s[1-9][A-H]$", re.I)


def stop_weight(name):
    """How busy a stop is: stations / platforms (\"Baseline 2A\") and malls far more than a street corner."""
    return 4.0 if STATION.search(name) else 1.0


def rush(sec):
    h = (sec // 3600) % 24
    return 1.8 if h in (7, 8, 15, 16, 17) else 1.2 if 6 <= h <= 19 else 0.5


class Shift:
    def __init__(self, route, pattern, trip, live):
        self.route, self.p, self.live = route, pattern, live
        stops = pattern["stops"]
        prof = pattern["profiles"][trip["profile"]]
        self.trip = trip
        self.sched_arr = [trip["start"] + a for a in prof["arr"]]
        self.sched_dep = [trip["start"] + d for d in prof["dep"]]
        shape = pattern["shape"] or [[s["lat"], s["lon"]] for s in stops]
        self.line = Line(shape, stops[0]["lat"], stops[0]["lon"])
        # each stop's place along the route, never going backwards
        self.stop_s, lo = [], 0.0
        for s in stops:
            x, y = self.line.to_xy(s["lat"], s["lon"])
            sv, _ = self.line.project(x, y, lo, lo + 3000)
            self.stop_s.append(sv)
            lo = sv
        self.n = len(stops)
        self.stops = stops
        # passengers, like real life: most ordinary stops are empty, stations and transfer points are busy,
        # and riders mostly head for those too. Each has a destination further on.
        rng = random.Random(trip["trip"])
        self.weight = [stop_weight(s["name"]) for s in stops]
        self.weight[0] = max(self.weight[0], 3.0)
        self.weight[-1] = max(self.weight[-1], 3.0)
        self.waiting = []
        for i in range(self.n):
            lam = 0 if i == self.n - 1 else rush(self.sched_arr[i]) * self.weight[i] * 0.3
            k = sum(1 for _ in range(12) if rng.random() < lam / 12)
            self.waiting.append([self._dest(rng, i) for _ in range(k)])
        self.rng = rng
        self.onboard = []
        self.events = []
        self.t0 = time.monotonic()
        self.clock0 = self.sched_dep[0] - 60     # one minute to get ready at the first stop
        self.i = 0                 # next stop to serve
        self.prog, self.lateral = 0.0, 0.0
        self.state = "to_stop"     # to_stop | dwelling | done
        self.dwell = {"since": None, "work": 0.0, "boarded": 0, "alighted": 0, "left": 0}
        self.results = []          # per stop: {i, arr, dep, late_arr, late_dep, served, skipped}
        self.comfort, self.jolts, self.throws, self._jolt_at = 100.0, 0, 0, 0
        self.carried = 0
        self.left_behind = 0
        self.missed = 0
        self.speed = 0.0
        self.doors = False
        self.finished = None
        self.sim_t = None          # set by --sim: the simulated seconds since the start

    def _dest(self, rng, i):
        if i >= self.n - 1:
            return i
        # busy stops draw riders; nearer stops a little more than far ones
        later = range(i + 1, self.n)
        w = [self.weight[k] * (0.97 ** (k - i)) for k in later]
        return rng.choices(list(later), weights=w)[0]

    def clock(self):
        if self.live:
            n = datetime.now()
            return n.hour * 3600 + n.minute * 60 + n.second + n.microsecond / 1e6
        if self.sim_t is not None:
            return self.clock0 + self.sim_t
        return self.clock0 + (time.monotonic() - self.t0)

    def note(self, kind, text):
        self.events.append({"t": round(time.time(), 1), "kind": kind, "text": text})
        self.events = self.events[-30:]

    def sched_at(self, s):
        """Scheduled time at a point along the route (between the stops round it)."""
        j = max(0, min(self.n - 2, bisect.bisect_right(self.stop_s, s) - 1))
        a, b = self.stop_s[j], self.stop_s[j + 1]
        f = 0 if b <= a else max(0.0, min(1.0, (s - a) / (b - a)))
        return self.sched_dep[j] + f * (self.sched_arr[j + 1] - self.sched_dep[j])

    def requested(self, i):
        return any(d == i for d in self.onboard)

    def update(self, lat, lon, speed, doors, ax=0.0, ay=0.0):
        """One pose from the game (10 Hz). Returns a list of game commands."""
        if self.state == "done":
            return []
        cmds = []
        self.speed, self.doors = speed, doors
        x, y = self.line.to_xy(lat, lon)
        s, lat_d = self.line.project(x, y, self.prog - 150, self.prog + 600)
        if lat_d > OFF_ROUTE_M:  # lost: search the whole route
            s, lat_d = self.line.project(x, y)
        self.prog, self.lateral = s, lat_d
        now = self.clock()

        # comfort: only matters with people aboard
        g = math.hypot(ax, ay)
        if self.onboard and g > JOLT and time.monotonic() - self._jolt_at > 1.5:
            self._jolt_at = time.monotonic()
            if g > THROW:
                self.throws += 1
                self.comfort -= 6
                self.note("bad", f"Passengers thrown about ({g / 9.81:.1f} g)")
            else:
                self.jolts += 1
                self.comfort -= 2
                self.note("warn", f"Rough ride ({g / 9.81:.1f} g)")
            self.comfort = max(0.0, self.comfort)

        i = self.i
        ds = self.stop_s[i] - s
        near = abs(ds) <= AT_STOP_M
        if self.state == "to_stop":
            if near and speed < STOPPED_MS and doors:
                self.state = "dwelling"
                self.dwell = {"since": now, "work": 0.0, "boarded": 0, "alighted": 0, "left": 0}
                if not self._result(i):
                    late = now - self.sched_arr[i]
                    self.results.append({"i": i, "arr": now, "late_arr": late})
                cmds.append({"cmd": "atStop", "trigger": f"rr_{i}"})
            elif ds < -PASSED_M:
                # drove past it
                want_off = sum(1 for d in self.onboard if d == i)
                want_on = len(self.waiting[i])
                if want_off or want_on:
                    self.missed += 1
                    self.left_behind += want_on
                    self.note("bad", f"Missed {self.stops[i]['name']}: {want_off} wanted off, {want_on} waiting")
                    self.onboard = [d if d != i else min(self.n - 1, i + 1) for d in self.onboard]  # they get off next
                    self.waiting[i] = []
                else:
                    self.note("ok", f"Passed {self.stops[i]['name']} (no one)")
                self.results.append({"i": i, "skipped": True, "missed": bool(want_off or want_on)})
                cmds += self._advance()
        elif self.state == "dwelling":
            if doors and speed < STOPPED_MS:
                d = self.dwell
                prev = d["work"]
                d["work"] += 0.1
                # people get off (rear door) and on (front door) side by side
                off = [k for k, dd in enumerate(self.onboard) if dd == i]
                if off and int(d["work"] / ALIGHT_S) > int(prev / ALIGHT_S):
                    self.onboard.pop(off[0])
                    d["alighted"] += 1
                    self.carried += 1
                if self.waiting[i] and int(d["work"] / BOARD_S) > int(prev / BOARD_S):
                    self.onboard.append(self.waiting[i].pop(0))
                    d["boarded"] += 1
            elif not doors or ds < -AT_STOP_M:
                # doors shut (or drove off with them open)
                d = self.dwell
                if self.waiting[i]:
                    d["left"] = len(self.waiting[i])
                    self.left_behind += d["left"]
                    self.note("bad", f"Left {d['left']} behind at {self.stops[i]['name']}")
                    self.waiting[i] = []
                if any(dd == i for dd in self.onboard):
                    stuck = sum(1 for dd in self.onboard if dd == i)
                    self.note("bad", f"{stuck} couldn't get off at {self.stops[i]['name']}")
                    self.onboard = [dd if dd != i else min(self.n - 1, i + 1) for dd in self.onboard]
                r = self._result(i)
                r["dep"] = now
                r["late_dep"] = now - self.sched_dep[i]
                r["boarded"], r["alighted"] = d["boarded"], d["alighted"]
                if r["late_dep"] < -60 and i < self.n - 1:
                    self.note("warn", f"Left {self.stops[i]['name']} {round(-r['late_dep'] / 60)} min early")
                self.state = "to_stop"
                cmds.append({"cmd": "departed", "trigger": f"rr_{i}"})
                cmds += self._advance()
        if self.i < self.n:
            cmds.append({"cmd": "stopRequest", "on": self.requested(self.i)})
        return cmds

    def _result(self, i):
        for r in self.results:
            if r["i"] == i:
                return r
        return None

    def _advance(self):
        self.i += 1
        if self.i >= self.n or (self.i == self.n - 1 and self._result(self.n - 1)):
            return self._finish()
        if self.i == self.n - 1:
            pass
        return [self.gps_cmd(), self.markers_cmd()]

    def _finish(self):
        self.state = "done"
        self.finished = self.summary()
        self.carried += len(self.onboard)
        self.onboard = []
        s = self.finished
        self.note("ok", f"Trip done: {s['on_time_pct']}% on time, comfort {s['comfort']}, score {s['score']}")
        return [{"cmd": "notify", "text": f"End of the line! Score {s['score']}"}, {"cmd": "markers", "stops": []},
                {"cmd": "gpsClear"}]

    def summary(self):
        arr = [r for r in self.results if "late_arr" in r]
        on_time = [r for r in arr if -60 <= r["late_arr"] <= 300]   # transit agencies' usual window: 1 early .. 5 late
        pct = round(100 * len(on_time) / len(arr)) if arr else 100
        score = round(pct * 0.5 + self.comfort * 0.3 + max(0, 20 - 4 * self.missed - self.left_behind))
        return {"on_time_pct": pct, "comfort": round(self.comfort), "jolts": self.jolts, "throws": self.throws,
                "missed": self.missed, "left_behind": self.left_behind, "carried": self.carried, "score": score,
                "stops_served": len(arr)}

    # ---- game commands ----
    def line_cmd(self):
        return {"cmd": "line", "routeID": self.route["route"], "direction": self.p["headsign"],
                "routeColor": "#" + self.route["color"],
                "tasklist": [[f"rr_{k}", s["name"]] for k, s in enumerate(self.stops)]}

    def gps_cmd(self):
        pts = [{"lat": s["lat"], "lon": s["lon"], "name": s["name"]} for s in self.stops[self.i:self.i + 10]]
        return {"cmd": "gps", "points": pts}

    def markers_cmd(self):
        out = []
        for k in range(self.i, min(self.n, self.i + 3)):
            x, y, hd = self.line.point_at(self.stop_s[k])
            la, lo = self.line.to_geo(x, y)
            s = self.stops[k]
            out.append({"lat": la, "lon": lo, "name": s["name"], "code": s["code"], "next": k == self.i})
        return {"cmd": "markers", "stops": out}

    def start_cmds(self):
        x, y, hd = self.line.point_at(max(0.0, self.stop_s[0] - 25))
        la, lo = self.line.to_geo(x, y)
        return [{"cmd": "goto", "lat": la, "lon": lo, "heading": hd, "name": self.stops[0]["name"], "bus": True},
                self.line_cmd(), self.gps_cmd(), self.markers_cmd()]

    # ---- for the console ----
    def view(self):
        now = self.clock()
        dev = now - self.sched_at(self.prog) if self.state != "dwelling" else now - self.sched_dep[self.i]
        if self.state == "dwelling" and dev < 0:
            dev = 0  # waiting at a stop for the scheduled time is fine
        nxt = []
        for k in range(self.i, min(self.n, self.i + 6)):
            s = self.stops[k]
            nxt.append({"i": k, "name": s["name"], "code": s["code"], "time": hhmm(self.sched_arr[k]),
                        "dist": round(self.stop_s[k] - self.prog), "waiting": len(self.waiting[k]),
                        "off": sum(1 for d in self.onboard if d == k),
                        "need": bool(self.waiting[k]) or any(d == k for d in self.onboard) or k == self.n - 1})
        d = self.dwell if self.state == "dwelling" else None
        ready = None
        if d:
            left_off = sum(1 for dd in self.onboard if dd == self.i)
            ready = not self.waiting[self.i] and not left_off and d["work"] >= DOOR_MIN_S
        return {"route": self.route["route"], "headsign": self.p["headsign"], "color": self.route["color"],
                "text": self.route["text"], "state": self.state, "clock": hhmm(now), "clock_s": now,
                "deviation": round(dev), "next": nxt, "index": self.i, "total": self.n,
                "onboard": len(self.onboard), "requested": self.i < self.n and self.requested(self.i),
                "comfort": round(self.comfort), "speed": round(self.speed * 3.6), "doors": self.doors,
                "lateral": round(self.lateral), "off_route": self.lateral > OFF_ROUTE_M,
                "progress": round(self.prog), "length": round(self.line.length),
                "dwell": d and {"boarded": d["boarded"], "alighted": d["alighted"], "ready": ready,
                                "wait_sched": max(0, round(self.sched_dep[self.i] - now))},
                "events": self.events[-8:], "summary": self.finished, "live": self.live,
                "trip_start": hhmm(self.sched_dep[0])}


# ---------- the helper ----------

class Helper:
    """The game link + the console's API. BeamDeck's server owns one and sends it every /routes/ request."""

    def __init__(self, data_root, gtfs=None, agency="octranspo"):
        self.data_root, self.gtfs, self.agency = data_root, gtfs, agency
        self.data = None
        self.bake_state = {"running": False, "error": None, "done": None}
        self._load()
        self.shift = None
        # bus signs without a shift (BeamDeck's Car tab): custom text and stops, or a real route's stops
        self.sign = {"mode": "off", "routeID": "", "direction": "Not in Service", "color": "#555555",
                     "stops": [], "pattern": None, "idx": 0}
        self.game = {"seen": 0}
        self.route = {"version": None, "points": []}   # BeamNG.Earth's planned GPS route (for the tablet map)
        self.map_port = None
        self.flat, self.level, self.roads_file = False, None, None
        self.seats = {"model": None, "seats": [], "at": 0}
        self._roads = (None, 0, b"")
        self._tiles = {}                                  # small in-memory cache: (z, x, y) -> jpeg
        self.lock = threading.Lock()
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.sock.bind(("127.0.0.1", MY_PORT))
        import sys
        if HERE not in sys.path:
            sys.path.insert(0, HERE)
        import rides, people
        self.people = people
        os.makedirs(data_root, exist_ok=True)
        self.rides = rides.Rides(self.send, data_root)
        threading.Thread(target=self.game_loop, daemon=True).start()

    def _load(self):
        root = os.path.join(self.data_root, self.agency)
        if os.path.exists(os.path.join(root, "index.json")):
            self.data = Data(root)

    def bake_async(self):
        if self.bake_state["running"]:
            return
        self.bake_state.update(running=True, error=None)

        def run():
            try:
                import sys
                if HERE not in sys.path:
                    sys.path.insert(0, HERE)
                import bake_routes
                kw = {"agency": self.agency, "out_root": self.data_root}
                if self.gtfs:
                    kw["gtfs"] = self.gtfs
                bake_routes.bake(**kw)
                self.data = None
                self._load()
                self.bake_state.update(done=time.strftime("%Y-%m-%d %H:%M"))
            except Exception as e:
                log("bake failed:", e)
                self.bake_state["error"] = str(e)
            finally:
                self.bake_state["running"] = False
        threading.Thread(target=run, daemon=True).start()

    def send(self, cmds):
        for c in cmds:
            try:
                self.sock.sendto(json.dumps(c, separators=(",", ":")).encode(), ("127.0.0.1", GAME_PORT))
            except OSError as e:
                log("send failed:", e)

    def game_loop(self):
        while True:
            data, _ = self.sock.recvfrom(65536)
            try:
                m = json.loads(data)
            except ValueError:
                continue
            with self.lock:
                if m.get("type") == "pose":
                    self.game = m | {"seen": time.time()}
                    if self.shift:
                        self.send(self.shift.update(m["lat"], m["lon"], m.get("speed", 0), bool(m.get("doors")),
                                                    m.get("gx", 0), m.get("gy", 0)))
                    else:
                        self.rides.update(m)
                elif m.get("type") == "places":
                    self.rides.on_places(m)
                elif m.get("type") == "debug":
                    self.debug = m
                elif m.get("type") == "seats":
                    self.seats = {"model": m.get("model"), "seats": m.get("seats") or [], "at": time.time()}
                elif m.get("type") == "route":
                    self.route = {"version": m.get("version"), "points": m.get("points") or [],
                                  "dist": m.get("dist"), "dur": m.get("dur"), "steps": m.get("steps") or []}
                    self.rides.on_route(self.route)
                elif m.get("type") == "hello":
                    self.game["seen"] = time.time()
                    self.game["earth"] = m.get("earth")
                    if m.get("mapPort"):
                        self.map_port = int(m["mapPort"])
                    # a regular map: the game's metres as lat / lon, and the level's roads for the map
                    self.flat = bool(m.get("flat"))
                    self.level = m.get("level")
                    if m.get("roads"):
                        self.roads_file = m["roads"]
                    if self.shift and self.shift.state != "done":  # game restarted mid-shift: show it again
                        self.send([self.shift.line_cmd(), self.shift.gps_cmd(), self.shift.markers_cmd()])
                    elif self.sign["mode"] != "off":
                        self._sign_send()

    def start(self, pid, trip_id, live, go):
        r, p = self.data.pattern(pid)
        trips = self.data.trips(pid, date.today()) or [{"trip": t[0], "start": t[2], "profile": t[3]} for t in p["trips"]]
        trip = next((t for t in trips if t["trip"] == trip_id), None) or trips[0]
        with self.lock:
            self.rides.go_online(False)
            self.shift = Shift(r, p, trip, live)
            cmds = self.shift.start_cmds()
            if not go:
                cmds = cmds[1:]
            self.send(cmds)
        log("shift", pid, trip["trip"], hhmm(trip["start"]), "live" if live else "own clock")

    # ---- regular maps: the level's roads (written by the mod) as GeoJSON, and a map style drawing them ----
    def roads_geojson(self):
        f = self.roads_file
        if not (f and os.path.exists(f)):
            return b'{"type":"FeatureCollection","features":[]}'
        mt = os.path.getmtime(f)
        if self._roads[0] == f and self._roads[1] == mt:
            return self._roads[2]
        segs = (json.load(open(f)) or {}).get("segs") or []
        main = [[[sg[1], sg[0]], [sg[3], sg[2]]] for sg in segs if len(sg) > 4 and sg[4] >= 0.5]
        minor = [[[sg[1], sg[0]], [sg[3], sg[2]]] for sg in segs if len(sg) > 4 and sg[4] < 0.5]
        out = json.dumps({"type": "FeatureCollection", "features": [
            {"type": "Feature", "properties": {"k": "main"}, "geometry": {"type": "MultiLineString", "coordinates": main}},
            {"type": "Feature", "properties": {"k": "minor"}, "geometry": {"type": "MultiLineString", "coordinates": minor}}]},
            separators=(",", ":")).encode()
        self._roads = (f, mt, out)
        return out

    def map_style(self, base, light=False):
        """The map's style: OpenFreeMap's real streets on BeamNG.Earth; the level's own roads on a regular map.
        `base` makes the roads URL absolute (Android Auto); the tablet uses it relative."""
        if not self.flat:
            return {"url": "https://tiles.openfreemap.org/styles/" + ("liberty" if light else "dark")}
        bg, minor, main = ("#ecebe6", "#ffffff", "#ffffff") if light else ("#11141d", "#2b3142", "#4a5268")
        edge = "#c9c6bd" if light else None
        roads = (base.rstrip("/") + "/api/roads") if base else "api/roads"
        return {"style": {
            "version": 8, "name": "Real Routes level map",
            "sources": {"roads": {"type": "geojson", "data": roads}},
            "layers": [
                {"id": "bg", "type": "background", "paint": {"background-color": bg}},
                *([{"id": "roads-edge", "type": "line", "source": "roads",
                    "paint": {"line-color": edge, "line-width": ["interpolate", ["linear"], ["zoom"], 12, 2.5, 18, 17]},
                    "layout": {"line-cap": "round", "line-join": "round"}}] if edge else []),
                {"id": "roads-minor", "type": "line", "source": "roads", "filter": ["==", ["get", "k"], "minor"],
                 "paint": {"line-color": minor, "line-width": ["interpolate", ["linear"], ["zoom"], 12, 1, 18, 6]},
                 "layout": {"line-cap": "round", "line-join": "round"}},
                {"id": "roads-main", "type": "line", "source": "roads", "filter": ["==", ["get", "k"], "main"],
                 "paint": {"line-color": main, "line-width": ["interpolate", ["linear"], ["zoom"], 12, 1.5, 18, 14]},
                 "layout": {"line-cap": "round", "line-join": "round"}}]}}

    # ---- map tiles: BeamNG.Earth's own (the in-game world map's), relayed to the tablet ----
    def tile(self, path):
        try:
            z, x, y = (int(v.split(".")[0]) for v in path.strip("/").split("/")[1:4])
        except ValueError:
            return 404, b"bad tile", "text/plain"
        key = (z, x, y)
        if key in self._tiles:
            return 200, self._tiles[key], "image/jpeg"
        if not self.map_port:
            return 503, b"BeamNG.Earth's map isn't running", "text/plain"
        try:
            data = _tile_get(self.map_port, z, x, y)
        except Exception as e:
            code = getattr(e, "code", 503)
            return (code if code in (404, 503) else 503), b"no tile", "text/plain"
        if len(self._tiles) > 600:
            self._tiles.pop(next(iter(self._tiles)))
        self._tiles[key] = data
        return 200, data, "image/jpeg"

    # ---- bus signs without a shift ----
    def shift_running(self):
        return bool(self.shift and self.shift.state != "done")

    def _sign_send(self):
        g = self.sign
        cmds = [{"cmd": "line", "routeID": g["routeID"], "direction": g["direction"], "routeColor": g["color"],
                 "tasklist": [[f"rr_{k}", n] for k, n in enumerate(g["stops"])]}]
        if g["idx"] > 0 and g["stops"]:
            cmds.append({"cmd": "departed", "trigger": f"rr_{g['idx'] - 1}"})
        self.send(cmds)

    def set_sign(self, body):
        """{mode: off|custom|route, routeID, direction, color, stops: [names] | pattern}"""
        if self.shift_running():
            return False, "A Real Routes shift is running: it controls the signs"
        mode = body.get("mode", "custom")
        g = {"mode": mode, "routeID": "", "direction": "Not in Service", "color": "#555555", "stops": [],
             "pattern": None, "idx": 0}
        if mode == "custom":
            g.update(routeID=str(body.get("routeID", ""))[:6], direction=str(body.get("direction", ""))[:40] or " ",
                     color=body.get("color") or "#555555",
                     stops=[str(x).strip()[:40] for x in body.get("stops", []) if str(x).strip()][:80])
        elif mode == "route":
            if not self.data:
                return False, "The routes aren't downloaded yet"
            r, p = self.data.pattern(body["pattern"])
            g.update(routeID=r["route"], direction=p["headsign"], color="#" + r["color"], pattern=p["id"],
                     stops=[st["name"] for st in p["stops"]])
        with self.lock:
            self.sign = g
            self._sign_send()
        return True, ""

    def step_sign(self, delta):
        if self.shift_running():
            return False, "A Real Routes shift is running: it controls the signs"
        g = self.sign
        if not g["stops"]:
            return False, "No stops on the sign"
        with self.lock:
            g["idx"] = max(0, min(len(g["stops"]) - 1, g["idx"] + int(delta)))
            self._sign_send()
        return True, ""

    def state(self):
        with self.lock:
            g = self.game
            out = {"game": {"connected": time.time() - g.get("seen", 0) < 3, "bus": g.get("bus"),
                            "ready": g.get("ready"), "speedLimit": g.get("limit"), "earth": g.get("earth"),
                            "lat": g.get("lat"), "lon": g.get("lon"), "heading": g.get("heading"),
                            "gpsLeft": g.get("gpsLeft"), "gpsEta": g.get("gpsEta"), "map": bool(self.map_port), "model": g.get("model"), "place": g.get("place"), "gpsName": g.get("gpsName"), "speed": g.get("speed"),
                            "routeVersion": self.route.get("version"), "flat": self.flat, "level": self.level,
                            "lights": bool(g.get("lights"))},
                   "shift": self.shift.view() if self.shift else None,
                   "rides": self.rides.view(),
                   "sign": self.sign | {"next": self.sign["stops"][self.sign["idx"]] if self.sign["stops"] else None,
                                        "locked": self.shift_running()},
                   "data": {"ready": self.data is not None, "bake": self.bake_state, "agency": self.agency}}
        return out

    def command(self, c):
        with self.lock:
            if c in ("doors", "kneel", "hazards"):
                self.send([{"cmd": c}])
            elif c == "end" and self.shift:
                self.send([{"cmd": "markers", "stops": []}, {"cmd": "gpsClear"},
                           {"cmd": "line", "routeID": "", "direction": "Not in Service", "routeColor": "#555555",
                            "tasklist": []}])
                self.shift = None
            elif c == "resync" and self.shift:
                self.send([self.shift.line_cmd(), self.shift.gps_cmd(), self.shift.markers_cmd()])
            elif c == "goto" and self.shift:
                self.send(self.shift.start_cmds()[:1])

    def handle(self, method, path, qs, body):
        """One console request; path is relative to the console's root ("" or "/" is the page).
        Returns (status, bytes, content type)."""
        js = lambda o: (200, json.dumps(o).encode(), "application/json")
        path = "/" + path.lstrip("/")
        if method == "GET" and path.startswith("/tiles/"):
            return self.tile(path)
        if method == "GET" and path.startswith("/maplibre/"):
            f = os.path.basename(path)
            if f in ("maplibre-gl.js", "maplibre-gl.css"):
                return 200, open(os.path.join(HERE, "maplibre", f), "rb").read(), \
                    "text/javascript" if f.endswith(".js") else "text/css"
        if method == "GET" and path.startswith("/avatars/"):
            from urllib.parse import unquote
            name = unquote(path[len("/avatars/"):]).rsplit(".", 1)[0]
            f = self.rides.avatar_path(name)
            if f:
                return 200, open(f, "rb").read(), "image/jpeg"
            if self.people.is_generic(name):                   # a built-in person: a drawn pictogram
                return 200, self.people.avatar_png(name), "image/png"
            return 404, b"no avatar", "text/plain"
        if method == "GET" and path == "/api/people":
            return js(self.rides.people_info())
        if method == "GET" and path == "/api/history":
            return js({"history": self.rides.stats.get("history", [])[:100]})
        if method == "GET" and path == "/api/seats":
            mdir = os.path.join(self.data_root, "models")
            chars = sorted(n for n in os.listdir(mdir) if os.path.exists(os.path.join(mdir, n, "seated.dae"))) \
                if os.path.isdir(mdir) else []
            return js(self.seats | {"characters": chars})
        if method == "GET" and path == "/api/pose":       # tiny and frequent: the tablet's map follows the car smoothly
            g = self.game
            return js({"lat": g.get("lat"), "lon": g.get("lon"), "heading": g.get("heading"), "speed": g.get("speed")})
        if method == "GET" and path == "/api/debug":
            return js(getattr(self, "debug", None) or {})
        if method == "GET" and path == "/api/roads":
            return 200, self.roads_geojson(), "application/json"
        if method == "GET" and path == "/api/mapstyle":
            return js(self.map_style(qs.get("base", ""), qs.get("light") == "1"))
        if method == "GET" and path == "/api/route":
            return js(self.route)
        if method == "GET" and path == "/api/shape" and self.shift:
            sh = self.shift
            return js({"shape": sh.p["shape"], "stops": [{"name": st["name"], "code": st["code"], "lat": st["lat"],
                                                          "lon": st["lon"]} for st in sh.stops]})
        if method == "GET":
            static = {"/": ("console.html", "text/html; charset=utf-8"), "/index.html": ("console.html", "text/html; charset=utf-8"),
                      "/manifest.json": ("manifest.json", "application/manifest+json"), "/sw.js": ("sw.js", "text/javascript"),
                      "/icon.svg": ("icon.svg", "image/svg+xml"), "/icon-512.png": ("icon-512.png", "image/png")}
            if path in static:
                f, ctype = static[path]
                return 200, open(os.path.join(HERE, f), "rb").read(), ctype
            if path == "/api/state":
                return js(self.state())
            if path == "/api/routes":
                return js(self.data.index if self.data else {"routes": []})
            if path == "/api/trips" and self.data:
                d = date.today() + timedelta(days=int(qs.get("day", 0)))
                return js({"date": d.isoformat(), "trips": self.data.trips(qs["pattern"], d)})
        elif method == "POST":
            if path == "/api/start" and self.data:
                self.start(body["pattern"], body.get("trip"), bool(body.get("live")), body.get("go", True))
                return js({"ok": True})
            if path == "/api/cmd":
                self.command(body.get("cmd"))
                return js({"ok": True})
            if path == "/api/bake":
                self.bake_async()
                return js({"ok": True})
            if path == "/api/rides":
                act = body.get("action")
                with self.lock:
                    if act == "online":
                        if self.shift_running():
                            return js({"ok": False, "error": "End the bus shift first"})
                        self.rides.go_online(True)
                    elif act == "offline":
                        self.rides.go_online(False)
                    elif act in ("accept", "decline", "cancel"):
                        getattr(self.rides, act)()
                    elif act == "preview" and body.get("avatar"):
                        self.rides.preview(str(body["avatar"]))
                return js({"ok": True})
            if path == "/api/seats":
                if body.get("seat"):
                    self.send([{"cmd": "seatPut", "seat": str(body["seat"]), "avatar": body.get("avatar") or None}])
                else:
                    self.send([{"cmd": "seats"}])
                return js({"ok": True})
            if path == "/api/profile":
                self.rides.set_profile(body)
                return js({"ok": True})
            if path == "/api/people":                     # the player's own riders, and who asks for rides
                act = body.get("action")
                with self.lock:
                    if act == "add":
                        ok, why = self.rides.add_person(body.get("name"), body.get("picture"), body.get("card"))
                    elif act == "delete":
                        ok, why = self.rides.delete_person(body.get("name"))
                    elif act == "pool":
                        self.rides.set_pool(str(body.get("pool")))
                        ok, why = True, ""
                    else:
                        ok, why = False, "unknown action"
                return js({"ok": ok, "error": None if ok else why, "name": why if ok else None})
            if path == "/api/sign":
                ok, why = self.set_sign(body)
                return js({"ok": ok, "error": why})
            if path == "/api/sign/step":
                ok, why = self.step_sign(body.get("delta", 1))
                return js({"ok": ok, "error": why})
        return 404, b"not found", "text/plain"


def _tile_get(port, z, x, y):
    import urllib.request
    with urllib.request.urlopen(f"http://127.0.0.1:{port}/tiles/{z}/{x}/{y}.jpg", timeout=20) as r:
        return r.read()


def allowed_client(addr):
    ip = ipaddress.ip_address(addr)
    return ip.is_loopback or ip.is_private or ip in ipaddress.ip_network("100.64.0.0/10")


def make_handler(h):
    """Standalone mode (no BeamDeck): the console on its own port."""
    from urllib.parse import urlsplit, parse_qsl

    class H(BaseHTTPRequestHandler):
        def _go(self, method):
            if not allowed_client(self.client_address[0]):
                return self.send_error(403)
            u = urlsplit(self.path)
            body = {}
            if method == "POST":
                n = int(self.headers.get("Content-Length") or 0)
                body = json.loads(self.rfile.read(n) or b"{}")
            code, data, ctype = h.handle(method, u.path, dict(parse_qsl(u.query)), body)
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def do_GET(self): self._go("GET")
        def do_POST(self): self._go("POST")
        def log_message(self, *a): pass
    return H


def simulate(data, pid, speedup):
    """Drive a fake bus along the route: stops at every requested stop, opens doors until all aboard."""
    r, p = data.pattern(pid)
    t = data.trips(pid, date.today())[0]
    sh = Shift(r, p, t, live=False)
    s, v, dt = 0.0, 0.0, 0.1
    hold = 0
    sh.sim_t = 0.0
    while sh.state != "done":
        i = sh.i
        target = sh.stop_s[i]
        need = sh.requested(i) or sh.waiting[i] or i == 0 or i == sh.n - 1
        doors = False
        if sh.state == "dwelling":
            vv = sh.view()
            doors = not (vv["dwell"] and vv["dwell"]["ready"]) or sh.clock() < sh.sched_dep[i] - 20
            v = 0
        elif need and 0 <= target - s < 40:
            v = max(0.0, v - 2.0 * dt) if target - s > 2 else 0
            doors = v == 0
        else:
            v = min(13.0, v + 1.2 * dt)
        s += v * dt
        x, y, _ = sh.line.point_at(s)
        la, lo = sh.line.to_geo(x, y)
        sh.update(la, lo, v, doors, ax=(2.0 if v < 13 else 0), ay=0)
        sh.sim_t += dt
        hold += 1
        if hold > 200000:
            print("stuck"); break
    print(json.dumps(sh.finished, indent=1))
    for e in sh.events[-10:]:
        print(e["kind"], e["text"])


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--data", default=os.path.expanduser("~/.local/share/beamdeck/realroutes"))
    ap.add_argument("--agency", default="octranspo")
    ap.add_argument("--sim", help="pattern id: run a simulated trip and exit")
    ap.add_argument("--lan", default="0.0.0.0", help="address the console listens on (standalone mode)")
    a = ap.parse_args()
    if a.sim:
        return simulate(Data(os.path.join(a.data, a.agency)), a.sim, 1)
    h = Helper(a.data, agency=a.agency)
    log(f"console on http://{a.lan}:{HTTP_PORT}/  game UDP {MY_PORT}->{GAME_PORT}")
    ThreadingHTTPServer((a.lan, HTTP_PORT), make_handler(h)).serve_forever()


if __name__ == "__main__":
    main()
