"""Rides: a ride-hailing driver job on BeamNG.Earth. Real places (from BeamNG.Earth's Overture data) are the
pickups and destinations; the tablet is the driver app. Lives in the Real Routes helper, which feeds it poses and
places answers from the game and sends its commands on.

States: offline -> online (waiting for a request) -> offered (a request card, accept within OFFER_S)
        -> to_pickup -> boarding (stopped at the pickup) -> on_trip -> done (fare, rating) -> online ...
"""
import json, math, os, random, re, time
import people

OFFER_S = 20
PICKUP_M, STOPPED_MS = 40, 1.0
BOARD_S = (4, 9)
REQUEST_EVERY = (12, 40)           # seconds between requests while online
PICKUP_R, DEST_MIN, DEST_MAX = 1800, 1500, 9000
# m/s² horizontal. A rider minds really hard braking, cornering or acceleration (about 0.75 g; 0.55 g when carsick
# or carrying something fragile) held for half a second, not a bump or a brisk corner; a crash is a real hit
# (3 g), or 2 g that lasts. Riders in a hurry don't mind rough driving at all, unless they have something fragile.
JOLT, JOLT_GENTLE, CRASH, CRASH_HELD = 7.5, 5.5, 30.0, 20.0
JOLT_HOLD = 5                      # samples in a row (10 a second): one spike from a kerb or pothole is not a jolt
JOLT_GAP_S = 6                     # hard moments closer together than this count once
JOLTS_FREE = 3                     # the first few hard moments in a trip are forgiven
JOLT_COST = 0.3                    # stars off the smoothness score for each one after that
SPEEDING_KMH = 25                  # over the posted limit: a little over doesn't count
SPEEDING_GRACE_S = 6               # ...and only when it lasts
SPEEDING_STAR_S = 45               # seconds of real speeding per star off the safety score
BASE, PER_KM, PER_MIN, MIN_FARE = 2.50, 1.15, 0.32, 6.00
LONG_CHANCE, LONG_MIN, LONG_MAX = 0.12, 9000, 20000   # long trips: across town, paid a little extra
LONG_BONUS, AIRPORT_BONUS = 1.15, 6.00
AIRPORT = re.compile(r"\b(airport|aéroport|airfield)\b", re.I)
NIGHT_HOURS = (22, 23, 0, 1, 2, 3)                     # late-night surge (bars closing, last trains gone)
CHANGE_CHANCE = 0.08                                    # riders who change their mind on the way
EMERGENCY_BONUS = 1.25                                  # priority fare for an emergency
HOSPITAL = re.compile(r"\b(hospital|h[ôo]pital|emergency|urgent care|clinic)\b", re.I)

PICKUP_CATS = ["group:food", "group:shop", "group:lodging", "group:culture", "group:education", "group:health",
               "group:transit", "group:park"]
DEST_CATS = PICKUP_CATS
MOODS = [
    (None, 0.55),
    ({"note": "In a hurry: running late", "hurry": True}, 0.14),
    ({"note": "Please drive gently, feeling carsick", "gentle": True}, 0.10),
    ({"note": "Carrying something fragile: smooth driving, please", "fragile": True}, 0.06),
    ({"note": "In a hurry, with something fragile on board", "hurry": True, "fragile": True}, 0.03),
    # an emergency: only the time counts. Rough driving and speed are fine (a crash is not)
    ({"note": "Emergency: get there as fast as you can", "hurry": True, "emergency": True}, 0.03),
    ({"note": "Has a lot of bags", "board": 6}, 0.08),
    ({"note": "First time riding, be nice", "tip": 1.3}, 0.10),
]


def dist_m(a, b):
    la = math.radians((a[0] + b[0]) / 2)
    return math.hypot((a[0] - b[0]) * 110574, (a[1] - b[1]) * 111320 * math.cos(la))


def pretty_cat(c):
    return (c or "place").replace("_", " ")


class Rides:
    def __init__(self, send, data_dir, announce=None):
        self.send = send                     # send(list of game commands)
        self.announce = announce or (lambda text, kind="pa", who=None: None)   # spoken on the tablet
        self.session = None                  # this time online: earnings, rides, stars...
        self.summary = None                  # the last session's summary, until the driver closes it
        self.file = os.path.join(data_dir, "rides.json")
        self.avatar_dir = os.path.join(data_dir, "avatars")
        self.stats = {"day": "", "earned": 0.0, "trips": 0, "ratings": [], "total_earned": 0.0, "total_trips": 0,
                      "history": [], "profile": {"name": "Driver", "avatar": None, "since": time.strftime("%B %Y")},
                      "offers": 0, "accepted": 0}
        try:
            self.stats.update(json.load(open(self.file)))
        except Exception:
            pass
        self.on_people = None                # called after pictures change (BeamDeck copies them into the game)
        people.ensure_cards(os.path.join(self.avatar_dir, "standees"))
        self.custom, self.generic = [], people.generic()
        self.riders = self._load_riders()
        self.state = "offline"
        self.req = None                      # the current request / trip
        self.pose = None
        self.places = []                     # nearby candidates (pickups)
        self.far = []                        # candidates for destinations
        self.places_at = None                # where the pool was fetched
        self.next_req_at = 0.0
        self.events = []
        self.last = None                     # the last finished trip's card
        self._q = 0
        self.pending_q = {}
        self._far_by = {}
        self._fail_noted = 0

    # ---- riders: the player's own people (pictures in the avatar folder: added in the console, or made by
    # make_avatars.py) and the built-in generic people (people.py) ----
    def _load_riders(self):
        try:
            credits = json.load(open(os.path.join(self.avatar_dir, "avatars.json")))
        except Exception:
            credits = {}
        out = []
        if os.path.isdir(self.avatar_dir):
            for f in sorted(os.listdir(self.avatar_dir)):
                if f.endswith(".jpg") and not f.startswith("_"):
                    n = f[:-4]
                    out.append({"name": n.replace(" (Collab)", ""), "avatar": n,
                                "credit": (credits.get(n) or {}).get("artist")})
        self.custom = out
        return out + self.generic

    def _party(self, size):
        """Who asks for this ride. Pool: "mixed" (the player's own people half the time), "custom", "generic"."""
        pool = self.stats.get("pool", "mixed")
        if pool == "generic" or not self.custom:
            src = self.generic
        elif pool == "custom":
            src = self.custom
        else:
            src = self.custom if random.random() < 0.5 else self.generic
        return random.sample(src, min(size, len(src)))

    def avatar_path(self, name):
        f = os.path.join(self.avatar_dir, os.path.basename(name) + ".jpg")
        return f if os.path.isfile(f) else None

    def _changed(self):
        self.riders = self._load_riders()
        if self.on_people:
            try:
                self.on_people()
            except Exception:
                pass

    @staticmethod
    def _image(b64, magic):
        """An uploaded picture (base64, possibly a data: URL) as bytes, or None if it is not that kind or too big."""
        import base64
        try:
            data = base64.b64decode(str(b64).split(",")[-1])
        except Exception:
            return None
        return data if data[:len(magic)] == magic and len(data) < 4_000_000 else None

    def add_person(self, name, picture, card):
        """A new rider (or new pictures for one): a square JPEG and a tall PNG card, both made by the console."""
        name = people.clean_name(name)
        jpg, png = self._image(picture, b"\xff\xd8"), self._image(card, b"\x89PNG")
        if not name or not jpg or not png:
            return False, "A name and a picture are needed"
        os.makedirs(os.path.join(self.avatar_dir, "standees"), exist_ok=True)
        with open(os.path.join(self.avatar_dir, name + ".jpg"), "wb") as f:
            f.write(jpg)
        with open(os.path.join(self.avatar_dir, "standees", name + ".png"), "wb") as f:
            f.write(png)
        self._changed()
        return True, name

    def delete_person(self, name):
        name = people.clean_name(name)
        if not name or not self.avatar_path(name):
            return False, "No such person"
        for f in (os.path.join(self.avatar_dir, name + ".jpg"), os.path.join(self.avatar_dir, "standees", name + ".png")):
            if os.path.isfile(f):
                os.remove(f)
        if self.stats["profile"].get("avatar") == name:
            self.stats["profile"]["avatar"] = None
            self._save()
        self._changed()
        return True, name

    def set_pool(self, pool):
        if pool in ("mixed", "custom", "generic"):
            self.stats["pool"] = pool
            self._save()

    def people_info(self):
        return {"custom": [{"name": p["name"], "avatar": p["avatar"], "credit": p["credit"]} for p in self.custom],
                "generic": len(self.generic), "pool": self.stats.get("pool", "mixed")}

    def set_profile(self, body):
        p = self.stats["profile"]
        if body.get("name"):
            p["name"] = str(body["name"]).strip()[:24] or p["name"]
        if "avatar" in body and (body["avatar"] is None or self.avatar_path(body["avatar"])):
            p["avatar"] = body["avatar"]
        if body.get("picture"):                      # the driver's own uploaded picture
            jpg = self._image(body["picture"], b"\xff\xd8")
            if jpg:
                os.makedirs(self.avatar_dir, exist_ok=True)
                with open(os.path.join(self.avatar_dir, "_me.jpg"), "wb") as f:
                    f.write(jpg)
                p["avatar"] = "_me"
                p["avatar_v"] = int(time.time())     # so pages reload the picture
        self._save()

    # ---- persistence ----
    def _save(self):
        try:
            json.dump(self.stats, open(self.file + ".part", "w"))
            os.replace(self.file + ".part", self.file)
        except OSError:
            pass

    def _today(self):
        d = time.strftime("%Y-%m-%d")
        if self.stats["day"] != d:
            self.stats.update(day=d, earned=0.0, trips=0)

    def say(self, text, kind="ok", secs=5):
        """The passenger speaks: on the tablet's log, as a message in game, and out loud on the tablet."""
        if not self.req:
            return
        self.note(kind, f"{self.req['name']}: “{text}”")
        self.send([{"cmd": "notify", "text": f"{self.req['name']}: “{text}”", "secs": secs}])
        who = (self.req.get("party") or [{}])[0].get("name") or self.req["name"]
        self.announce(text, "rider", who)

    # ---- sessions (one stretch online) and the day-by-day history ----
    def _session_add(self, **kw):
        ss = self.session
        if not ss:
            return
        for k, v in kw.items():
            if k == "stars":
                ss["stars"].append(v)
            elif k == "best_tip":
                ss["best_tip"] = max(ss["best_tip"], v)
            else:
                ss[k] = round(ss[k] + v, 2)

    def _end_session(self):
        ss, self.session = self.session, None
        if not ss:
            return
        ss["end"] = round(time.time())
        ss["mins"] = round((ss["end"] - ss["start"]) / 60)
        ss["rating"] = round(sum(ss["stars"]) / len(ss["stars"]), 2) if ss["stars"] else None
        if ss["trips"] or ss["cancelled"] or ss["mins"] >= 2:
            self.stats["sessions"] = ([ss] + self.stats.get("sessions", []))[:200]
            self._save()
            self.summary = ss

    def record_bus(self, entry):
        """A bus trip (from the helper), for the Profile's day-by-day history."""
        self.stats["bus"] = ([entry] + self.stats.get("bus", []))[:300]
        self._save()

    def dismiss(self):
        self.summary = None

    def days(self, n=14):
        """Per day, newest first: rides (trips, earned, tips, stars, km), time online and bus trips."""
        out = {}
        day = lambda t: time.strftime("%Y-%m-%d", time.localtime(t))
        blank = lambda: {"rides": 0, "earned": 0.0, "tips": 0.0, "stars": [], "km": 0.0, "cancelled": 0,
                         "online_min": 0, "bus": 0, "bus_scores": [], "carried": 0}
        for h in self.stats.get("history", []):
            d = out.setdefault(day(h["t"]), blank())
            if h.get("cancelled"):
                d["cancelled"] += 1
                continue
            d["rides"] += 1
            d["earned"] += h.get("pay", 0)
            d["tips"] += h.get("tip", 0)
            d["km"] += h.get("km", 0)
            if h.get("stars"):
                d["stars"].append(h["stars"])
        for ss in self.stats.get("sessions", []):
            out.setdefault(day(ss["start"]), blank())["online_min"] += ss.get("mins", 0)
        for b in self.stats.get("bus", []):
            d = out.setdefault(day(b["t"]), blank())
            d["bus"] += 1
            d["bus_scores"].append(b.get("score", 0))
            d["carried"] += b.get("carried", 0)
        rows = []
        for k in sorted(out, reverse=True)[:n]:
            d = out[k]
            rows.append({"day": k, "rides": d["rides"], "earned": round(d["earned"], 2), "tips": round(d["tips"], 2),
                         "rating": round(sum(d["stars"]) / len(d["stars"]), 2) if d["stars"] else None,
                         "km": round(d["km"], 1), "cancelled": d["cancelled"], "online_min": d["online_min"],
                         "bus": d["bus"], "carried": d["carried"],
                         "bus_score": round(sum(d["bus_scores"]) / len(d["bus_scores"])) if d["bus_scores"] else None})
        return {"days": rows, "bus": self.stats.get("bus", [])[:30]}

    def _standee(self, lat=None, lon=None, avatar=None, bearing=90.0):
        """Show the ride's people standing at a spot (side by side, 0.9 m apart across `bearing`), or nobody."""
        if lat is None:
            self.send([{"cmd": "standee", "show": False}])
            return
        party = (self.req or {}).get("party") or ([{"avatar": avatar}] if avatar else [])
        people = []
        n = len(party)
        b = math.radians(bearing)
        for k, who in enumerate(party):
            if not who.get("avatar"):
                continue
            off = (k - (n - 1) / 2) * 0.9                     # spread along the bearing, centred on the spot
            la = lat + off * math.cos(b) / 110574
            lo = lon + off * math.sin(b) / (111320 * max(0.2, math.cos(math.radians(lat))))
            people.append({"avatar": who["avatar"], "lat": la, "lon": lo})
        if people:
            self.send([{"cmd": "standee", "show": True, "people": people}])

    def _seated(self, on):
        party = (self.req or {}).get("party") or []
        self.send([{"cmd": "seated", "people": [w["avatar"] for w in party if w.get("avatar")] if on else []}])

    def note(self, kind, text):
        self.events.append({"t": round(time.time(), 1), "kind": kind, "text": text})
        self.events = self.events[-20:]

    # ---- places from the game ----
    def ask_places(self, lat, lon, radius, cats, purpose, cached=False):
        self._q += 1
        qid = f"q{self._q}"
        self.pending_q[qid] = purpose
        self.send([{"cmd": "places", "id": qid, "lat": lat, "lon": lon, "radius": radius, "categories": cats,
                    "limit": 120, "cachedOnly": cached}])

    def on_places(self, m):
        purpose = self.pending_q.get(m.get("id"))
        if not purpose:
            return
        if m.get("complete") or not m.get("pending"):
            self.pending_q.pop(m.get("id"), None)
        good = [p for p in m.get("places", []) if p.get("name") and p.get("open") != "permanently_closed"
                and (p.get("confidence") is None or p["confidence"] >= 0.6)]
        if purpose == "near":
            self.places = good
        else:
            # several far lookups (one per direction): keep them all, newest answer for each spot
            self._far_by[purpose] = good
            seen, self.far = set(), []
            for lst in self._far_by.values():
                for p in lst:
                    if p.get("id") not in seen:
                        seen.add(p.get("id")); self.far.append(p)
        if m.get("error"):
            self.note("warn", f"Places: {m['error']}")

    # ---- driver actions (tablet) ----
    def go_online(self, on):
        if on and self.state == "offline":
            self.state = "online"
            self.summary = None
            self.session = {"start": round(time.time()), "earned": 0.0, "trips": 0, "tips": 0.0, "km": 0.0,
                            "stars": [], "best_tip": 0.0, "cancelled": 0, "offers": 0, "accepted": 0}
            self.next_req_at = time.time() + 5
            self.note("ok", "Online: waiting for requests")
            self.places_at = None
        elif not on:
            if self.req and self.state in ("to_pickup", "boarding", "on_trip"):
                self.note("bad", f"Went offline mid-trip: {self.req['name']} cancelled")
            was_on = self.state != "offline"
            self._clear_game()
            self.state, self.req = "offline", None
            if was_on:
                self._end_session()

    def preview(self, avatar):
        """Stand a passenger beside the car for 15 s (to look at their cut-out or 3D model)."""
        if not self.pose:
            return
        hd = math.radians((self.pose.get("heading") or 0) + 90)
        la = self.pose["lat"] + 4.0 * math.cos(hd) / 110574
        lo = self.pose["lon"] + 4.0 * math.sin(hd) / (111320 * math.cos(math.radians(self.pose["lat"])))
        self._standee(la, lo, avatar)
        self._standee_off_at = time.time() + 15

    def accept(self):
        if self.state != "offered":
            return
        r = self.req
        r["accepted_at"] = time.time()
        self.stats["accepted"] = self.stats.get("accepted", 0) + 1
        self._session_add(accepted=1)
        # until BeamNG.Earth's planner answers: ~30 km/h along streets, plus a little
        r["pickup_est"] = r["pickup_m"] * 1.35 / 8.0 + 30
        r["await_route"] = "pickup"
        self.state = "to_pickup"
        self.note("ok", f"Picking up {r['name']} at {r['pickup']['name']}")
        self._standee(r["pickup"]["lat"], r["pickup"]["lon"], r.get("avatar"))
        self.send([{"cmd": "gpsTo", "lat": r["pickup"]["lat"], "lon": r["pickup"]["lon"], "name": "Pickup: " + r["pickup"]["name"]},
                   self._markers()])

    def decline(self):
        if self.state == "offered":
            self.note("warn", f"Declined {self.req['name']}")
            self.state, self.req = "online", None
            self.next_req_at = time.time() + random.uniform(*REQUEST_EVERY)

    def cancel(self):
        if self.req and self.state in ("to_pickup", "boarding", "on_trip"):
            self.note("bad", f"Cancelled {self.req['name']}'s ride")
            r = self.req
            self._session_add(cancelled=1)
            self.stats["history"] = ([{"t": round(time.time()), "name": r["name"], "avatar": r.get("avatar"),
                                       "pickup": r["pickup"]["name"], "dest": r["dest"]["name"], "km": r["km"],
                                       "fare": r["fare"], "pay": 0, "cancelled": True}]
                                     + self.stats.get("history", []))[:300]
            self._save()
            self._clear_game()
            self.state, self.req = "online", None
            self.next_req_at = time.time() + random.uniform(*REQUEST_EVERY)

    def _clear_game(self):
        self.send([{"cmd": "gpsClear"}, {"cmd": "markers", "stops": []}, {"cmd": "seated", "people": []}])
        if not getattr(self, "_standee_off_at", 0):
            self._standee()

    def _markers(self):
        # the passengers themselves (cut-out / 3D) show where to go: no cylinders or labels for rides
        return {"cmd": "markers", "stops": []}

    # ---- the request generator ----
    def _make_request(self):
        me = (self.pose["lat"], self.pose["lon"])
        near = [p for p in self.places if 80 < dist_m(me, (p["lat"], p["lon"])) < PICKUP_R]
        if not near:
            return False
        pick = random.choice(near[:60])
        pp = (pick["lat"], pick["lon"])
        pool = [p for p in self.far + self.places if p.get("id") != pick.get("id")]
        kind = None
        # an airport run or a long trip across town now and then, when such places are known
        airports = [p for p in pool if AIRPORT.search(p.get("name") or "") or "airport" in (p.get("category") or "")]
        longs = [p for p in pool if LONG_MIN < dist_m(pp, (p["lat"], p["lon"])) < LONG_MAX]
        dests = []
        if airports and random.random() < 0.06:
            dests, kind = airports, "airport"
        elif longs and random.random() < LONG_CHANCE:
            dests, kind = longs, "long"
        if not dests:
            dests = [p for p in pool if DEST_MIN < dist_m(pp, (p["lat"], p["lon"])) < DEST_MAX]
        if not dests:   # far places not in yet: a shorter ride from what is round here
            dests = [p for p in self.places if dist_m(pp, (p["lat"], p["lon"])) > 800 and p.get("id") != pick.get("id")]
        if not dests:
            if time.time() - self._fail_noted > 60:
                self._fail_noted = time.time()
                self.note("warn", f"No destinations yet ({len(self.places)} places near, {len(self.far)} far): still looking")
            return False
        dest = random.choice(dests)
        mood = random.choices([m for m, _ in MOODS], weights=[w for _, w in MOODS])[0] or {}
        if mood.get("emergency"):
            # usually a hospital, when one is known nearby; otherwise somewhere they urgently have to be
            care = [p for p in pool if (HOSPITAL.search(p.get("name") or "") or p.get("category") in ("hospital", "health"))
                    and dist_m(pp, (p["lat"], p["lon"])) > 600]
            if care and random.random() < 0.7:
                # a medical emergency: nothing about the driving matters, only getting there
                dest, kind = random.choice(care), "emergency"
                mood = dict(mood, medical=True, note="Medical emergency: get them to the hospital")
        km = dist_m(pp, (dest["lat"], dest["lon"])) * 1.3 / 1000
        mins = km / 32 * 60 + 2                  # ~32 km/h in town, plus lights
        hour = time.localtime().tm_hour
        surge, surge_why = 1.0, None
        if hour in (7, 8, 16, 17) and random.random() < 0.4:
            surge, surge_why = 1.4, "busy"
        elif hour in NIGHT_HOURS and random.random() < 0.55:
            surge, surge_why = random.choice((1.3, 1.5, 1.8)), "late night"
        fare = max(MIN_FARE, (BASE + PER_KM * km + PER_MIN * mins) * surge)
        if mood.get("emergency"):
            fare *= EMERGENCY_BONUS
        if kind == "long":
            fare *= LONG_BONUS
        elif kind == "airport":
            fare += AIRPORT_BONUS
        size = random.choices([1, 2, 3], weights=[0.74, 0.18, 0.08])[0]
        party = self._party(size)
        who = party[0]
        self.stats["offers"] = self.stats.get("offers", 0) + 1
        self._session_add(offers=1)
        fare *= 1 + 0.15 * (len(party) - 1)
        self.req = {"name": who["name"] + (f" +{len(party) - 1}" if len(party) > 1 else ""),
                    "party": [{"name": w["name"], "avatar": w["avatar"], "credit": w["credit"]} for w in party],
                    "avatar": who["avatar"], "credit": who["credit"],
                    "rides": random.randint(3, 480), "since": random.randint(2019, 2026),
                    "rating": round(random.uniform(4.5, 5.0), 2),
                    "pickup": {k: pick[k] for k in ("name", "lat", "lon")} | {"cat": pretty_cat(pick.get("category"))},
                    "dest": {k: dest[k] for k in ("name", "lat", "lon")} | {"cat": pretty_cat(dest.get("category"))},
                    "km": round(km, 1), "mins": round(mins), "fare": round(fare, 2), "surge": surge,
                    "surge_why": surge_why, "kind": kind,
                    "change_at": random.uniform(0.3, 0.6) if random.random() < CHANGE_CHANCE and not mood.get("emergency") else None,
                    "pickup_m": round(dist_m(me, pp)), "mood": mood, "offered_at": time.time(),
                    "jolts": 0, "crash": 0, "speeding_s": 0.0,
                    "board_left": random.uniform(*BOARD_S) + mood.get("board", 0) + 3 * (len(party) - 1)}
        self.state = "offered"
        self.note("ok", f"Request: {self.req['name']} → {dest['name']} (${self.req['fare']:.2f})")
        self.send([{"cmd": "notify", "text": f"Ride request: {self.req['name']}, ${self.req['fare']:.2f}", "secs": 4}])
        return True

    @staticmethod
    def _road_end(pts, place):
        """Where the route reaches the place on the road. On BeamNG.Earth the route ends on the road beside the
        place; on regular maps it runs on to the place's own position (often well off the road), so take the last
        point at least 6 m from the place, or the end."""
        for p in reversed(pts[:-1]):
            if dist_m(p, place) >= 6:
                return (p[0], p[1])
        return (pts[-1][0], pts[-1][1])

    def _near(self, me, place, spot):
        return dist_m(me, place) < PICKUP_M or (spot is not None and dist_m(me, spot) < PICKUP_M)

    def on_route(self, route):
        """BeamNG.Earth planned a route: its duration (free-flow) sets the time the passenger expects."""
        r = self.req
        if not (r and r.get("await_route") and route.get("dur")):
            return
        expect = route["dur"] * 1.25 + 20          # lights, turns, a stop sign or two
        pts = route.get("points") or []
        if r["await_route"] == "trip" and self.state == "on_trip" and pts:
            r["drop_spot"] = self._road_end(pts, (r["dest"]["lat"], r["dest"]["lon"]))
        if r["await_route"] == "pickup" and self.state == "to_pickup" and pts:
            pk = (r["pickup"]["lat"], r["pickup"]["lon"])
            end = self._road_end(pts, pk)
            r["pickup_spot"] = end
            d = max(1.0, dist_m(end, pk))
            f = min(1.0, 4.0 / d)                    # 4 m from the road's edge towards the place
            toward = math.degrees(math.atan2((pk[1] - end[1]) * math.cos(math.radians(end[0])), pk[0] - end[0]))
            self._standee(end[0] + (pk[0] - end[0]) * f, end[1] + (pk[1] - end[1]) * f, r.get("avatar"), toward + 90)
        if r["await_route"] == "trip_change" and self.state == "on_trip":
            if pts:
                r["drop_spot"] = self._road_end(pts, (r["dest"]["lat"], r["dest"]["lon"]))
            r["trip_est"] = (time.time() - r["start_at"]) + expect
            r["await_route"] = None
            return
        if r["await_route"] == "pickup" and self.state == "to_pickup":
            r["pickup_est"] = expect
        elif r["await_route"] == "trip" and self.state == "on_trip":
            r["trip_est"] = expect
        r["await_route"] = None

    # ---- the driving loop (10 Hz) ----
    def update(self, m):
        self.pose = m
        if getattr(self, "_standee_off_at", 0) and time.time() > self._standee_off_at and self.state in ("online", "offline"):
            self._standee_off_at = 0
            self._standee()
        if self.state == "offline":
            return
        now = time.time()
        me = (m["lat"], m["lon"])
        speed = m.get("speed", 0)
        # keep a pool of nearby places, refreshed as the driver moves
        if self.places_at is None or dist_m(me, self.places_at) > 1200:
            self.places_at = me
            self._far_by = {}
            self.ask_places(me[0], me[1], 2500, PICKUP_CATS, "near")
            # destinations: a few spots 3-5 km away in different directions (BeamNG.Earth fetches those areas)
            for k, bearing in enumerate(random.sample(range(0, 360, 45), 4)):
                d = random.uniform(3000, 5000)
                la = me[0] + d * math.cos(math.radians(bearing)) / 110574
                lo = me[1] + d * math.sin(math.radians(bearing)) / (111320 * math.cos(math.radians(me[0])))
                self.ask_places(la, lo, 1500, DEST_CATS, f"far{k}")
        if self.state == "online" and now >= self.next_req_at:
            if not self._make_request():
                self.next_req_at = now + 5       # places not in yet
        elif self.state == "offered" and now - self.req["offered_at"] > OFFER_S:
            self.note("warn", f"Missed {self.req['name']}'s request")
            self.state, self.req = "online", None
            self.next_req_at = now + random.uniform(*REQUEST_EVERY)
        elif self.state == "to_pickup":
            if self._near(me, (self.req["pickup"]["lat"], self.req["pickup"]["lon"]), self.req.get("pickup_spot")) and speed < STOPPED_MS:
                self.state = "boarding"
                self.req.setdefault("pickup_s", now - self.req["accepted_at"])
                self.send([{"cmd": "notify", "text": f"{self.req['name']} is getting in…", "secs": 3}])
                hd = (m.get("heading") or 0)
                side = math.radians(hd + 90)                   # 2.2 m to the right of the car, lined up along it
                self._standee(me[0] + 2.2 * math.cos(side) / 110574,
                              me[1] + 2.2 * math.sin(side) / (111320 * max(0.2, math.cos(math.radians(me[0])))),
                              self.req.get("avatar"), hd)
        elif self.state == "boarding":
            if speed > 2:
                self.state = "to_pickup"            # drove off before they got in
            else:
                self.req["board_left"] -= 0.1
                if self.req["board_left"] <= 0:
                    self._standee()
                    self._seated(True)
                    mood = self.req["mood"]
                    self.say(random.choice(["Hi! Thanks for picking me up.", "Hey there!", "Hello! Nice car.",
                                            "Hi, thanks for coming!"]) +
                             (" It's an emergency, please go as fast as you can!" if mood.get("emergency") else
                              " I'm late, but go easy on the bumps, this box is fragile!" if mood.get("hurry") and mood.get("fragile") else
                              " I'm running late, could we hurry?" if mood.get("hurry") else
                              " Careful with the bumps, I've got something fragile." if mood.get("fragile") else
                              " Could you take it easy? I get carsick." if mood.get("gentle") else ""))
                    self.state = "on_trip"
                    self.req["start_at"] = now
                    self.req["trip_est"] = self.req["mins"] * 60
                    self.req["await_route"] = "trip"
                    self.note("ok", f"{self.req['name']} is in. Off to {self.req['dest']['name']}")
                    self.announce(f"Starting trip to {self.req['dest']['name']}.", "app")
                    d = self.req["dest"]
                    self.send([{"cmd": "gpsTo", "lat": d["lat"], "lon": d["lon"], "name": "Drop-off: " + d["name"]},
                               self._markers()])
        elif self.state == "on_trip":
            r = self.req
            g = math.hypot(m.get("gx", 0), m.get("gy", 0))
            mood = r["mood"]
            careful = mood.get("gentle") or mood.get("fragile")
            minds_rough = careful or not mood.get("hurry")      # in a hurry: hard braking and corners are fine
            jolt_at = JOLT_GENTLE if careful else JOLT
            r["_g_run"] = r.get("_g_run", 0) + 1 if (g > jolt_at and minds_rough) else 0
            r["_c_run"] = r.get("_c_run", 0) + 1 if g > CRASH_HELD else 0
            if (g > CRASH or r["_c_run"] >= 2) and now - r.get("_g_at", 0) > 3:
                r["_g_at"] = now; r["crash"] += 1; r["_g_run"] = 0
                self.say("Don't stop, just keep going!" if mood.get("medical") else "Whoa! What was THAT?", "bad")
            elif r["_g_run"] >= JOLT_HOLD and now - r.get("_g_at", 0) > JOLT_GAP_S:
                r["_g_at"] = now; r["jolts"] += 1; r["_g_run"] = 0
                if mood.get("fragile") and r["jolts"] in (2, 4):
                    self.say(random.choice(["Careful, that's fragile!", "Mind the box, please!",
                                            "I heard something shift back there…"]), "warn")
                elif r["jolts"] in ((3, 6) if careful else (4, 8)):
                    self.say(random.choice(["Easy on the brakes…", "Careful!", "Ow, my coffee!"]), "warn")
            lim = m.get("limit")
            if mood.get("emergency"):
                r["_slow"] = r.get("_slow", 0) + 0.1 if speed < 3 else 0
                if r["_slow"] > 20 and now - r.get("_nag_at", 0) > 60 and r.get("_nags", 0) < 3:
                    r["_nag_at"] = now; r["_nags"] = r.get("_nags", 0) + 1
                    self.say(random.choice(["Please, we have to keep moving!", "Can we go another way? Hurry!",
                                            "Come on, come on…"]), "warn")
                r["_over"] = 0
            elif lim and speed * 3.6 > lim * 3.6 + SPEEDING_KMH:
                r["_over"] = r.get("_over", 0) + 0.1
                if r["_over"] > SPEEDING_GRACE_S:
                    r["speeding_s"] += 0.1
                    if r["speeding_s"] > 3 and not r.get("_said_fast"):
                        r["_said_fast"] = True
                        self.say(random.choice(["Bit fast, no?", "Whoa, what's the speed limit here?",
                                                "We're not in a race, you know."]), "warn")
            else:
                r["_over"] = 0
            if r.get("change_at") and not r.get("changed") and now - r["start_at"] > r["change_at"] * r.get("trip_est", 300):
                self._change_dest(me)
            if self._near(me, (r["dest"]["lat"], r["dest"]["lon"]), r.get("drop_spot")) and speed < STOPPED_MS:
                self._finish()

    def _change_dest(self, me):
        """The rider changes their mind on the way: somewhere else 1-4 km from here. The fare follows the
        distance, and the time they expect starts again from now."""
        r = self.req
        r["changed"] = True
        old = (r["dest"]["lat"], r["dest"]["lon"])
        cands = [p for p in self.far + self.places if 1000 < dist_m(me, (p["lat"], p["lon"])) < 4000
                 and dist_m(old, (p["lat"], p["lon"])) > 600]
        if not cands:
            return
        new = random.choice(cands)
        before = dist_m(me, old) * 1.3 / 1000
        after = dist_m(me, (new["lat"], new["lon"])) * 1.3 / 1000
        extra = (after - before) * PER_KM * r.get("surge", 1.0)
        r["fare"] = round(max(r["fare"] * 0.8, r["fare"] + extra), 2)
        r["km"] = round(r["km"] + after - before, 1)
        r["dest"] = {k: new[k] for k in ("name", "lat", "lon")} | {"cat": pretty_cat(new.get("category"))}
        r["drop_spot"] = None
        r["trip_est"] = (time.time() - r["start_at"]) + after * 1000 / 8.0 + 30
        r["await_route"] = "trip_change"
        self.say(random.choice([f"Actually, could you take me to {new['name']} instead?",
                                f"Change of plans! {new['name']}, please.",
                                f"Sorry, can we go to {new['name']}? I forgot something."]), "warn", 6)
        self.note("warn", f"New destination: {new['name']} (fare now ${r['fare']:.2f})")
        self.send([{"cmd": "gpsTo", "lat": new["lat"], "lon": new["lon"], "name": "Drop-off: " + new["name"]}])

    @staticmethod
    def _time_score(actual, expected, strict=False):
        ratio = actual / max(30.0, expected)
        # stars lost as the time runs over the estimate: 30% over is still on time (traffic, lights, a missed turn);
        # strict = in a hurry, "emergency" = every minute counts
        steps = {False: (1.3, 1.7, 2.2, 3.0), True: (1.1, 1.35, 1.7, 2.2),
                 "emergency": (1.0, 1.15, 1.35, 1.6)}[strict or False]
        return 5 - sum(1 for t in steps if ratio > t)

    def _finish(self):
        r = self.req
        self._today()
        trip_s = time.time() - r["start_at"]
        pickup_s = r.get("pickup_s", 0)
        mood = r["mood"]
        sub = {
            "pickup": self._time_score(pickup_s, r.get("pickup_est", 120)),
            "trip": self._time_score(trip_s, r.get("trip_est", r["mins"] * 60),
                                     strict="emergency" if mood.get("emergency") else bool(mood.get("hurry"))),
            "smooth": max(1, round(5 - JOLT_COST * max(0, r["jolts"] - JOLTS_FREE)
                                   * (1.5 if mood.get("gentle") or mood.get("fragile") else 1) - 1.5 * r["crash"])),
            "safety": max(1, round(5 - r["speeding_s"] / SPEEDING_STAR_S - 1.5 * r["crash"])),
        }
        weights = {"pickup": 0.2, "trip": 0.3, "smooth": 0.25, "safety": 0.25}
        if mood.get("emergency"):                   # getting there fast is what matters
            weights = {"pickup": 0.2, "trip": 0.7, "smooth": 0.05, "safety": 0.05}
        stars = max(1, min(5, round(sum(sub[k] * w for k, w in weights.items()))))
        if mood.get("medical"):                     # got there: that's all that counts (4 at least, 5 if quick)
            sub["smooth"] = sub["safety"] = 5
            stars = max(4, sub["trip"] if sub["trip"] == 5 else 4)
        elif r["crash"]:                            # one knock: 4 at best; more: 2
            stars = min(stars, 4 if r["crash"] == 1 else 2)
        comments = {
            "pickup": ("Took a while to show up…", "Bit of a wait for the pickup."),
            "trip": ("Thank you for getting me here." if mood.get("medical") else
                     "We didn't make it in time…" if mood.get("emergency") else
                     "That took way longer than it should have." if not mood.get("hurry") else "I'm going to be late now!",
                     "A little slower than I expected."),
            "smooth": ("I think something broke in the box." if mood.get("fragile") else
                       "I feel sick. That was rough." if mood.get("gentle") else "Hold on! Rough driving.",
                       "A few jerky stops." if not mood.get("fragile") else "A bit bumpy for my stuff."),
            "safety": ("You were flying! I didn't feel safe.", "A bit fast in places."),
        }
        worst = min(sub, key=sub.get)
        if stars == 5 and mood.get("emergency"):
            comment = random.choice(["You got me there in time. Thank you so much!", "Made it! You're a lifesaver.",
                                     "That was incredible driving, thank you!"])
        elif stars == 5:
            comment = random.choice(["Great ride, thanks!", "Smooth and quick. Perfect.", "Best driver this week!",
                                     "Got here in no time, thank you!"])
        else:
            comment = comments[worst][0 if sub[worst] <= 2 else 1]
        tip = 0.0
        if stars == 5:
            tip = round(r["fare"] * random.uniform(0.10, 0.22) * mood.get("tip", 1.0), 2)
            if mood.get("emergency") and sub["trip"] == 5:
                tip = round(r["fare"] * random.uniform(0.35, 0.5), 2)      # grateful
            elif mood.get("hurry") and sub["trip"] == 5:
                tip += 3.0
        pay = round(r["fare"] + tip, 2)
        mmss = lambda sec: f"{int(sec // 60)}:{int(sec % 60):02d}"
        self.stats["earned"] = round(self.stats["earned"] + pay, 2)
        if getattr(self, "earn", None):                       # CareerMP: this hour's payslip, as the ride-share job
            self.earn("rideshare", pay, f"Ride: {r['name']}")
        self.stats["total_earned"] = round(self.stats["total_earned"] + pay, 2)
        self.stats["trips"] += 1
        self.stats["total_trips"] += 1
        self.stats["ratings"] = (self.stats["ratings"] + [stars])[-100:]
        self._session_add(earned=pay, trips=1, tips=tip, km=r["km"], stars=stars, best_tip=tip)
        self.stats["history"] = ([{"t": round(time.time()), "name": r["name"], "avatar": r.get("avatar"),
                                   "pickup": r["pickup"]["name"], "dest": r["dest"]["name"], "km": r["km"],
                                   "fare": r["fare"], "tip": tip, "pay": pay, "stars": stars, "comment": comment,
                                   "pickup_time": mmss(pickup_s), "trip_time": mmss(trip_s), "sub": sub,
                                   "kind": r.get("kind"), "changed": bool(r.get("changed"))}]
                                 + self.stats.get("history", []))[:300]
        self.last = {"name": r["name"], "avatar": r.get("avatar"), "dest": r["dest"]["name"], "fare": r["fare"], "tip": tip, "pay": pay,
                     "stars": stars, "comment": comment, "sub": sub,
                     "pickup_time": mmss(pickup_s), "pickup_expect": mmss(r.get("pickup_est", 0)),
                     "trip_time": mmss(trip_s), "trip_expect": mmss(r.get("trip_est", 0)),
                     "jolts": r["jolts"], "crash": r["crash"], "speeding_s": round(r["speeding_s"]), "t": time.time()}
        self.note("ok", f"Dropped {r['name']}: ${pay:.2f} · {'★' * stars} \"{comment}\"")
        self.send([{"cmd": "notify", "text": f"{r['name']}: “{comment}”  {'★' * stars}  ${pay:.2f}", "secs": 7}])
        if self.pose and r.get("avatar"):
            hd = math.radians((self.pose.get("heading") or 0) + 90)       # the kerb side (right)
            la = self.pose["lat"] + 3.0 * math.cos(hd) / 110574
            lo = self.pose["lon"] + 3.0 * math.sin(hd) / (111320 * math.cos(math.radians(self.pose["lat"])))
            self._seated(False)
            self._standee(la, lo, r.get("avatar"), self.pose.get("heading") or 0)
            self._standee_off_at = time.time() + 12
        self._clear_game()
        self.state, self.req = "online", None
        self.next_req_at = time.time() + random.uniform(*REQUEST_EVERY)

    # ---- for the tablet ----
    def view(self):
        self._today()
        r = self.req
        me = (self.pose["lat"], self.pose["lon"]) if self.pose else None
        cur = None
        if r:
            target = r["dest"] if self.state == "on_trip" else r["pickup"]
            cur = {k: r[k] for k in ("name", "rating", "pickup", "dest", "km", "mins", "fare", "surge", "pickup_m",
                                     "avatar", "credit", "rides", "since")}
            cur["surge_why"], cur["kind"], cur["changed"] = r.get("surge_why"), r.get("kind"), bool(r.get("changed"))
            cur["emergency"] = "medical" if r["mood"].get("medical") else bool(r["mood"].get("emergency"))
            cur["party"] = [w["avatar"] for w in r.get("party", []) if w.get("avatar")]
            cur["note"] = r["mood"].get("note")
            cur["left_s"] = max(0, round(OFFER_S - (time.time() - r["offered_at"]))) if self.state == "offered" else None
            cur["dist"] = round(dist_m(me, (target["lat"], target["lon"]))) if me else None
            cur["jolts"], cur["crash"] = r["jolts"], r["crash"]
            if self.state in ("to_pickup", "boarding"):
                cur["timer"] = round(time.time() - r["accepted_at"]); cur["expect"] = round(r.get("pickup_est", 0))
            elif self.state == "on_trip":
                cur["timer"] = round(time.time() - r["start_at"]); cur["expect"] = round(r.get("trip_est", 0))
        rt = self.stats["ratings"]
        h = self.stats.get("history", [])
        done = [x for x in h if not x.get("cancelled")]
        me = dict(self.stats["profile"]) | {
            "trips": self.stats["total_trips"], "earned": self.stats["total_earned"],
            "five": sum(1 for x in done if x.get("stars") == 5),
            "accept": round(100 * self.stats.get("accepted", 0) / self.stats["offers"]) if self.stats.get("offers") else None,
            "km": round(sum(x.get("km", 0) for x in done), 1),
            "cancelled": sum(1 for x in h if x.get("cancelled"))}
        ss = self.session
        live = ss and {"mins": round((time.time() - ss["start"]) / 60), "earned": ss["earned"], "trips": ss["trips"]}
        return {"state": self.state, "req": cur, "last": self.last, "events": self.events[-8:], "me": me,
                "session": live, "summary": self.summary,
                "riders": [x["avatar"] for x in self.custom],
                "today": {"earned": self.stats["earned"], "trips": self.stats["trips"]},
                "rating": round(sum(rt) / len(rt), 2) if rt else None,
                "places": len(self.places), "speed": round((self.pose or {}).get("speed", 0) * 3.6),
                "limit": (self.pose or {}).get("limit")}
