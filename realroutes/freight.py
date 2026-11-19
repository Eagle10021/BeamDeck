"""Freight: trucking jobs on BeamNG.Earth (or any map). The tablet is the dispatcher: a Euro Truck-style job board, a
garage to pick the truck, and a driving screen with live truck stats. Lives in the Real Routes helper, which feeds it
poses and the game's answers and sends its commands on.

States: idle (browsing the board) -> resolving (finding real places for the pickup and drops) -> spawning (the mod
puts the truck and a fitting trailer at the pickup) -> driving (deliver every drop) -> done (pay, rating).
"""
import json, math, os, random, re, time
import freight_data as D

STOPPED_MS = 1.5                   # m/s: counts as stopped at a drop
ARRIVE_M = 60                      # metres from a drop (or the road end beside it)
RESOLVE_S = 14                     # how long to wait for the game's places before using the town's centre
SPAWN_S = 120                      # teleports and spawns can take a while
JOLT_HOLD = 5                      # samples in a row (10 a second)
JOLT_GAP_S = 6
JOLTS_FREE = 2
CRASH, CRASH_HELD = 30.0, 20.0     # m/s²: a real hit
LIMIT = {"normal": 4.5, "fragile": 3.5, "hazard": 3.0}   # m/s² sustained: trucks are heavy, 0.45 g is a hard moment
SPEEDING_KMH, SPEEDING_S = 25, 10  # over the posted limit by this much for this long is a ticket
TICKET = 25.0
LOST_WARN_S, LOST_FAIL_S = 3, 45   # trailer unhooked
SLACK = 1.18
ROAD = ((40, 1.35), (200, 1.28), (1e9, 1.22))   # straight line -> road distance, by length

NEED_LABEL = {"tractor": "a semi tractor", "box": "a box truck", "flatbed": "a flatbed truck", "tanker": "a tanker truck",
              "dump": "a dump truck", "mixer": "a concrete mixer", "drawbar": "a truck with an eye hitch (pintle) for a drawbar trailer",
              "light": "a pickup, van or small truck"}


def dist_m(a, b):
    la = math.radians((a[0] + b[0]) / 2)
    return math.hypot((a[0] - b[0]) * 110574, (a[1] - b[1]) * 111320 * math.cos(la))


def bearing(a, b):
    la = math.radians(a[0])
    return (math.degrees(math.atan2((b[1] - a[1]) * math.cos(la), b[0] - a[0])) + 360) % 360


def offset(p, bearing_deg, metres):
    la = math.radians(p[0])
    return (p[0] + metres * math.cos(math.radians(bearing_deg)) / 110574,
            p[1] + metres * math.sin(math.radians(bearing_deg)) / (111320 * max(0.2, math.cos(la))))


def road_km(straight_m):
    km = straight_m / 1000
    for lim, f in ROAD:
        if km < lim:
            return km * f


def round5(v):
    return int(round(v / 5.0) * 5)


class Freight:
    def __init__(self, send, data_dir, announce=None):
        self.send = send                     # send(list of game commands)
        self.announce = announce or (lambda text, kind="pa", who=None: None)
        self.file = os.path.join(data_dir, "freight.json")
        self.stats = {"balance": 0.0, "jobs": 0, "km": 0.0, "tonne_km": 0.0, "late": 0, "best": 0.0, "fuel_l": 0.0,
                      "history": [], "since": time.strftime("%B %Y")}
        self.truck = None                    # the selected truck: {model, config, name}
        self.overrides = {}                  # "model/config" -> list of rig classes the player set by hand
        self.models = []                     # what the game reported: [{model, brand, name, type, configs: [...]}]
        self.models_at = 0
        self.favourites = ["percheron"]
        try:
            saved = json.load(open(self.file))
            self.stats.update(saved.get("stats", {}))
            self.truck = saved.get("truck")
            self.overrides = saved.get("overrides", {})
            self.models = saved.get("models", [])
            self.models_at = saved.get("models_at", 0)
            self.favourites = saved.get("favourites", self.favourites)
        except Exception:
            pass
        self.state = "idle"
        self.job = None
        self.summary = None
        self.pose = None
        self.tel = {}
        self.market = {"hub": None, "offers": [], "built": 0, "n": 0, "at": None}
        self.custom = {}                     # offers the player planned: id -> offer
        self.events = []
        self.v = 1                           # bumped when the board's contents change, so the console refetches it
        self._q = 0
        self.pending = {}                    # places request id -> (job id, stop index)
        self._asked_models = 0
        self._hello_at = 0.0
        self._models_part = []

    # ------------------------------------------------------------------ plumbing
    def _save(self):
        try:
            json.dump({"stats": self.stats, "truck": self.truck, "overrides": self.overrides, "models": self.models,
                       "models_at": self.models_at, "favourites": self.favourites}, open(self.file + ".part", "w"))
            os.replace(self.file + ".part", self.file)
        except OSError:
            pass

    def active(self):
        return self.state in ("resolving", "spawning", "driving")

    def note(self, kind, text):
        self.events.append({"t": round(time.time(), 1), "kind": kind, "text": text})
        self.events = self.events[-25:]
        self.v += 1

    def say(self, text, kind="ok", secs=6, speak=True):
        self.note(kind, text)
        self.send([{"cmd": "notify", "text": text, "secs": secs}])
        if speak:
            self.announce(text, "app")

    def connected(self):
        return bool(self.pose) and time.time() - self.pose.get("_at", 0) < 3

    # ------------------------------------------------------------------ the garage
    def on_vehicles(self, m):
        """The game's vehicle list, one model per message. Keeps freight trucks (every model with a config we can
        classify) and the specs the game shows for them."""
        if m.get("first"):
            self._models_part = []
        mod = m.get("model")
        if mod:
            self._models_part.append(mod)
        if m.get("last"):
            self.models = self._models_part
            self._models_part = []
            self.models_at = time.time()
            self.v += 1
            self._save()

    def caps_of(self, model, key, name="", mtype="Truck"):
        ov = self.overrides.get(f"{model}/{key}")
        if ov is not None:
            return set(ov)
        return D.classify(model, key, name, mtype)

    def garage(self):
        out = []
        for md in self.models:
            for c in md.get("configs", []):
                caps = self.caps_of(md["model"], c["key"], c.get("name", ""), md.get("type", "Truck"))
                if not caps:
                    continue
                out.append({"model": md["model"], "key": c["key"], "brand": md.get("brand", ""), "name": md.get("name", ""),
                            "label": c.get("name") or c["key"], "caps": sorted(caps), "family": D.family(md["model"]),
                            "fav": md["model"] in self.favourites, "specs": c.get("specs", {}),
                            "override": f"{md['model']}/{c['key']}" in self.overrides})
        out.sort(key=lambda t: (not t["fav"], t["brand"], t["name"], t["label"]))
        return out

    def truck_caps(self):
        t = self.truck
        if not t:
            return set()
        return self.caps_of(t["model"], t["config"], t.get("label", ""), "Truck")

    def select_truck(self, model, key):
        found = next((g for g in self.garage() if g["model"] == model and g["key"] == key), None)
        if not found and not (model and key):
            return False, "Pick a truck from the list"
        label = found["label"] if found else key
        self.truck = {"model": model, "config": key, "label": label,
                      "name": f"{found['brand']} {found['name']}".strip() if found else model}
        self.v += 1
        self._save()
        return True, ""

    def set_override(self, model, key, caps):
        caps = [c for c in caps if c in NEED_LABEL]
        k = f"{model}/{key}"
        if caps:
            self.overrides[k] = caps
        else:
            self.overrides.pop(k, None)
        self.v += 1
        self._save()

    def fits(self, offer):
        """(ok, why) for the selected truck."""
        if not self.truck:
            return False, "Pick a truck in the Garage"
        cargo = D.CARGO_BY_ID[offer["cargo"]]
        if D.can_carry(self.truck_caps(), cargo):
            return True, ""
        return False, "Needs " + NEED_LABEL[cargo["needs"]]

    # ------------------------------------------------------------------ the market
    def _near_hub(self, lat, lon):
        best = min(D.HUBS, key=lambda h: dist_m((lat, lon), (h[1], h[2])))
        return best[0]

    def _me(self):
        p = self.pose
        return (p["lat"], p["lon"]) if p and p.get("lat") is not None else None

    def set_market(self, hub=None, refresh=False):
        if hub and hub not in D.HUB:
            return False, "Unknown town"
        if not hub:
            me = self._me()
            hub = self.market["hub"] or (self._near_hub(*me) if me else "Ottawa")
        if refresh:
            self.market["n"] += 1
        if hub != self.market["hub"] or refresh or not self.market["offers"]:
            self.market["hub"] = hub
            self.market["offers"] = self._build(hub)
            self.market["built"] = time.time()
            self.v += 1
        return True, ""

    def _hub_pt(self, name):
        h = D.HUB[name]
        return {"name": name, "lat": h["lat"], "lon": h["lon"], "hub": name}

    def _local_pt(self, rng, hubname, role):
        h = D.HUB[hubname]
        p = offset((h["lat"], h["lon"]), rng.uniform(0, 360), rng.uniform(1500, 7000))
        return {"name": f"{hubname} {role}", "lat": p[0], "lon": p[1], "hub": hubname}

    def _along(self, a, b, n, rng):
        """n towns between a and b, in driving order. On the Ottawa-Toronto corridor that is the highway's own order
        (Brockville, Kingston, Belleville...); elsewhere the towns near the straight line between them."""
        C = D.CORRIDOR
        if a in C and b in C:
            i, j = C.index(a), C.index(b)
            mids = C[i + 1:j] if i < j else C[j + 1:i][::-1]
            # the towns a driver would name first (a drop in Mallorytown is less likely than one in Kingston)
            anchors = [m for m in ("Brockville", "Kingston", "Belleville", "Cobourg", "Oshawa") if m in mids]
            pool = anchors + [m for m in mids if m not in anchors] if len(anchors) < n else anchors
            pool = sorted(pool[:max(n, len(anchors))], key=mids.index)
            if len(pool) <= n:
                return pool
            return sorted({pool[int(k * len(pool) / n)] for k in range(n)}, key=mids.index)
        A, B = D.HUB[a], D.HUB[b]
        pa, pb = (A["lat"], A["lon"]), (B["lat"], B["lon"])
        total = dist_m(pa, pb)
        cands = []
        for h in D.HUBS:
            if h[0] in (a, b):
                continue
            p = (h[1], h[2])
            da, db = dist_m(pa, p), dist_m(p, pb)
            if da + db > total * 1.05 or da < total * 0.07 or db < total * 0.07:
                continue
            cands.append((da, h[0]))
        cands.sort()
        if len(cands) <= n:
            return [c[1] for c in cands]
        return [cands[int((k + 0.5) * len(cands) / n)][1] for k in range(n)]

    def _make_offer(self, rng, cargo, origin, stops, kind, scen=None, pay_mult=1.0, title=None, blurb=None, mins_mult=1.0):
        pts = [origin] + stops
        straight = sum(dist_m((a["lat"], a["lon"]), (b["lat"], b["lon"])) for a, b in zip(pts, pts[1:]))
        km = max(road_km(straight), 3.0)
        speed = 36 if km < 30 else 58 if km < 150 else 76
        mins = max(12, round((km / speed * 60 + 10 + 4 * len(stops)) * SLACK * mins_mult))
        fam = "us"
        trailer = None
        for f in ("us", "eu"):
            if (cargo.get("trailers") or {}).get(f):
                trailer = cargo["trailers"][f][0]
                fam = f
                break
        weight = rng.uniform(*cargo["weight"])
        if trailer:
            mt = re.search(r"(\d{4,5})kg", trailer[1])
            if mt:
                weight = int(mt.group(1)) / 1000
        weight = round(weight, 1)
        pay = (50 + km * cargo["rate"]) * (0.85 + weight / 60) * pay_mult
        if km >= 300:
            pay *= 1.1
        pay = round5(pay)
        return {"id": f"o{rng.getrandbits(32):08x}", "cargo": cargo["id"], "scenario": scen, "icon": cargo["icon"],
                "title": title or cargo["name"], "load": rng.choice(cargo["loads"]), "kind": kind,
                "from": origin, "stops": stops, "km": round(km), "mins": mins, "pay": pay, "weight": weight,
                "needs": cargo["needs"], "fragile": cargo["fragile"], "hazard": bool(cargo.get("hazard")),
                "rush": bool(cargo.get("rush")), "blurb": blurb or "", "trailer": trailer[1] if trailer else None}

    def _build(self, hubname):
        rng = random.Random(f"{hubname}|{int(time.time() // 900)}|{self.market['n']}")
        me = self.truck_caps()
        origin = self._hub_pt(hubname)
        here = (origin["lat"], origin["lon"])
        hubs = sorted((h for h in D.HUBS if h[0] != hubname), key=lambda h: dist_m(here, (h[1], h[2])))
        by = lambda lo, hi: [h[0] for h in hubs if lo <= dist_m(here, (h[1], h[2])) / 1000 * 1.25 <= hi]
        offers = []

        def pick_cargo(local=False, port_here=False):
            pool = [c for c in D.CARGO if not (c.get("port") and "port" not in D.HUB[hubname]["tags"])
                    and (not c.get("local") or True)]
            if local:
                pool = [c for c in pool if c.get("local")] or pool
            fit = [c for c in pool if D.can_carry(me, c)] if me else []
            if fit and rng.random() < 0.7:
                return rng.choice(fit)
            return rng.choice(pool)

        # local: inside the town (drops spread round it)
        for _ in range(4):
            c = pick_cargo(local=True)
            n = rng.randint(*c["stops"]) if c.get("stops") else 1
            stops = [self._local_pt(rng, hubname, rng.choice(["customer", "site", "yard"])) for _ in range(n)]
            offers.append(self._make_offer(rng, c, self._local_pt(rng, hubname, "depot"), stops, "local"))
        # regional: the towns within reach
        for dest in rng.sample(by(30, 160), min(6, len(by(30, 160)))):
            c = pick_cargo()
            offers.append(self._make_offer(rng, c, origin, [self._hub_pt(dest)], "regional"))
        # long haul
        far = by(160, 800)
        for dest in rng.sample(far, min(4, len(far))):
            c = pick_cargo()
            offers.append(self._make_offer(rng, c, origin, [self._hub_pt(dest)], "long"))
        # themed scenarios
        for sc in D.SCENARIOS:
            o = self._scenario(rng, sc, hubname, origin, by)
            if o:
                offers.append(o)
        return offers

    def _scenario(self, rng, sc, hubname, origin, by):
        cargo = D.CARGO_BY_ID[sc["cargo"]]
        if sc.get("needs_tag") and sc["needs_tag"] not in D.HUB[hubname]["tags"]:
            return None
        frm = sc.get("frm")
        here = (origin["lat"], origin["lon"])
        if frm:                                    # a fixed pickup: only when the market town is near it
            f = D.HUB[frm]
            if dist_m(here, (f["lat"], f["lon"])) > 90000:
                return None
            o = self._hub_pt(frm)
        else:
            o = origin if not cargo.get("local") else self._local_pt(rng, hubname, "depot")
        to = sc.get("to")
        stops = []
        if cargo.get("local") or sc.get("stops") and not to:
            n = rng.randint(*sc["stops"]) if sc.get("stops") else 1
            stops = [self._local_pt(rng, hubname, rng.choice(["customer", "site", "yard"])) for _ in range(n)]
        elif to:
            if sc.get("via"):
                stops = [self._hub_pt(v) for v in self._along(frm, to, sc["via"], rng)]
            stops.append(self._hub_pt(to))
        else:
            lo, hi = sc.get("minkm", 30), sc.get("maxkm", 300)
            hubs = [h for h in by(lo, hi)]
            if not hubs:
                return None
            stops = [self._hub_pt(rng.choice(hubs))]
        if sc["id"] == "steel_mill" and not to:
            stops = [self._hub_pt(rng.choice(by(60, 500) or by(30, 800)))]
        mult = 0.55 if sc["id"] == "concrete_rush" else 1.0
        return self._make_offer(rng, cargo, o, stops, "scenario", sc["id"], sc.get("pay", 1.0), sc["name"], sc["blurb"], mult)

    def plan(self, body):
        """A custom job from the player's choices: from / to / via towns and a cargo. Returns (ok, offer or error)."""
        frm, to = body.get("from"), body.get("to")
        via = [v for v in (body.get("via") or []) if v in D.HUB]
        if frm not in D.HUB or to not in D.HUB:
            return False, "Pick the towns to start and finish in"
        if frm == to and not via:
            return False, "Pick two different towns"
        cargo = D.CARGO_BY_ID.get(body.get("cargo"))
        if not cargo:
            return False, "Pick a cargo"
        rng = random.Random(f"{frm}{to}{via}{time.time()}")
        stops = [self._hub_pt(v) for v in via] + [self._hub_pt(to)]
        o = self._make_offer(rng, cargo, self._hub_pt(frm), stops, "custom", None, 1.0, f"{frm} → {to}",
                             "Your own route" + (f" via {', '.join(via)}" if via else ""))
        self.custom[o["id"]] = o
        return True, o

    def _find_offer(self, oid):
        return self.custom.get(oid) or next((o for o in self.market["offers"] if o["id"] == oid), None)

    # ------------------------------------------------------------------ starting a job
    def start(self, oid):
        if self.active():
            return False, "Finish or cancel the current job first"
        offer = self._find_offer(oid)
        if not offer:
            return False, "That job is gone: pick another from the board"
        ok, why = self.fits(offer)
        if not ok:
            return False, why
        if not self.connected():
            return False, "Start BeamNG and load BeamNG.Earth's world first"
        cargo = D.CARGO_BY_ID[offer["cargo"]]
        self.summary = None
        pts = [dict(offer["from"])] + [dict(s) for s in offer["stops"]]
        for i, p in enumerate(pts):
            p["i"] = i
            p["resolved"] = False
        self.job = {"id": offer["id"], "offer": offer, "pts": pts, "truck": dict(self.truck), "cargo_cat": cargo["id"],
                    "t0": time.time(), "stage": "Finding the pickup…", "idx": 1, "cond": 100.0, "mins": offer["mins"],
                    "jolts": 0, "crash": 0, "tickets": 0, "speeding_s": 0.0, "dist_m": 0.0, "max_kmh": 0.0,
                    "fuel0": self.tel.get("fuel"), "lost_s": 0.0, "rig": None, "log": [], "drop_times": []}
        self.state = "resolving"
        self.job["resolve_at"] = time.time()
        for p in pts:
            cats = cargo["src"] if p["i"] == 0 else cargo["dst"]
            self._ask(p, cats)
        self.note("ok", f"Job accepted: {offer['title']} ({offer['load']})")
        self.v += 1
        return True, ""

    def _ask(self, p, cats):
        self._q += 1
        qid = f"fq{self._q}"
        self.pending[qid] = (self.job["id"], p["i"])
        self.send([{"cmd": "places", "id": qid, "lat": p["lat"], "lon": p["lon"], "radius": 1800, "categories": cats,
                    "limit": 60, "cachedOnly": False}])

    def on_places(self, m):
        key = self.pending.get(m.get("id"))
        if not key:
            return False
        if m.get("complete") or not m.get("pending"):
            self.pending.pop(m.get("id"), None)
        j = self.job
        if not j or j["id"] != key[0] or self.state != "resolving":
            return True
        p = j["pts"][key[1]]
        if p["resolved"]:
            return True
        good = [x for x in m.get("places", []) if x.get("name") and x.get("open") != "permanently_closed"
                and (x.get("confidence") is None or x["confidence"] >= 0.6)]
        if good:
            pick = random.choice(good[:8])
            p.update(name=pick["name"], lat=pick["lat"], lon=pick["lon"], resolved=True, real=True)
        elif not m.get("pending"):
            p["resolved"] = True              # nothing there: the town's own point stands
        if all(x["resolved"] for x in j["pts"]):
            self._spawn()
        return True

    def _fallback(self):
        for p in self.job["pts"]:
            p["resolved"] = True
        self._spawn()

    def _spawn(self):
        j = self.job
        o = j["offer"]
        cargo = D.CARGO_BY_ID[o["cargo"]]
        cands = D.trailer_candidates(cargo, D.family(j["truck"]["model"])) if cargo["needs"] in ("tractor", "drawbar", "light") else []
        if cands:
            k = random.randrange(min(3, len(cands)))
            cands = cands[k:] + cands[:k] if k and cands[k]["family"] == cands[0]["family"] else cands
        first = j["pts"][0]
        self.state = "spawning"
        j["spawn_at"] = time.time()
        j["stage"] = "Moving the truck to the pickup…"
        self.send([{"cmd": "freightSpawn", "id": j["id"], "truck": {"model": j["truck"]["model"], "config": j["truck"]["config"]},
                    "at": {"lat": first["lat"], "lon": first["lon"], "name": first["name"]},
                    "heading": bearing((first["lat"], first["lon"]), (j["pts"][1]["lat"], j["pts"][1]["lon"])),
                    "trailers": cands, "needs": cargo["needs"], "load": o["load"]}])
        self.v += 1

    # ------------------------------------------------------------------ the game's replies
    def on_game(self, m):
        j = self.job
        ev = m.get("event")
        if not j or m.get("id") != j["id"]:
            return
        if ev == "stage":
            j["stage"] = m.get("text", j["stage"])
        elif ev == "ready" and self.state == "spawning":
            j["rig"] = {"trailer": m.get("trailer"), "hitch": m.get("hitch"), "coupled": bool(m.get("coupled")),
                        "trailer_name": m.get("trailerName")}
            j["started"] = time.time()
            self.state = "driving"
            j["stage"] = "Driving"
            tr = j["rig"]["trailer"]
            if tr and not j["rig"]["coupled"]:
                self.say(f"The {m.get('trailerName') or 'trailer'} is in place but not hooked up yet. "
                         "Back onto it and press the coupler button.", "warn", 9)
            self._leg_cmds(first=True)
            self.say(f"Job started: {j['offer']['load']} to {j['pts'][1]['name']}.", "ok")
        elif ev == "fail":
            self._fail(m.get("why") or "The truck could not be set up")

    def _fail(self, why):
        j = self.job
        self.note("bad", why)
        self.send([{"cmd": "notify", "text": why, "secs": 9}, {"cmd": "markers", "stops": []}, {"cmd": "gpsClear"}])
        self.state = "idle"
        self.job = None
        self.summary = {"failed": True, "why": why, "title": j["offer"]["title"] if j else ""}
        self.v += 1

    def _stop_pt(self):
        j = self.job
        return j["pts"][j["idx"]]

    def _leg_cmds(self, first=False):
        j = self.job
        p = self._stop_pt()
        last = j["idx"] == len(j["pts"]) - 1
        name = ("Drop-off: " if last else f"Stop {j['idx']}: ") + p["name"]
        marks = []
        for k in range(j["idx"], len(j["pts"])):
            q = j["pts"][k]
            marks.append({"lat": q["lat"], "lon": q["lon"], "name": q["name"], "kind": "dropoff", "next": k == j["idx"]})
        self.send([{"cmd": "gpsTo", "lat": p["lat"], "lon": p["lon"], "name": name}, {"cmd": "markers", "stops": marks}])
        j["await_route"] = True
        j["spot"] = None

    def on_route(self, route):
        """BeamNG.Earth planned a route: the road's own duration sets the time allowed, and where it ends beside the
        drop is where the truck has to stop."""
        j = self.job
        if not (j and self.state == "driving" and j.get("await_route")):
            return
        pts = route.get("points") or []
        if pts:
            p = self._stop_pt()
            end = pts[-1]
            for q in reversed(pts[:-1]):
                if dist_m((q[0], q[1]), (p["lat"], p["lon"])) >= 6:
                    end = q
                    break
            j["spot"] = (end[0], end[1])
        if route.get("dur") and j["idx"] == 1 and not j.get("est_set"):
            # first leg only: the road's own time replaces the estimate (a truck is slower than a car)
            j["est_set"] = True
            total = route["dur"] * 1.12
            rest = sum(dist_m((a["lat"], a["lon"]), (b["lat"], b["lon"])) for a, b in zip(j["pts"][1:], j["pts"][2:]))
            total += road_km(rest) / 70 * 3600 if rest else 0
            total = (total + 240 + 240 * (len(j["pts"]) - 2)) * 1.1
            j["mins"] = max(10, round(total / 60))
        j["await_route"] = False

    def on_hello(self):
        """The game says hello every 5 s. After a silence the game restarted: ask for the truck list, and put a running
        job's markers and route back."""
        now = time.time()
        restarted = now - self._hello_at > 12
        self._hello_at = now
        if (not self.models or now - self.models_at > 86400) and now - self._asked_models > 60:
            self._asked_models = now
            self.send([{"cmd": "vehicles"}])
        if restarted and self.job and self.state == "driving":
            self._leg_cmds()

    # ------------------------------------------------------------------ the driving loop (10 Hz)
    def update(self, m):
        now = time.time()
        dt = min(0.5, now - self.pose["_at"]) if self.pose else 0.1
        self.pose = m | {"_at": now}
        self._tel(m)
        if self.state == "resolving":
            if now - self.job["resolve_at"] > RESOLVE_S:
                self._fallback()
            return
        if self.state == "spawning":
            if now - self.job["spawn_at"] > SPAWN_S:
                self._fail("Timed out setting up the truck. Is BeamNG.Earth's world loaded?")
            return
        if self.state != "driving":
            return
        j = self.job
        me = (m["lat"], m["lon"])
        speed = m.get("speed", 0) or 0
        j["dist_m"] += speed * dt
        j["max_kmh"] = max(j["max_kmh"], speed * 3.6)
        o = j["offer"]
        # --- the load: hard moments, hits, a lost trailer
        g = math.hypot(m.get("gx", 0) or 0, m.get("gy", 0) or 0)
        lim_g = LIMIT["hazard"] if o["hazard"] else LIMIT["fragile"] if o["fragile"] >= 0.4 else LIMIT["normal"]
        j["_g_run"] = j.get("_g_run", 0) + 1 if g > lim_g else 0
        j["_c_run"] = j.get("_c_run", 0) + 1 if g > CRASH_HELD else 0
        sens = 0.6 + o["fragile"] + (0.6 if o["hazard"] else 0)
        if (g > CRASH or j["_c_run"] >= 2) and now - j.get("_g_at", 0) > 3:
            j["_g_at"], j["_g_run"] = now, 0
            j["crash"] += 1
            j["cond"] = max(0.0, j["cond"] - 6 * sens)
            self.say("Hard hit: the load took a knock.", "bad", 5, False)
        elif j["_g_run"] >= JOLT_HOLD and now - j.get("_g_at", 0) > JOLT_GAP_S:
            j["_g_at"], j["_g_run"] = now, 0
            j["jolts"] += 1
            if j["jolts"] > JOLTS_FREE:
                j["cond"] = max(0.0, j["cond"] - 1.6 * sens)
            if j["jolts"] in (3, 6, 10):
                self.say("Easy on the pedals: the load is shifting." if not o["hazard"] else "Careful: dangerous goods!", "warn", 4, False)
        dmg = m.get("dmg")
        if dmg is not None:
            if j.get("dmg0") is None:
                j["dmg0"] = dmg
            d = dmg - j["dmg0"]
            if d > j.get("_dmg_seen", 0) + 4e5:                # joules dissipated in deformation since the last check
                j["cond"] = max(0.0, j["cond"] - min(8.0, (d - j.get("_dmg_seen", 0)) / 4e5) * sens * 0.5)
                j["_dmg_seen"] = d
        rig = j["rig"] or {}
        if rig.get("trailer") and m.get("tr") is not None:
            if m["tr"] == 0:
                j["lost_s"] += dt
                if j["lost_s"] > LOST_WARN_S and not j.get("_lost_said"):
                    j["_lost_said"] = True
                    j["cond"] = max(0.0, j["cond"] - 10)
                    self.say("The trailer came unhooked!", "bad", 6)
                if j["lost_s"] > LOST_FAIL_S:
                    self._fail("You lost the trailer, so the job failed.")
                    return
            else:
                if j["lost_s"] > 0 and j.get("_lost_said"):
                    self.say("Trailer hooked up again.", "ok", 3, False)
                j["lost_s"], j["_lost_said"] = 0.0, False
        # --- speed limit
        lim = m.get("limit")
        if lim and speed * 3.6 > lim * 3.6 + SPEEDING_KMH:
            j["_over"] = j.get("_over", 0) + dt
            j["speeding_s"] += dt
            if j["_over"] > SPEEDING_S and not j.get("_ticketed"):
                j["_ticketed"] = True
                j["tickets"] += 1
                self.say(f"Speeding ticket: ${TICKET:.0f}.", "bad", 5, False)
        else:
            j["_over"], j["_ticketed"] = 0, False
        # --- time left
        left = j["mins"] * 60 - (now - j["started"])
        for mark, txt in ((600, "10 minutes left."), (120, "Two minutes left."), (0, "You are late: the pay drops now.")):
            if left <= mark and not j.get(f"_t{mark}") and (left > mark - 30 or mark == 0):
                j[f"_t{mark}"] = True
                self.say(txt, "warn", 4, mark != 600)
        # --- arrived at a drop?
        p = self._stop_pt()
        near = dist_m(me, (p["lat"], p["lon"])) < ARRIVE_M or (j.get("spot") and dist_m(me, j["spot"]) < ARRIVE_M)
        if near and speed < STOPPED_MS:
            j["drop_times"].append(round(now - j["started"]))
            if j["idx"] == len(j["pts"]) - 1:
                self._finish()
            else:
                self.say(f"Delivered at {p['name']}. Next: {j['pts'][j['idx'] + 1]['name']}.", "ok", 5)
                j["idx"] += 1
                self._leg_cmds()
            self.v += 1

    def _tel(self, m):
        """The live truck stats and control states the tablet shows."""
        has = m.get("has") or {}
        self.tel = {"speed": round((m.get("speed", 0) or 0) * 3.6), "limit": round(m["limit"] * 3.6) if m.get("limit") else None,
                    "rpm": m.get("rpm"), "gear": m.get("gear"), "fuel": m.get("fuel"), "fuelL": m.get("fuelL"),
                    "fuelCap": m.get("fuelCap"), "water": m.get("wT"), "oil": m.get("oT"), "odo": m.get("odo"),
                    "trip": m.get("trip"), "pb": m.get("pb"), "load": m.get("load"), "eng": m.get("eng"),
                    "mass": m.get("mass"), "tr": m.get("tr"), "boost": m.get("boost"), "check": m.get("chk"),
                    "lowfuel": m.get("lowf"), "model": m.get("model"), "heading": m.get("heading"),
                    # the control panel: which controls this truck has, and where they are set
                    "has": has, "dm": m.get("dm") or {}, "ret": m.get("ret"), "retOn": m.get("retOn"),
                    "retS": m.get("retS"), "cb": m.get("cb"), "cbOn": m.get("cbOn"), "cc": m.get("cc"),
                    "ccT": round(m["ccT"] * 3.6) if m.get("ccT") else None, "pto": m.get("ptoRpm"),
                    "raised": m.get("raised"), "mix": m.get("mix")}

    # ------------------------------------------------------------------ finishing
    def _score(self, j):
        o = j["offer"]
        now = time.time()
        mins = (now - j["started"]) / 60
        cond = j["cond"]
        cf = max(0.3, min(1.0, cond / 90))
        if mins <= j["mins"]:
            tf = 1 + 0.1 * (j["mins"] - mins) / j["mins"]
        else:
            tf = max(0.5, 1 - 0.6 * (mins - j["mins"]) / j["mins"])
        fines = TICKET * j["tickets"]
        pay = max(o["pay"] * 0.1, o["pay"] * cf * tf - fines)
        stars = 5.0
        stars -= max(0.0, (90 - cond) / 22)
        stars -= 0 if mins <= j["mins"] else min(2.0, 2.0 * (mins - j["mins"]) / j["mins"])
        stars -= 0.4 * j["tickets"]
        stars -= 0.5 if j.get("_lost_said") else 0
        return {"base": o["pay"], "cond_f": round(cf, 2), "time_f": round(tf, 2), "fines": fines, "pay": round(pay, 2),
                "stars": round(max(1.0, min(5.0, stars)), 1), "mins": round(mins), "late": mins > j["mins"]}

    def _finish(self):
        j = self.job
        o = j["offer"]
        s = self._score(j)
        fuel_l = 0.0
        f0, f1, cap = j.get("fuel0"), self.tel.get("fuel"), self.tel.get("fuelCap")
        if f0 is not None and f1 is not None and cap and f0 >= f1:
            fuel_l = round((f0 - f1) * cap, 1)
        km_driven = round(j["dist_m"] / 1000, 1)
        st = self.stats
        st["balance"] = round(st["balance"] + s["pay"], 2)
        st["jobs"] += 1
        st["km"] = round(st["km"] + km_driven, 1)
        st["tonne_km"] = round(st["tonne_km"] + o["weight"] * o["km"], 1)
        st["late"] += 1 if s["late"] else 0
        st["best"] = max(st.get("best", 0), s["pay"])
        st["fuel_l"] = round(st.get("fuel_l", 0) + fuel_l, 1)
        entry = {"t": round(time.time()), "title": o["title"], "icon": o["icon"], "load": o["load"], "from": j["pts"][0]["name"],
                 "to": j["pts"][-1]["name"], "stops": len(j["pts"]) - 1, "km": o["km"], "km_driven": km_driven,
                 "pay": s["pay"], "stars": s["stars"], "cond": round(j["cond"]), "mins": s["mins"], "late": s["late"],
                 "truck": j["truck"].get("name"), "label": j["truck"].get("label"), "weight": o["weight"], "fuel_l": fuel_l,
                 "avg_kmh": round(km_driven / max(0.05, (time.time() - j["started"]) / 3600), 1), "max_kmh": round(j["max_kmh"])}
        st["history"] = ([entry] + st["history"])[:150]
        self.summary = {"job": entry, "score": s, "tickets": j["tickets"], "jolts": j["jolts"], "crashes": j["crash"],
                        "title": o["title"], "drops": j["drop_times"]}
        self.state = "done"
        self.send([{"cmd": "markers", "stops": []}, {"cmd": "gpsClear"},
                   {"cmd": "notify", "text": f"Delivered! +${s['pay']:.2f} ({s['stars']}★)", "secs": 7}])
        self.announce(f"Delivered. You earned {s['pay']:.0f} dollars.", "app")
        self.note("ok", f"Delivered {o['title']}: ${s['pay']:.2f}")
        self._save()
        self.v += 1

    # ------------------------------------------------------------------ the console's actions
    def act(self, body):
        a = body.get("action")
        if a == "truck":
            return self.select_truck(body.get("model"), body.get("config"))
        if a == "override":
            self.set_override(body.get("model"), body.get("config"), body.get("caps") or [])
            return True, ""
        if a == "favourite":
            m = body.get("model")
            if m:
                (self.favourites.remove if m in self.favourites else self.favourites.append)(m)
                self.v += 1
                self._save()
            return True, ""
        if a == "market":
            return self.set_market(body.get("hub"), refresh=bool(body.get("refresh")))
        if a == "plan":
            ok, res = self.plan(body)
            return (True, res) if ok else (False, res)
        if a == "start":
            return self.start(body.get("offer"))
        if a == "cancel":
            return self.cancel()
        if a == "dismiss":
            self.dismiss()
            return True, ""
        if a == "recouple":
            self.send([{"cmd": "freightRecouple"}])
            return True, ""
        if a == "trucks":                                 # ask the game for its list again
            self.send([{"cmd": "vehicles"}])
            return True, ""
        return False, "unknown action"

    def cancel(self):
        if not self.active():
            return False, "No job to cancel"
        title = self.job["offer"]["title"]
        self.send([{"cmd": "markers", "stops": []}, {"cmd": "gpsClear"}])
        self.note("warn", f"Cancelled {title}")
        self.state = "idle"
        self.job = None
        self.summary = None
        self.v += 1
        return True, ""

    def dismiss(self):
        """Close the summary; a finished job's trailer is unhooked and removed."""
        if self.state == "done":
            self.send([{"cmd": "freightClear"}])
        self.state = "idle"
        self.job = None
        self.summary = None
        self.v += 1

    # ------------------------------------------------------------------ what the console sees
    def _job_view(self):
        j = self.job
        if not j:
            return None
        o = j["offer"]
        out = {"id": j["id"], "title": o["title"], "icon": o["icon"], "load": o["load"], "weight": o["weight"],
               "needs": o["needs"], "fragile": o["fragile"], "hazard": o["hazard"], "km": o["km"], "stage": j["stage"],
               "from": j["pts"][0]["name"], "stops": [{"name": p["name"], "lat": p["lat"], "lon": p["lon"], "done": k < j["idx"], "now": k == j["idx"] and self.state == "driving"}
                                                      for k, p in enumerate(j["pts"]) if k > 0],
               "idx": j["idx"], "cond": round(j["cond"]), "jolts": j["jolts"], "tickets": j["tickets"], "mins": j["mins"],
               "truck": j["truck"], "rig": j["rig"], "trailer": o.get("trailer"), "pay": o["pay"], "crash": j["crash"]}
        if self.state == "driving":
            now = time.time()
            out["elapsed"] = round(now - j["started"])
            out["left"] = round(j["mins"] * 60 - (now - j["started"]))
            p = self._stop_pt()
            gl = (self.pose or {}).get("gpsLeft")
            me = self._me()
            out["dist_left_km"] = round((gl if gl else dist_m(me, (p["lat"], p["lon"])) * 1.25) / 1000, 1) if me else None
            out["driven_km"] = round(j["dist_m"] / 1000, 1)
            out["max_kmh"] = round(j["max_kmh"])
            out["avg_kmh"] = round(j["dist_m"] / 1000 / max(0.02, (now - j["started"]) / 3600), 1)
            out["lost_s"] = round(j["lost_s"])
            f0, f1, cap = j.get("fuel0"), self.tel.get("fuel"), self.tel.get("fuelCap")
            out["fuel_used_l"] = round((f0 - f1) * cap, 1) if (f0 is not None and f1 is not None and cap and f0 >= f1) else None
            out["stop"] = {"name": p["name"], "lat": p["lat"], "lon": p["lon"]}
        return out

    def view(self):
        """Small and cheap: part of every /api/state."""
        return {"state": self.state, "connected": self.connected(), "truck": self.truck, "job": self._job_view(),
                "summary": self.summary, "tel": self.tel if self.connected() else {}, "v": self.v,
                "hub": self.market["hub"], "balance": self.stats["balance"], "jobs": self.stats["jobs"],
                "events": self.events[-6:], "garage": len(self.models), "caps": sorted(self.truck_caps())}

    def board(self):
        """Everything for the board, the garage and the history (fetched when the version changes)."""
        if not self.market["hub"] or time.time() - self.market["built"] > 900:
            self.set_market(self.market["hub"])
        offers = []
        for o in self.market["offers"] + list(self.custom.values())[-6:]:
            ok, why = self.fits(o)
            offers.append(o | {"fits": ok, "why": why})
        offers.sort(key=lambda o: (not o["fits"], {"scenario": 0, "custom": 0, "local": 1, "regional": 2, "long": 3}[o["kind"]], -o["pay"]))
        me = self._me()
        near = sorted(D.HUBS, key=lambda h: dist_m(me, (h[1], h[2])))[:1] if me else []
        return {"v": self.v, "hub": self.market["hub"], "offers": offers, "garage": self.garage(), "truck": self.truck,
                "caps": sorted(self.truck_caps()), "stats": {k: v for k, v in self.stats.items() if k != "history"},
                "history": self.stats["history"][:60], "towns": [{"name": h[0], "tags": h[3].split()} for h in D.HUBS],
                "cargo": [{"id": c["id"], "name": c["name"], "icon": c["icon"], "needs": c["needs"],
                           "fits": bool(self.truck) and D.can_carry(self.truck_caps(), c)} for c in D.CARGO],
                "needs": NEED_LABEL, "corridor": D.CORRIDOR, "here": near[0][0] if near else None, "models_at": self.models_at}
