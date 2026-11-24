"""Freight: trucking jobs on BeamNG.Earth (or any map). The tablet is the dispatcher: a Euro Truck-style job board, a
garage to pick the truck, and a driving screen with live truck stats. Lives in the Real Routes helper, which feeds it
poses and the game's answers and sends its commands on.

States: idle (browsing the board) -> resolving (finding real places for the pickup and drops) -> spawning (the mod
puts the truck near the pickup) -> to_pickup (drive there) -> loading (stand still while the trailer is hooked on or the
load appears on the deck) -> driving (deliver every drop, standing still at each while it is unloaded) -> done (pay, rating).
"""
import json, math, os, random, re, time
import freight_data as D

STOPPED_MS = 1.5                   # m/s: counts as stopped at a drop
LOAD_S = (6, 16)                   # loading at the pickup takes this long (more for a heavy load), standing still
UNLOAD_S = 4                       # standing still at a drop while it is unloaded
STAGE_M = (1500, 2600)             # the truck is set down this far from the pickup (the drive to it is part of the job)
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
EXTRA_BASE_MIN, EXTRA_PER_KM, EXTRA_MAX_MIN = 60, 0.2, 60   # on top of the driving time: an hour, up to two on a long haul


def extra_min(km, contract=None):
    """Spare time added to every job so a stop for fuel, a wrong turn or a break does not make it late. An express
    contract keeps half of it, since being quick is what it pays for."""
    extra = EXTRA_BASE_MIN + min(EXTRA_MAX_MIN, km * EXTRA_PER_KM)
    return round(extra * (0.5 if contract == "rush" else 1.0))
BASE_FEE, RATE_X = 150, 1.6        # pay: a base fee plus km x the cargo's rate x RATE_X, scaled by weight and bonuses
MARKET_TARGET, MARKET_MAX = 18, 28 # regular jobs kept on the board (new ones appear over time, old ones leave)
NEW_EVERY_S = (40, 90)             # a new random job appears this often
LIFE_S = (600, 1800)               # how long a regular job stays on the board
CONTRACTS = {"rush": {"title": "Express", "mins": 0.72, "pay": 1.35, "label": "Express contract +35%"},
             "valuable": {"title": "High-value", "frag": 0.35, "pay": 1.45, "label": "High-value cargo +45%"}}
TAILORED = 0.45                    # share of generated jobs made for the selected truck (the rest are general)
TAILORED_MIN = 6                   # a fresh board always has at least this many
MOD_REQUIRED = 8                   # the oldest BeamDeck mod (realroutes.lua MOD_VERSION) this helper works with
LOAD_WAIT_S = 12
CHAIN_PAY, CHAIN_STEP_PAY, CHAIN_LIFE_S = 1.25, 0.10, 2 * 3600   # a chain job pays 25% more, 10% more again for each link after
CONVOY_BONUS = 0.05                  # extra pay per other driver in the convoy on the same job ...
CONVOY_MAX_EXTRA = 4                 # ... for up to this many                   # after the loading time, how long to wait for the game's answer before going on
MODELS_V = 2                       # version of the truck list the mod sends (2: key parts and the config's source)
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


def load_kind(load, needs):
    """Which of the truck's own built-in loads suits this job (the mod picks the part nearest the job's weight):
    a rigid flatbed carries pipes, wood, a marble block or storage boxes; a box body carries pallets; a tipper rocks;
    a tanker its liquid. A trailer or a mixer carries its load itself ("none")."""
    q = (load or "").lower()
    if needs == "flatbed":
        if "pipe" in q:
            return "pipes"
        if any(w in q for w in ("timber", "lumber", "wood", "plank")):
            return "wood"
        if "marble" in q:
            return "marble"
        return "boxes"
    if needs == "box":
        return "pallets"
    if needs == "dump":
        return "rocks"
    if needs == "tanker":
        for words, fluid in ((("diesel", "heating"), "diesel"), (("petrol", "fuel", "gas"), "petroleum"), (("milk",), "milk"), (("water",), "water")):
            if any(w in q for w in words):
                return "tanker:" + fluid
        return "tanker:dynamic"
    return "none"


class Freight:
    def __init__(self, send, data_dir, announce=None):
        # a regular map (stock or mod, not BeamNG.Earth): jobs run between the map's own places (see on_hello)
        self.flat, self.level, self.level_title, self.sites, self._sites_src = False, None, None, [], None
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
        self.staging = True                  # start away from the pickup and drive to it (off: start at the pickup)
        self.tickets = True                  # traffic offenses / speeding tickets (like ETS2/ATS: off = no fines or tickets)
        import previews
        self.previews = previews.Previews()  # the pictures of the trucks (the game's own preview images)
        self.mp = None                       # freight_mp.MP when multiplayer is wired up by the helper
        self.lock = None                     # the helper's lock: hub calls are made outside it, a job start inside it
        self.on_start = None                 # called (under the lock) before a convoy job starts, e.g. to end Rides
        self.player = ""                     # the BeamMP nickname the game reported (the default multiplayer name)
        self.damage = True                   # cargo damage lowers the pay and stars (off = damage is free)
        self.late = True                     # late delivery lowers the pay and stars (off = no time penalty)
        try:
            saved = json.load(open(self.file))
            self.stats.update(saved.get("stats", {}))
            self.truck = saved.get("truck")
            self.overrides = saved.get("overrides", {})
            self.models = saved.get("models", [])
            self.models_at = saved.get("models_at", 0)
            self.favourites = saved.get("favourites", self.favourites)
            self.staging = saved.get("staging", True)
            self.tickets = saved.get("tickets", True)
            old = saved.get("penalties", True)               # the earlier single switch covered both
            self.damage = saved.get("damage", old)
            self.late = saved.get("late", old)
            if saved.get("models_v") != MODELS_V:           # an older list lacks the parts and custom flags: ask again
                self.models_at = 0
        except Exception:
            pass
        self.state = "idle"
        self.job = None
        self.summary = None
        self.pose = None
        self.tel = {}
        self.market = {"hub": None, "offers": [], "built": 0, "n": 0, "at": None, "next_new": 0}
        self.custom = {}                     # offers the player planned: id -> offer
        self.events = []
        self.v = 1                           # bumped when the board's contents change, so the console refetches it
        self._q = 0
        self.pending = {}                    # places request id -> (job id, stop index)
        self._asked_models = 0
        self._hello_at = 0.0
        self.mod_ver = None                  # what the running game's mod says it is (None: not heard yet)
        self._born = time.time()
        self._models_part = []

    # ------------------------------------------------------------------ plumbing
    def _save(self):
        try:
            json.dump({"stats": self.stats, "truck": self.truck, "overrides": self.overrides, "models": self.models,
                       "models_at": self.models_at, "favourites": self.favourites, "models_v": MODELS_V,
                       "staging": self.staging, "tickets": self.tickets, "damage": self.damage, "late": self.late}, open(self.file + ".part", "w"))
            os.replace(self.file + ".part", self.file)
        except OSError:
            pass

    def active(self):
        return self.state in ("resolving", "spawning", "to_pickup", "loading", "driving")

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

    def _config(self, model, key):
        for md in self.models:
            if md["model"] == model:
                c = next((c for c in md.get("configs", []) if c["key"] == key), None)
                if c:
                    return c, md
        return None, None

    def caps_of(self, model, key, name="", mtype="Truck"):
        ov = self.overrides.get(f"{model}/{key}")
        if ov is not None:
            return set(ov)
        c, _ = self._config(model, key)
        return D.classify(model, key, name, mtype, c.get("parts") if c else None)

    def garage(self):
        out = []
        for md in self.models:
            for c in md.get("configs", []):
                custom = (c.get("source") or "") == "Custom"
                has_override = f"{md['model']}/{c['key']}" in self.overrides
                caps = self.caps_of(md["model"], c["key"], c.get("name", ""), md.get("type", "Truck"))
                # the player's own saved configs always appear, even when nothing says what they are (set it by hand)
                if not caps and not (custom or has_override):
                    continue
                if not caps and D.is_bus_like(c.get("parts")) and not has_override:
                    continue
                out.append({"model": md["model"], "key": c["key"], "brand": md.get("brand", ""), "name": md.get("name", ""),
                            "label": c.get("name") or c["key"], "caps": sorted(caps), "family": D.family(md["model"]),
                            "fav": md["model"] in self.favourites, "specs": c.get("specs", {}), "custom": custom,
                            "source": c.get("source") or "", "override": has_override})
        out.sort(key=lambda t: (not t["fav"], t["brand"], t["name"], not t["custom"], t["label"].lower()))
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
        self.market["offers"] = []                  # rebuilt on the next look, filled with jobs this truck can carry
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
        self.market["offers"] = []
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
        """(Re)build the market for a town: when the town changes, on request, when the truck changed (so the board is
        filled with jobs it can carry), or when the board is old."""
        if self.flat:                                        # a regular map: one market, the map's own places
            hub = self.level_title or "This map"
        elif hub and hub not in D.HUB:
            return False, "Unknown town"
        if not hub:
            me = self._me()
            hub = self.market["hub"] or (self._near_hub(*me) if me else "Ottawa")
        if refresh:
            self.market["n"] += 1
        stale = time.time() - self.market["built"] > 900
        if hub != self.market["hub"] or refresh or stale or not self.market["offers"]:
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

    def _make_offer(self, rng, cargo, origin, stops, kind, scen=None, pay_mult=1.0, title=None, blurb=None, mins_mult=1.0,
                    contract=None):
        now = time.time()
        pts = [origin] + stops
        straight = sum(dist_m((a["lat"], a["lon"]), (b["lat"], b["lon"])) for a, b in zip(pts, pts[1:]))
        km = max(road_km(straight), 3.0)
        speed = 36 if km < 30 else 58 if km < 150 else 76
        mins = (km / speed * 60 + 10 + 4 * len(stops)) * SLACK * mins_mult
        fragile, rush = cargo["fragile"], bool(cargo.get("rush"))
        con = CONTRACTS.get(contract)
        if con:
            mins *= con.get("mins", 1.0)
            fragile = min(0.9, fragile + con.get("frag", 0.0))
            pay_mult *= con["pay"]
            rush = rush or contract == "rush"
        mins = max(12, round(mins)) + extra_min(km, contract)
        trailer = None
        for fam in ("us", "eu"):
            if (cargo.get("trailers") or {}).get(fam):
                trailer = cargo["trailers"][fam][0]
                break
        weight = rng.uniform(*cargo["weight"])
        if trailer:
            mt = re.search(r"(\d{4,5})kg", trailer[1])
            if mt:
                weight = int(mt.group(1)) / 1000
        weight = round(weight, 1)
        pay = (BASE_FEE + km * cargo["rate"] * RATE_X) * (0.9 + weight / 40) * pay_mult
        if km >= 300:
            pay *= 1.15
        pay = round5(pay)
        ttl = None if kind in ("scenario", "custom") else rng.uniform(*LIFE_S)
        return {"id": f"o{rng.getrandbits(32):08x}", "cargo": cargo["id"], "scenario": scen, "icon": cargo["icon"],
                "title": title or (f"{con['title']} {cargo['name'].lower()}" if con else cargo["name"]), "load": rng.choice(cargo["loads"]),
                "kind": kind, "from": origin, "stops": stops, "km": round(km), "mins": mins, "pay": pay, "weight": weight,
                "needs": cargo["needs"], "fragile": fragile, "hazard": bool(cargo.get("hazard")), "rush": rush,
                "blurb": blurb or "", "trailer": trailer[1] if trailer else None, "contract": contract,
                "contract_label": con["label"] if con else None, "created": now, "expires": now + ttl if ttl else None}

    def _regular(self, rng, hubname, kind=None, tailored=None):
        """One random job round a market town: inside the town, to a town nearby, or a long haul. Some start in a
        neighbouring town, and some carry a contract (express or high-value) that pays more for more risk. A tailored
        job is made for the selected truck (cargo it is built to carry); the others are general and mix in."""
        kind = kind or rng.choices(("local", "regional", "long"), (0.3, 0.45, 0.25))[0]
        me = self.truck_caps()
        here = (D.HUB[hubname]["lat"], D.HUB[hubname]["lon"])
        if tailored is None:
            tailored = bool(me) and rng.random() < TAILORED
        made_for = []

        def pick(local=False, origin_hub=hubname):
            pool = [c for c in D.CARGO if not (c.get("port") and "port" not in D.HUB[origin_hub]["tags"])]
            if local:
                pool = [c for c in pool if c.get("local")] or pool
            fit = [c for c in pool if D.can_carry(me, c)] if me else []
            if tailored and fit:
                made_for.append(True)
                return rng.choice(fit)
            return rng.choice(fit) if fit and rng.random() < 0.3 else rng.choice(pool)

        contract = rng.choice(("rush", "valuable")) if rng.random() < 0.3 else None
        if kind == "local":
            c = pick(local=True)
            n = rng.randint(*c["stops"]) if c.get("stops") else 1
            stops = [self._local_pt(rng, hubname, rng.choice(["customer", "site", "yard"])) for _ in range(n)]
            o = self._make_offer(rng, c, self._local_pt(rng, hubname, "depot"), stops, "local", contract=contract)
            o["tailored"] = bool(made_for)
            return o
        origin_hub = hubname
        if rng.random() < 0.4:
            near = [h[0] for h in D.HUBS if h[0] != hubname and dist_m(here, (h[1], h[2])) / 1000 * 1.25 <= 90]
            if near:
                origin_hub = rng.choice(near)
        oh = D.HUB[origin_hub]
        ohere = (oh["lat"], oh["lon"])
        lo, hi = (30, 160) if kind == "regional" else (160, 800)
        cands = [h[0] for h in D.HUBS if h[0] != origin_hub and lo <= dist_m(ohere, (h[1], h[2])) / 1000 * 1.25 <= hi]
        if not cands:
            return None
        c = pick(origin_hub=origin_hub)
        o = self._make_offer(rng, c, self._hub_pt(origin_hub), [self._hub_pt(rng.choice(cands))], kind, contract=contract)
        o["tailored"] = bool(made_for)
        return o

    def _build(self, hubname):
        if self.flat:
            return self._level_build()
        rng = random.Random(f"{hubname}|{int(time.time() // 900)}|{self.market['n']}|{random.random()}")
        origin = self._hub_pt(hubname)
        offers = []
        for kind, n in (("local", 5), ("regional", 8), ("long", 5)):
            for _ in range(n):
                o = self._regular(rng, hubname, kind)
                if o:
                    offers.append(o)
        if self.truck_caps():                                 # make sure a fresh board has plenty made for this truck
            tries = 0
            while sum(1 for o in offers if o.get("tailored")) < TAILORED_MIN and tries < 40:
                tries += 1
                o = self._regular(rng, hubname, tailored=True)
                if o and not any(x["cargo"] == o["cargo"] and x["stops"][-1]["name"] == o["stops"][-1]["name"] for x in offers):
                    offers.append(o)
        # themed scenarios
        here = (origin["lat"], origin["lon"])
        hubs = sorted((h for h in D.HUBS if h[0] != hubname), key=lambda h: dist_m(here, (h[1], h[2])))
        by = lambda lo, hi: [h[0] for h in hubs if lo <= dist_m(here, (h[1], h[2])) / 1000 * 1.25 <= hi]
        for sc in D.SCENARIOS:
            o = self._scenario(rng, sc, hubname, origin, by)
            if o:
                offers.append(o)
        self.market["next_new"] = time.time() + rng.uniform(*NEW_EVERY_S)
        return offers

    def _market_tick(self):
        """The market is alive: jobs leave when their time is up and new random ones turn up every minute or so."""
        m = self.market
        if not m["hub"] or not m["offers"]:
            return
        now = time.time()
        keep = [o for o in m["offers"] if not o.get("expires") or o["expires"] > now]
        changed = len(keep) != len(m["offers"])
        if now >= m["next_new"]:
            rng = random.Random(f"{m['hub']}|{now}|{len(keep)}")
            regular = [o for o in keep if o["kind"] != "scenario"]
            for _ in range(1 if len(regular) >= MARKET_TARGET else 3):
                o = self._level_offer(rng) if self.flat else self._regular(rng, m["hub"])
                if o:
                    keep.append(o)
            m["next_new"] = now + rng.uniform(*NEW_EVERY_S)
            changed = True
        regular = [o for o in keep if o["kind"] != "scenario"]
        if len(regular) > MARKET_MAX:                       # the oldest regular jobs go first
            drop = {o["id"] for o in sorted(regular, key=lambda o: o["created"])[:len(regular) - MARKET_MAX]}
            keep = [o for o in keep if o["id"] not in drop]
            changed = True
        if changed:
            m["offers"] = keep
            self.v += 1

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
        if to:
            if sc.get("via"):
                stops = [self._hub_pt(v) for v in self._along(frm, to, sc["via"], rng)]
            stops.append(self._hub_pt(to))
        elif cargo.get("local") or sc.get("stops"):
            n = rng.randint(*sc["stops"]) if sc.get("stops") else 1
            stops = [self._local_pt(rng, hubname, rng.choice(["customer", "site", "yard"])) for _ in range(n)]
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
        if self.flat:
            return self._level_plan(frm, to, body)
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
        return (self.custom.get(oid) or next((o for o in self.market["offers"] if o["id"] == oid), None)
                or next((o for o in self._shared() if o["id"] == oid), None))

    # ------------------------------------------------------------------ regular maps (stock and mod maps)
    # The game mod lists the map's own places (realroutes.lua exportFreightSites): BeamNG's delivery businesses where the
    # map has them (warehouses, shops, fuel stations, trailer yards, with what they ship and take), its big-map points
    # of interest, and spots spread over its roads. Jobs run between those; distances are the map's own (a few km).
    LEVEL_KINDS = {            # BeamNG's logistic types / site groups -> the tags cargo is matched on
        "industrial": "warehouse industrial", "mechanicalParts": "warehouse industrial", "parcel": "warehouse",
        "shopSupplies": "shop", "foodSupplies": "food shop", "officeSupplies": "office shop",
        "warehouses": "warehouse", "shops": "shop", "restaurants": "food", "fuelStations": "fuel", "mechanics": "industrial",
        "offices": "office", "residential": "home", "mixed": "shop", "gasStation": "fuel", "garage": "industrial",
        "dealership": "shop", "road": "warehouse yard",
    }
    PATTERN_TAGS = [           # cargo src/dst place patterns (BeamNG.Earth categories) -> those tags
        (("warehouse", "wholesale", "storage", "manufactur", "shipping", "port", "harbo", "transport"), {"warehouse", "yard"}),
        (("industrial",), {"industrial", "warehouse", "yard"}),
        (("construction", "building_supply", "hardware", "quarr", "mining", "gravel", "sawmill", "lumber", "wood", "forest"),
         {"site", "industrial", "yard"}),
        (("shop", "grocery", "supermarket", "department", "food", "farm", "dairy", "agricultur"), {"shop", "food", "office"}),
        (("fuel", "gas_station"), {"fuel"}),
    ]

    def _level_hello(self, m):
        flat = bool(m.get("flat"))
        if not flat:
            if self.flat:
                self.flat, self.level, self.sites, self._sites_src = False, None, [], None
                self.market["hub"] = None
            return
        path = m.get("freightSites")
        if not path or path == self._sites_src and m.get("level") == self.level:
            return
        try:
            d = json.load(open(path, encoding="utf-8"))
        except (OSError, ValueError):
            return
        sites = []
        for x in d.get("sites") or []:
            try:
                lat, lon = float(x["lat"]), float(x["lon"])
            except (KeyError, TypeError, ValueError):
                continue
            tags = set()
            for k in [str(x.get("group") or "")] + [str(t) for t in x.get("types") or []]:
                tags |= set(self.LEVEL_KINDS.get(k, "").split())
                if k.startswith(("trailerDelivery", "vehLargeTruck")):      # trailer yards and big-truck spots
                    tags |= {"yard", "warehouse"}
                if "Construction" in k:
                    tags |= {"site"}
            name = str(x.get("name") or "").strip()
            if x.get("source") == "road" and any(w in name for w in ("Industrial", "Works")):
                tags |= {"industrial", "site"}
            try:
                z = float(x.get("z"))
            except (TypeError, ValueError):
                z = None
            sites.append({"name": name or "Yard", "lat": lat, "lon": lon, "z": z, "tags": tags or {"place"},
                          "gives": x.get("gives", True), "takes": x.get("takes", True), "source": x.get("source")})
        self.flat, self.level, self.level_title, self._sites_src = True, d.get("level") or m.get("level"), d.get("title"), path
        self.sites = sites
        self.market["hub"] = None                       # rebuilt for this map on the next look at the board
        self.v += 1

    def _site_pt(self, x):
        return {"name": x["name"], "lat": x["lat"], "lon": x["lon"], "hub": self.level_title}

    def _matches(self, site, pats):
        want = set()
        for words, tags in self.PATTERN_TAGS:
            if any(w in str(p).lower() for p in pats for w in words):
                want |= tags
        return bool(site["tags"] & want)

    @staticmethod
    def _map_mins(o, mult=1.0):
        """A map job's clock: town driving speed over its few km, a few minutes a stop (the highway formula would give
        an hour for 3 km)."""
        stops = len(o.get("stops") or [])
        o["mins"] = max(6, round((o["km"] / 28 * 60 + 4 + 2 * stops) * mult))
        return o

    def _level_offer(self, rng, tailored=None):
        """One job between two (or more) of the map's places. Cargo with a fitting pickup and drop-off first."""
        if len(self.sites) < 2:
            return None
        me = self.truck_caps()
        if tailored is None:
            tailored = bool(me) and rng.random() < TAILORED
        pool = [c for c in D.CARGO if not c.get("port") or any("yard" in x["tags"] for x in self.sites)]
        fit = [c for c in pool if D.can_carry(me, c)] if me else []
        c = rng.choice(fit) if (tailored and fit) or (fit and rng.random() < 0.3) else rng.choice(pool)
        src = [x for x in self.sites if x["gives"] and self._matches(x, c["src"])] or [x for x in self.sites if x["gives"]] or self.sites
        origin = rng.choice(src)
        n = rng.randint(*c["stops"]) if c.get("stops") else 1
        n = max(1, min(n, 3, len(self.sites) - 1))
        dst = [x for x in self.sites if x is not origin and x["takes"] and self._matches(x, c["dst"])]
        rest = [x for x in self.sites if x is not origin and x not in dst]
        stops, last = [], origin
        for _ in range(n):
            cand = [x for x in dst + rest if x not in stops and dist_m((last["lat"], last["lon"]), (x["lat"], x["lon"])) >= 250]
            if not cand:
                break
            good = [x for x in cand if x in dst]
            pick = rng.choice(good or cand)                # a fitting drop-off whenever the map has one
            stops.append(pick)
            last = pick
        if not stops:
            return None
        contract = rng.choice(("rush", "valuable")) if rng.random() < 0.3 else None
        o = self._make_offer(rng, c, self._site_pt(origin), [self._site_pt(x) for x in stops], "local", contract=contract)
        o["map"], o["tailored"] = self.level, bool(tailored and fit)
        return self._map_mins(o, CONTRACTS[contract].get("mins", 1.0) if contract in CONTRACTS else 1.0)

    def _level_build(self):
        rng = random.Random(f"{self.level}|{int(time.time() // 900)}|{self.market['n']}|{random.random()}")
        offers = [o for o in (self._level_offer(rng) for _ in range(14)) if o]
        offers += self._level_challenges(rng)
        tries = 0
        while self.truck_caps() and sum(1 for o in offers if o.get("tailored")) < TAILORED_MIN and tries < 30:
            tries += 1
            o = self._level_offer(rng, tailored=True)
            if o and o["tailored"]:
                offers.append(o)
        self.market["next_new"] = time.time() + rng.uniform(*NEW_EVERY_S)
        return offers

    # the stock maps' character, for the challenge jobs' names (a mod map gets plain names)
    MAP_FLAVOUR = {"west_coast_usa": "Coastal hills", "utah": "Canyon country", "italy": "Mountain pass",
                   "johnson_valley": "Desert", "east_coast_usa": "Back roads", "jungle_rock_island": "Jungle island",
                   "small_island": "Island", "industrial": "Industrial site", "cliff": "Cliff road", "gridmap_v2": "Grid map",
                   "automation_test_track": "Test track", "hirochi_raceway": "Raceway", "derby": "Derby arena"}
    CHALLENGE_CARGO = ("steel", "pipes", "logs", "gravel", "concrete", "lumber", "container", "dryvan")

    def _level_challenges(self, rng):
        """Hard jobs from the map's own lie of the land: the biggest climb, the steepest descent (brakes and retarder)
        and the longest haul across the map, each with a heavy load and a tighter clock, paying more."""
        pts = [x for x in self.sites if x.get("z") is not None]
        if len(pts) < 3:
            return []
        me = self.truck_caps()
        heavy = [D.CARGO_BY_ID[c] for c in self.CHALLENGE_CARGO if c in D.CARGO_BY_ID]
        fit = [c for c in heavy if D.can_carry(me, c)] if me else []
        flavour = self.MAP_FLAVOUR.get(str(self.level).lower())
        pairs = []
        for a in pts:
            for b in pts:
                if a is b:
                    continue
                d = dist_m((a["lat"], a["lon"]), (b["lat"], b["lon"]))
                if d >= 600:
                    pairs.append((a, b, d, b["z"] - a["z"]))
        if not pairs:
            return []
        span = max(p[2] for p in pairs)
        picks = []
        climb = max(pairs, key=lambda p: p[3])
        if climb[3] >= 40:
            picks.append(("climb", climb))
        rest = [p for p in pairs if not (p[0] is climb[1] and p[1] is climb[0])]   # not the climb driven backwards
        drop = min(rest or pairs, key=lambda p: p[3])
        if drop[3] <= -40:
            picks.append(("descent", drop))
        far = max(pairs, key=lambda p: p[2])
        if far[2] >= span * 0.8:
            picks.append(("across", far))
        out = []
        for kind, (a, b, d, dz) in picks:
            c = rng.choice(fit or heavy)
            if kind == "climb":
                title = ("Mountain climb" if dz >= 150 else "Hill climb")
                blurb = f"Climb {round(dz)} m from {a['name']} to {b['name']} fully loaded. Low gears, and keep an eye on the engine."
            elif kind == "descent":
                title = "Steep descent"
                blurb = f"Down {round(-dz)} m from {a['name']} to {b['name']} with a heavy load. Use the retarder and engine brake, not just the brakes."
            else:
                title = "Across the map"
                blurb = f"From {a['name']} to {b['name']}, the longest haul this map has: {round(d / 1000, 1)} km in a straight line."
            if flavour:
                title = f"{flavour}: {title.lower()}"
            o = self._make_offer(rng, c, self._site_pt(a), [self._site_pt(b)], "scenario", f"map_{kind}", 1.6, title, blurb, 0.85)
            o["map"], o["challenge"] = self.level, kind
            self._map_mins(o, 0.85)
            o["weight"] = round(max(o["weight"], c["weight"][1]), 1)       # the heaviest this cargo comes
            out.append(o)
        return out

    def _stage_site(self, first):
        """Where the truck is set down before the pickup on a regular map: another of its places, 350 m to 2 km away
        (on the map's roads, unlike a random point that may be in a field or the sea)."""
        here = (first["lat"], first["lon"])
        near = [x for x in self.sites if 350 <= dist_m(here, (x["lat"], x["lon"])) <= 2000]
        if not near:
            near = sorted((x for x in self.sites if dist_m(here, (x["lat"], x["lon"])) >= 150),
                          key=lambda x: dist_m(here, (x["lat"], x["lon"])))[:1]
        return random.choice(near) if near else None

    def _level_chain(self, rng, o, c, last):
        here = (last["lat"], last["lon"])
        cands = [x for x in self.sites if dist_m(here, (x["lat"], x["lon"])) >= 300]
        good = [x for x in cands if self._matches(x, c["dst"])] or cands
        if not good:
            return None
        prev = o.get("chain") or {}
        step = prev.get("step", 1) + 1
        who = self.mp.my_name() if (self.mp and self.mp.enabled()) else "You"
        mult = CHAIN_PAY + CHAIN_STEP_PAY * (step - 2)
        gave = D.CARGO_BY_ID[o["cargo"]]["name"].lower()
        to = rng.choice(good)
        text = f"{who} delivered {gave} to {last['name']}. {c['name']} is ready there to go on to {to['name']}, {round((mult - 1) * 100)}% more."
        new = self._make_offer(rng, c, {"name": last["name"], "lat": last["lat"], "lon": last["lon"], "hub": self.level_title},
                               [self._site_pt(to)], "local", pay_mult=mult, title=f"Supply chain: {c['name']}", blurb=text)
        new["expires"] = time.time() + CHAIN_LIFE_S
        new["map"] = self.level
        self._map_mins(new)
        new["chain"] = {"id": prev.get("id") or o["id"], "step": step, "from": who, "gave": gave, "text": text}
        return new

    def _level_plan(self, frm, to, body):
        by = {x["name"]: x for x in self.sites}
        via = [by[v] for v in (body.get("via") or []) if v in by]
        if frm not in by or to not in by:
            return False, "Pick the places to start and finish at"
        if frm == to and not via:
            return False, "Pick two different places"
        cargo = D.CARGO_BY_ID.get(body.get("cargo"))
        if not cargo:
            return False, "Pick a cargo"
        rng = random.Random(f"{frm}{to}{time.time()}")
        o = self._make_offer(rng, cargo, self._site_pt(by[frm]), [self._site_pt(x) for x in via] + [self._site_pt(by[to])],
                             "custom", None, 1.0, f"{frm} → {to}", "Your own route" + (f" via {', '.join(x['name'] for x in via)}" if via else ""))
        o["map"] = self.level
        self._map_mins(o)
        self.custom[o["id"]] = o
        return True, o

    # ------------------------------------------------------------------ starting a job
    def start(self, oid):
        if self.active():
            return False, "Finish or cancel the current job first"
        offer = self._find_offer(oid)
        if not offer:
            return False, "That job is gone: pick another from the board"
        if not self.connected():
            return False, "Start BeamNG and load a map (or BeamNG.Earth's world) first"
        if self.mod_old():
            return False, "The game is running an older BeamDeck mod. Press Ctrl+L in BeamNG (or restart it), then take the job again."
        ok, why = self.fits(offer)
        if not ok:
            return False, why
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
        if offer.get("map"):                              # a regular map's job: its places are already exact
            if offer["map"] != self.level:
                self.job, self.state = None, "idle"
                return False, "That job is on another map. Load that map, or take a job from this one."
            for p in pts:
                p["resolved"] = True
            self._spawn()
        else:
            for p in pts:
                cats = cargo["src"] if p["i"] == 0 else cargo["dst"]
                self._ask(p, cats)
        self.note("ok", f"Job accepted: {offer['title']} ({offer['load']})")
        if self.mp:
            self.mp.share(offer)                          # friends see it on the shared board and can take the same job
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
        j["cands"], j["load_kind"] = cands, load_kind(o["load"], cargo["needs"])
        first = j["pts"][0]
        at = {"lat": first["lat"], "lon": first["lon"], "name": first["name"]}
        j["staged"] = bool(self.staging)
        if self.staging:                                       # set down short of the pickup: the drive there is part of the job
            near = self._stage_site(first) if o.get("map") else None
            if near:                                           # a regular map: another of its places, on its roads
                at = {"lat": near["lat"], "lon": near["lon"], "name": near["name"]}
            else:
                la, lo = offset((first["lat"], first["lon"]), random.uniform(0, 360), random.uniform(*STAGE_M))
                at = {"lat": la, "lon": lo, "name": "Near " + first["name"]}
        self.state = "spawning"
        j["spawn_at"] = time.time()
        j["stage"] = "Moving the truck…"
        self.send([{"cmd": "freightSpawn", "id": j["id"], "truck": {"model": j["truck"]["model"], "config": j["truck"]["config"]},
                    "at": at, "heading": bearing((at["lat"], at["lon"]), (first["lat"], first["lon"])),
                    "trailers": cands, "needs": cargo["needs"], "load": o["load"], "deferLoad": True}])
        self.v += 1

    # ------------------------------------------------------------------ the game's replies
    def on_game(self, m):
        j = self.job
        ev = m.get("event")
        if not j or m.get("id") != j["id"]:
            return
        if ev == "stage":
            j["stage"] = m.get("text", j["stage"])
        elif ev == "ready" and self.state == "spawning":              # the truck is in place (no load yet)
            if j.get("staged"):
                self.state = "to_pickup"
                j["stage"] = "Drive to the pickup"
                self._pickup_cmds()
                self.say(f"Job accepted: {j['offer']['load']}. Drive to the pickup: {j['pts'][0]['name']}.", "ok")
            else:
                self._begin_loading()
        elif ev == "loaded" and self.state in ("loading", "driving"):  # the trailer is hooked on / the load is on the deck
            j["rig"] = {"trailer": m.get("trailer"), "hitch": m.get("hitch"), "coupled": bool(m.get("coupled")),
                        "trailer_name": m.get("trailerName"), "load_name": m.get("loadName"), "load_kg": m.get("loadKg")}
            if m.get("loadKg"):
                j["weight_actual"] = round(m["loadKg"] / 1000, 1)       # the weight of the load part the game put on
            j["loaded_event"] = True
            if m.get("noLoad") and j.get("load_kind") != "none":
                self.say("This truck has no built-in load for that cargo: carry on as if it were loaded.", "warn", 7, False)
            if m.get("trailer") and not m.get("coupled"):
                self.say(f"The {m.get('trailerName') or 'trailer'} is in place but not hooked up yet. "
                         "Back onto it and press the coupler button.", "warn", 9)
        elif ev == "fail":
            self._fail(m.get("why") or "The truck could not be set up")

    def _pickup_cmds(self):
        j = self.job
        p = j["pts"][0]
        self.send([{"cmd": "gpsTo", "lat": p["lat"], "lon": p["lon"], "name": "Pickup: " + p["name"]},
                   {"cmd": "markers", "stops": [{"lat": p["lat"], "lon": p["lon"], "name": p["name"], "kind": "pickup", "next": True}]}])
        j["await_route"], j["spot"] = True, None

    def _begin_loading(self):
        j = self.job
        o = j["offer"]
        self.state = "loading"
        j["load_s"] = min(LOAD_S[1], LOAD_S[0] + o["weight"] * 0.4)
        j["load_left"], j["load_at"], j["loaded_event"] = j["load_s"], time.time(), False
        j["stage"] = "Loading: stay stopped"
        cargo = D.CARGO_BY_ID[o["cargo"]]
        self.send([{"cmd": "gpsClear"}, {"cmd": "freightLoad", "id": j["id"], "trailers": j["cands"], "kind": j["load_kind"],
                                           "weightKg": round(o["weight"] * 1000), "needs": cargo["needs"], "load": o["load"]}])
        self.say(f"Loading {o['load']}. Stay stopped.", "ok", 6)
        self.v += 1

    def _start_driving(self):
        j = self.job
        self.state = "driving"
        j["started"] = time.time()
        j["stage"] = "Driving"
        self._leg_cmds(first=True)
        self.say(f"Loaded. Deliver to {j['pts'][1]['name']}.", "ok")
        self.v += 1

    def _fail(self, why):
        j = self.job
        self.note("bad", why)
        self.send([{"cmd": "notify", "text": why, "secs": 9}, {"cmd": "markers", "stops": []}, {"cmd": "gpsClear"}, {"cmd": "freightClear"}])
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
        if not (j and self.state in ("to_pickup", "driving") and j.get("await_route")):
            return
        pts = route.get("points") or []
        if pts:
            p = j["pts"][0] if self.state == "to_pickup" else self._stop_pt()
            end = pts[-1]
            for q in reversed(pts[:-1]):
                if dist_m((q[0], q[1]), (p["lat"], p["lon"])) >= 6:
                    end = q
                    break
            j["spot"] = (end[0], end[1])
        if self.state == "to_pickup":
            j["await_route"] = False
            return
        if route.get("dur") and j["idx"] == 1 and not j.get("est_set"):
            # first leg only: the road's own time replaces the estimate (a truck is slower than a car)
            j["est_set"] = True
            total = route["dur"] * 1.12
            rest = sum(dist_m((a["lat"], a["lon"]), (b["lat"], b["lon"])) for a, b in zip(j["pts"][1:], j["pts"][2:]))
            total += road_km(rest) / 70 * 3600 if rest else 0
            total = (total + 240 + 240 * (len(j["pts"]) - 2)) * 1.1
            j["mins"] = max(10, round(total / 60)) + extra_min(j["offer"]["km"], j["offer"].get("contract"))
        j["await_route"] = False

    def mod_old(self):
        """True when the game is running an older copy of the mod than this helper needs (reload it with Ctrl+L). Right
        after the helper starts the version is not known yet, which only counts as old after a short while."""
        if not self.connected():
            return False
        if self.mod_ver is not None:
            return self.mod_ver < MOD_REQUIRED
        return time.time() - max(self._hello_at, self._born) > 12

    def on_hello(self, m=None):
        """The game says hello every 5 s. After a silence the game restarted: ask for the truck list, and put a running
        job's markers and route back."""
        now = time.time()
        if m is not None:
            self.mod_ver = int(m.get("rr", 0) or 0)
            self.player = str(m.get("player") or self.player or "")[:20]
            if m.get("gamePath") or m.get("userPath"):
                self.previews.set_paths(m.get("gamePath"), m.get("userPath"))
            self._level_hello(m)
        restarted = now - self._hello_at > 12
        self._hello_at = now
        if (not self.models or now - self.models_at > 86400) and now - self._asked_models > 60:
            self._asked_models = now
            self.send([{"cmd": "vehicles"}])
        if restarted and self.job and self.state == "driving":
            self._leg_cmds()
        elif restarted and self.job and self.state == "to_pickup":
            self._pickup_cmds()

    # ------------------------------------------------------------------ the driving loop (10 Hz)
    def update(self, m):
        now = time.time()
        gap = now - self.pose["_at"] if self.pose else 0.0
        dt = min(0.5, gap) if self.pose else 0.1
        paused = bool(m.get("paused"))
        if paused:
            dt = 0.0
        self.pose = m | {"_at": now}
        self._tel(m)
        if self.job and self.state == "driving" and self.job.get("started"):
            # the job's clock follows the game: it stands still while the game is paused or the game is not talking
            self.job["started"] += max(0.0, gap - (0.0 if paused else min(gap, 0.5)))
        if self.state == "resolving":
            if now - self.job["resolve_at"] > RESOLVE_S:
                self._fallback()
            return
        if self.state == "spawning":
            if now - self.job["spawn_at"] > SPAWN_S:
                self._fail("Timed out setting up the truck. Is BeamNG.Earth's world loaded?")
            return
        j = self.job
        if self.state in ("to_pickup", "loading"):
            me = (m["lat"], m["lon"])
            speed = m.get("speed", 0) or 0
            if self.state == "to_pickup":
                p = j["pts"][0]
                near = dist_m(me, (p["lat"], p["lon"])) < ARRIVE_M or (j.get("spot") and dist_m(me, j["spot"]) < ARRIVE_M)
                if near and speed < STOPPED_MS:
                    self._begin_loading()
                return
            if speed < STOPPED_MS:
                j["load_left"] -= dt
            elif now - j.get("_move_say", 0) > 10:
                j["_move_say"] = now
                self.say("Stay stopped while it loads.", "warn", 4, False)
            if j["load_left"] <= 0 and not j.get("loaded_event"):
                j["stage"] = ("Waiting for the game: it runs an older BeamDeck mod, press Ctrl+L in BeamNG"
                              if self.mod_old() else "Waiting for the game to finish loading…")
            if j["load_left"] <= 0 and (j.get("loaded_event") or now - j["load_at"] > j["load_s"] + LOAD_WAIT_S):
                if not j.get("loaded_event"):
                    self.say("The game did not confirm the load" + (" (an older mod is running: press Ctrl+L)" if self.mod_old() else "")
                             + ". Hook up or load by hand if needed.", "warn", 8)
                self._start_driving()
            return
        if self.state != "driving":
            return
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
        if self.tickets and lim and speed * 3.6 > lim * 3.6 + SPEEDING_KMH:
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
        for mark, txt in ((600, "10 minutes left."), (120, "Two minutes left."),
                          (0, "You are late: the pay drops now." if self.late else "You are past scheduled time.")):
            if left <= mark and not j.get(f"_t{mark}") and (left > mark - 30 or mark == 0):
                j[f"_t{mark}"] = True
                self.say(txt, "warn" if mark else ("bad" if self.late else "warn"), 4, mark != 600)
        # --- arrived at a drop?
        p = self._stop_pt()
        near = dist_m(me, (p["lat"], p["lon"])) < ARRIVE_M or (j.get("spot") and dist_m(me, j["spot"]) < ARRIVE_M)
        if near and speed < STOPPED_MS:
            j["svc"] = j.get("svc", 0.0) + dt                  # standing still while the load is taken off
            if j["svc"] >= UNLOAD_S:
                j["svc"] = 0.0
                j["drop_times"].append(round(now - j["started"]))
                if j["idx"] == len(j["pts"]) - 1:
                    self._finish()
                else:
                    self.say(f"Delivered at {p['name']}. Next: {j['pts'][j['idx'] + 1]['name']}.", "ok", 5)
                    j["idx"] += 1
                    self._leg_cmds()
                self.v += 1
        elif not near or speed > STOPPED_MS * 2:
            j["svc"] = 0.0

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
                    "has": has, "dm": m.get("dm") or {}, "ret": m.get("ret"), "retL": m.get("retL"), "retA": m.get("retA"),
                    "retAuto": m.get("retAuto"), "cb": m.get("cb"), "cbL": m.get("cbL"), "cbOn": m.get("cbOn"), "cc": m.get("cc"),
                    "ccT": round(m["ccT"] * 3.6) if m.get("ccT") else None, "pto": m.get("ptoRpm"),
                    "raised": m.get("raised"), "mix": m.get("mix"), "paused": bool(m.get("paused"))}

    # ------------------------------------------------------------------ finishing
    def _score(self, j):
        o = j["offer"]
        now = time.time()
        mins = (now - j["started"]) / 60
        cond = j["cond"]
        cf = max(0.3, min(1.0, cond / 90)) if self.damage else 1.0
        if mins <= j["mins"]:
            tf = 1 + 0.1 * (j["mins"] - mins) / j["mins"]                 # the early bonus stays whatever the options
        else:
            tf = max(0.5, 1 - 0.6 * (mins - j["mins"]) / j["mins"]) if self.late else 1.0

        fines = (TICKET * j["tickets"]) if self.tickets else 0.0
        conv_n = self.mp.convoy_size_on(o["id"]) if self.mp else 0
        conv_f = 1 + CONVOY_BONUS * min(CONVOY_MAX_EXTRA, max(0, conv_n - 1))   # hauling the same job together pays more
        pay = max(o["pay"] * 0.1, o["pay"] * cf * tf * conv_f - fines)
        stars = 5.0
        if self.damage:
            stars -= max(0.0, (90 - cond) / 22)
        if self.late:
            stars -= 0 if mins <= j["mins"] else min(2.0, 2.0 * (mins - j["mins"]) / j["mins"])
        if self.late or self.damage:
            stars -= 0.5 if j.get("_lost_said") else 0
        if self.tickets:
            stars -= 0.4 * j["tickets"]
        return {"base": o["pay"], "cond_f": round(cf, 2), "time_f": round(tf, 2), "convoy_n": conv_n, "convoy_f": round(conv_f, 2), "fines": fines, "pay": round(pay, 2),
                "stars": round(max(1.0, min(5.0, stars)), 1), "mins": round(mins), "late": mins > j["mins"]}

    def _chain_offer(self, j):
        """A delivery becomes the next job: the goods stay at the drop-off and are ready to go on. The new job starts at that
        drop-off, pays more the longer the chain, and is for anyone to take (on the shared board when multiplayer is on)."""
        o = j["offer"]
        nxt = D.CHAINS.get(o["cargo"])
        c = D.CARGO_BY_ID.get(nxt) if nxt else None
        if not c or not j.get("pts"):
            return None
        last = j["pts"][-1]
        here = (last["lat"], last["lon"])
        rng = random.Random(f"chain|{o['id']}|{time.time()}")
        if o.get("map"):
            return self._level_chain(rng, o, c, last)
        base = min(D.HUBS, key=lambda h: dist_m(here, (h[1], h[2])))[0]
        cands = [h[0] for h in D.HUBS if h[0] != base and 30 <= dist_m(here, (h[1], h[2])) / 1000 * 1.25 <= 160]
        if c.get("local") and (not cands or rng.random() < 0.4):
            stops, kind = [self._local_pt(rng, base, rng.choice(["customer", "site", "yard"]))], "local"
        elif cands:
            stops, kind = [self._hub_pt(rng.choice(cands))], "regional"
        else:
            return None
        prev = o.get("chain") or {}
        step = prev.get("step", 1) + 1
        who = self.mp.my_name() if (self.mp and self.mp.enabled()) else "You"
        mult = CHAIN_PAY + CHAIN_STEP_PAY * (step - 2)
        gave = D.CARGO_BY_ID[o["cargo"]]["name"].lower()
        text = f"{who} delivered {gave} to {last['name']}. {c['name']} is ready there to go on to {stops[-1]['name']}, {round((mult - 1) * 100)}% more."
        origin = {"name": last["name"], "lat": last["lat"], "lon": last["lon"], "hub": base}
        new = self._make_offer(rng, c, origin, stops, kind, pay_mult=mult, title=f"Supply chain: {c['name']}", blurb=text)
        new["expires"] = time.time() + CHAIN_LIFE_S
        new["chain"] = {"id": prev.get("id") or o["id"], "step": step, "from": who, "gave": gave, "text": text}
        return new

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
        if getattr(self, "earn", None):                       # CareerMP: this hour's payslip, as the trucker job
            self.earn("trucker", s["pay"], o["title"])
        st["jobs"] += 1
        st["km"] = round(st["km"] + km_driven, 1)
        st["tonne_km"] = round(st["tonne_km"] + o["weight"] * o["km"], 1)
        st["late"] += 1 if s["late"] else 0
        st["best"] = max(st.get("best", 0), s["pay"])
        st["fuel_l"] = round(st.get("fuel_l", 0) + fuel_l, 1)
        entry = {"t": round(time.time()), "title": o["title"], "icon": o["icon"], "load": o["load"], "from": j["pts"][0]["name"],
                 "to": j["pts"][-1]["name"], "stops": len(j["pts"]) - 1, "km": o["km"], "km_driven": km_driven,
                 "pay": s["pay"], "stars": s["stars"], "cond": round(j["cond"]), "mins": s["mins"], "late": s["late"],
                 "truck": j["truck"].get("name"), "label": j["truck"].get("label"), "weight": j.get("weight_actual") or o["weight"], "fuel_l": fuel_l,
                 "avg_kmh": round(km_driven / max(0.05, (time.time() - j["started"]) / 3600), 1), "max_kmh": round(j["max_kmh"])}
        st["history"] = ([entry] + st["history"])[:150]
        self.summary = {"job": entry, "score": s, "tickets": j["tickets"], "jolts": j["jolts"], "crashes": j["crash"],
                        "title": o["title"], "drops": j["drop_times"],
                        "tickets_on": self.tickets, "damage_on": self.damage, "late_on": self.late}
        self.state = "done"
        self.send([{"cmd": "markers", "stops": []}, {"cmd": "gpsClear"},
                   {"cmd": "notify", "text": f"Delivered! +${s['pay']:.2f} ({s['stars']}★)", "secs": 7}])
        self.announce(f"Delivered. You earned {s['pay']:.0f} dollars.", "app")
        if self.mp:
            self.mp.done(s["pay"], km_driven or o["km"], s["stars"], o["title"], s.get("convoy_n", 0))
        chain = self._chain_offer(j)
        if chain:
            if self.mp and self.mp.enabled():
                self.mp.share(chain)                          # on the shared board: whoever takes it carries the chain on
            else:
                self.market["offers"].append(chain)
            self.note("ok", chain["chain"]["text"])
        self.note("ok", f"Delivered {o['title']}: ${s['pay']:.2f}")
        self._save()
        self.v += 1

    # ------------------------------------------------------------------ multiplayer
    def mp_state(self):
        """What the hub is told every couple of seconds: where this player is and what they are hauling."""
        out = {"truck": {"name": self.truck.get("name", ""), "label": self.truck.get("label", "")} if self.truck else {}}
        p = self.pose or {}
        if self.connected() and p.get("lat") is not None and time.time() - p.get("_at", 0) < 5:
            out |= {"lat": p["lat"], "lon": p["lon"], "hdg": p.get("heading") or 0, "kmh": round((p.get("speed") or 0) * 3.6)}
        j = self.job
        if j and self.active():
            o = j["offer"]
            out["job"] = {"title": o["title"], "phase": self.state, "offer": o["id"], "to": j["pts"][-1]["name"],
                          "pct": 0 if self.state in ("resolving", "spawning", "to_pickup", "loading")
                          else min(99, round(j["dist_m"] / 1000 / max(1, o["km"]) * 100))}
        return out

    SHARED_NEED = ("id", "cargo", "title", "icon", "load", "kind", "from", "stops", "km", "mins", "pay", "weight", "needs")

    def _shared(self):
        """The hub's jobs that this BeamDeck can run: a job from a player on other data (an unknown cargo, missing
        fields) is left out instead of breaking the board."""
        if not self.mp:
            return []
        out = []
        for o in self.mp.offers():
            if o.get("map") != (self.level if self.flat else None):      # a job on another map than this player's
                continue
            if o.get("cargo") in D.CARGO_BY_ID and all(k in o for k in self.SHARED_NEED) and isinstance(o["needs"], str):
                out.append({"fragile": 0.0, "hazard": False, "rush": False, "blurb": "", "trailer": None, "contract": None,
                            "contract_label": None, "scenario": None, "created": time.time(), "expires": None} | o)
        return out

    def make_shared(self, n):
        """Fresh general jobs for the shared board (not kept in this player's own market)."""
        hub = self.market["hub"] or ("Ottawa" if not self._me() else self._near_hub(*self._me()))
        rng = random.Random(f"shared|{time.time()}|{hub}")
        made, tries = [], 0
        while len(made) < n and tries < n * 5:             # a roll can come up empty: try again for the number asked for
            tries += 1
            o = self._regular(rng, hub)                    # a mix: some made for this driver's truck, so every kind of truck gets work
            if o:
                o["tailored"] = False                       # "made for your truck" would be wrong on another driver's board
                made.append(o)
        return made

    def _locked(self):
        import contextlib
        return self.lock or contextlib.nullcontext()

    def _start_for_convoy(self, oid):
        if self.active():
            return False, "Finish or cancel the current job first"
        if self.on_start:
            self.on_start()
        return self.start(oid)

    def _mp_act(self, a, body):
        if not self.mp:
            return False, "Multiplayer isn't available"
        if a == "mp_chat":
            return self.mp.say(str(body.get("text") or ""), bool(body.get("convoy")))
        if a == "mp_route":                               # a GPS route to a friend, to meet up before a convoy
            if body.get("clear"):
                self.send([{"cmd": "gpsClear"}])
                return True, ""
            if self.active():
                return False, "Your job's route is on the GPS: finish or cancel the job first"
            if not self.connected():
                return False, "Start BeamNG and load a map (or BeamNG.Earth's world) first"
            p = next((p for p in self.mp.view().get("players", []) if p["name"] == body.get("name") and not p.get("me")), None)
            if not p or p.get("lat") is None:
                return False, "That driver's position isn't known yet"
            self.send([{"cmd": "gpsTo", "lat": p["lat"], "lon": p["lon"], "name": "Meet " + p["name"]}])
            return True, ""
        if a == "mp_leave":
            ok, msg = self.mp.convoy_leave()
            self.v += 1
            return ok, msg
        if a == "mp_create":                              # open a convoy on a job and take it
            offer = self._find_offer(body.get("offer"))
            if not offer:
                return False, "That job is gone: pick another from the board"
            with self._locked():
                ok, why = self.fits(offer)
            if not ok:
                return False, why
            ok, r = self.mp.convoy_create(offer)          # (the hub call: not under the lock)
            if not ok:
                return False, r
            with self._locked():
                ok, why = self._start_for_convoy(offer["id"])
            if not ok:
                self.mp.convoy_leave()                    # no orphan convoy when the job could not start
                return False, why
            return True, {"code": r["code"]}
        if a == "mp_join":                                # join a convoy by its code and take its job
            ok, r = self.mp.convoy_join(str(body.get("code") or ""))
            if not ok:
                return False, r
            offer = r.get("offer")
            if not offer:
                return False, "That convoy's job is gone"
            with self._locked():
                ok, why = self._start_for_convoy(offer["id"]) if self._find_offer(offer["id"]) else (
                    False, "The job hasn't reached your board yet: try again in a moment")
            return (True, {"code": r["code"]}) if ok else (False, "You joined convoy " + r["code"] + ", but can't take its job: " + why)
        return False, "unknown action"

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
        if a in ("mp_create", "mp_join", "mp_leave", "mp_chat", "mp_route"):
            return self._mp_act(a, body)
        if a == "cancel":
            return self.cancel()
        if a == "dismiss":
            self.dismiss()
            return True, ""
        if a == "recouple":
            self.send([{"cmd": "freightRecouple"}])
            return True, ""
        if a == "skip_loading":
            if self.state != "loading":
                return False, "Nothing is loading"
            self._start_driving()
            return True, ""
        if a == "skip_to_pickup":
            if self.state != "to_pickup":
                return False, "Not on the way to a pickup"
            p = self.job["pts"][0]
            self.send([{"cmd": "freightGoto", "lat": p["lat"], "lon": p["lon"], "name": p["name"]}])
            return True, ""
        if a == "staging":
            self.staging = bool(body.get("on"))
            self.v += 1
            self._save()
            return True, ""
        if a in ("tickets", "damage", "late"):            # the rules, each on its own like ETS2 / ATS
            setattr(self, a, bool(body.get("on")))
            self.v += 1
            self._save()
            return True, ""
        if a == "trucks":                                 # ask the game for its list again
            self.send([{"cmd": "vehicles"}])
            return True, ""
        return False, "unknown action"

    def cancel(self):
        if not self.active():
            return False, "No job to cancel"
        title = self.job["offer"]["title"]
        self.send([{"cmd": "markers", "stops": []}, {"cmd": "gpsClear"}, {"cmd": "freightClear"}])
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
        out = {"id": j["id"], "title": o["title"], "icon": o["icon"], "load": o["load"], "weight": j.get("weight_actual") or o["weight"],
               "needs": o["needs"], "fragile": o["fragile"], "hazard": o["hazard"], "km": o["km"], "stage": j["stage"],
               "from": j["pts"][0]["name"], "stops": [{"name": p["name"], "lat": p["lat"], "lon": p["lon"], "done": k < j["idx"], "now": k == j["idx"] and self.state == "driving"}
                                                      for k, p in enumerate(j["pts"]) if k > 0],
               "idx": j["idx"], "phase": self.state, "cond": round(j["cond"]), "jolts": j["jolts"], "tickets": j["tickets"], "mins": j["mins"],
               "truck": j["truck"], "rig": j["rig"], "trailer": o.get("trailer"), "pay": o["pay"], "crash": j["crash"]}
        if self.state == "to_pickup":
            p = j["pts"][0]
            gl = (self.pose or {}).get("gpsLeft")
            me = self._me()
            out["pickup"] = {"name": p["name"], "lat": p["lat"], "lon": p["lon"],
                             "dist_km": round((gl if gl else dist_m(me, (p["lat"], p["lon"])) * 1.25) / 1000, 1) if me else None}
        if self.state == "loading":
            out["load_pct"] = round(max(0.0, min(1.0, 1 - j["load_left"] / j["load_s"])) * 100)
            out["load_s"] = round(max(0, j["load_left"]))
        if self.state == "driving":
            now = time.time()
            out["unload_pct"] = round(min(1.0, j.get("svc", 0.0) / UNLOAD_S) * 100) if j.get("svc") else 0
            out["elapsed"] = round(now - j["started"])
            out["left"] = round(j["mins"] * 60 - (now - j["started"]))
            out["paused"] = bool(self.tel.get("paused"))
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
        self._market_tick()
        return {"state": self.state, "connected": self.connected(), "truck": self.truck, "job": self._job_view(),
                "summary": self.summary, "tel": self.tel if self.connected() else {}, "v": self.v,
                "hub": self.market["hub"], "balance": self.stats["balance"], "jobs": self.stats["jobs"],
                "staging": self.staging, "tickets_on": self.tickets, "damage_on": self.damage, "late_on": self.late,
                "mod_old": self.mod_old(), "mod_ver": self.mod_ver,
                "events": self.events[-6:], "garage": len(self.models), "caps": sorted(self.truck_caps())}

    def board(self):
        """Everything for the board, the garage and the history (fetched when the version changes)."""
        self.set_market(self.market["hub"])
        offers = []
        garage = self.garage()
        shared = self._shared()                                    # jobs every player can take (multiplayer)
        have = {o["id"] for o in self.market["offers"]}
        for o in self.market["offers"] + list(self.custom.values())[-6:] + [s for s in shared if s["id"] not in have]:
            ok, why = self.fits(o)
            alts = []
            if not ok and garage:
                cargo = D.CARGO_BY_ID[o["cargo"]]
                alts = [g for g in garage if D.can_carry(set(g["caps"]), cargo)]
            offers.append(o | {"fits": ok, "why": why, "alts": {"n": len(alts), "top": [
                {"model": g["model"], "key": g["key"], "name": g["name"], "label": g["label"], "custom": g["custom"]} for g in alts[:3]]}})
        offers.sort(key=lambda o: (not o["fits"], {"scenario": 0, "custom": 0, "local": 1, "regional": 2, "long": 3}[o["kind"]], -o["pay"]))
        me = self._me()
        near = sorted(D.HUBS, key=lambda h: dist_m(me, (h[1], h[2])))[:1] if me else []
        return {"v": self.v, "now": time.time(), "hub": self.market["hub"], "offers": offers, "garage": garage, "truck": self.truck,
                "caps": sorted(self.truck_caps()), "stats": {k: v for k, v in self.stats.items() if k != "history"},
                "history": self.stats["history"][:60],
                "towns": ([{"name": x["name"], "tags": sorted(x["tags"])} for x in self.sites] if self.flat
                          else [{"name": h[0], "tags": h[3].split()} for h in D.HUBS]),
                "map": {"id": self.level, "title": self.level_title, "places": len(self.sites)} if self.flat else None,
                "cargo": [{"id": c["id"], "name": c["name"], "icon": c["icon"], "needs": c["needs"],
                           "fits": bool(self.truck) and D.can_carry(self.truck_caps(), c)} for c in D.CARGO],
                "needs": NEED_LABEL, "corridor": [] if self.flat else D.CORRIDOR,
                "here": None if self.flat else (near[0][0] if near else None), "models_at": self.models_at}
