"""Turn-by-turn directions on BeamNG.Earth's planned GPS route, worked out once in the helper so every client (tablet,
PC browser, the Android Auto app) shows the same next turn, and the spoken prompts come from one place.

The route comes from the game mod as points [[lat, lon], ...] and OSRM-style steps {type, modifier, name, exit,
offset (metres along the route)}.
"""
import math

ICONS = {"left", "right", "slight_left", "slight_right", "sharp_left", "sharp_right", "uturn", "straight",
         "roundabout", "merge", "arrive", "depart"}


def icon(step):
    """The arrow to draw for a step (the same set of arrows as the Android Auto app)."""
    t, m = step.get("type", ""), step.get("modifier", "") or ""
    side = "left" if "left" in m else "right" if "right" in m else ""
    if t == "arrive":
        return "arrive"
    if t == "depart":
        return "depart"
    if t in ("roundabout", "rotary", "roundabout turn", "exit roundabout", "exit rotary"):
        return "roundabout"
    if t == "merge":
        return "merge"
    if t in ("fork", "on ramp", "off ramp"):
        return "slight_" + (side or "right")
    if m == "uturn":
        return "uturn"
    key = m.replace(" ", "_")
    return key if key in ICONS else "straight"


def cue(step):
    """The words on the turn card: "Turn right onto Merivale Road"."""
    road = (step.get("name") or "").strip()
    onto = f" onto {road}" if road else ""
    t, m = step.get("type", ""), step.get("modifier", "") or ""
    side = "left" if "left" in m else "right"
    if t == "arrive":
        return "Arrive" + (f", on the {side}" if ("left" in m or "right" in m) else "")
    if t in ("roundabout", "rotary", "roundabout turn"):
        return f"At the roundabout, take exit {max(1, int(step.get('exit') or 1))}{onto}"
    if t == "merge":
        return "Merge" + onto
    if t == "fork":
        return f"Keep {side}{onto}"
    if t == "on ramp":
        return "Take the ramp" + onto
    if t == "off ramp":
        return "Take the exit" + onto
    if t == "end of road":
        return f"At the end of the road, turn {side}{onto}"
    if m == "uturn":
        return "Make a U-turn" + onto
    if m in ("", "straight"):
        return "Continue" + onto
    return f"Turn {m}{onto}"


def spoken_distance(m):
    if m >= 950:
        km = round(m / 1000, 1)
        return f"{km:g} kilometre" + ("" if km == 1 else "s")
    step = 100 if m >= 300 else 50 if m >= 100 else 10
    return f"{int(round(m / step) * step)} metres"


def shown_distance(m):
    return f"{m / 1000:.1f} km" if m >= 1000 else f"{int(round(m / 10) * 10)} m"


class Nav:
    def __init__(self):
        self.version = None
        self.xy, self.cum, self.steps = [], [], []
        self.idx = 0                       # last matched segment, so a pose only searches nearby
        self.spoken = set()                # (step offset, "far" | "near") already said
        self.view = None

    def set_route(self, route):
        if route.get("version") == self.version and self.xy:
            return
        self.version = route.get("version")
        pts = route.get("points") or []
        self.xy, self.cum, self.idx, self.spoken, self.view = [], [], 0, set(), None
        if len(pts) < 2:
            self.steps = []
            return
        self.lat0, self.lon0 = pts[0][0], pts[0][1]
        self.kx, self.ky = 111320.0 * math.cos(math.radians(self.lat0)), 110574.0
        self.xy = [((p[1] - self.lon0) * self.kx, (p[0] - self.lat0) * self.ky) for p in pts]
        c = [0.0]
        for i in range(1, len(self.xy)):
            c.append(c[-1] + math.dist(self.xy[i], self.xy[i - 1]))
        self.cum = c
        steps = [s for s in (route.get("steps") or []) if isinstance(s.get("offset"), (int, float))]
        self.steps = sorted(steps, key=lambda s: s["offset"])

    def _project(self, lat, lon):
        x, y = (lon - self.lon0) * self.kx, (lat - self.lat0) * self.ky
        n = len(self.xy)

        def best_in(lo, hi):
            best, bd, bi = 0.0, float("inf"), 0
            for i in range(max(0, lo), min(n - 1, hi)):
                (ax, ay), (bx, by) = self.xy[i], self.xy[i + 1]
                dx, dy = bx - ax, by - ay
                l2 = max(dx * dx + dy * dy, 1e-9)
                t = min(1.0, max(0.0, ((x - ax) * dx + (y - ay) * dy) / l2))
                d = math.hypot(x - (ax + t * dx), y - (ay + t * dy))
                if d < bd:
                    best, bd, bi = self.cum[i] + t * math.sqrt(l2), d, i
            return best, bd, bi

        along, off, i = best_in(self.idx - 30, self.idx + 300)
        if off > 50:                       # lost the place (a jump, a new route): look along the whole line
            along, off, i = best_in(0, n)
        self.idx = i
        return along, off

    def update(self, lat, lon, speed, quiet_arrive=False):
        """The car moved: returns the spoken prompts that are due now ([(text), ...]); self.view is the card."""
        if not self.xy or lat is None or lon is None:
            self.view = None
            return []
        along, off = self._project(lat, lon)
        total = self.cum[-1]
        ahead = [s for s in self.steps if s.get("type") != "depart" and s["offset"] > along + 3]
        if not ahead:
            self.view = None
            return []
        s = ahead[0]
        d = s["offset"] - along
        nxt = ahead[1] if len(ahead) > 1 and ahead[1]["offset"] - s["offset"] < 150 else None
        self.view = {"icon": icon(s), "cue": cue(s), "road": s.get("name") or "", "dist": round(d),
                     "distText": shown_distance(d), "exit": s.get("exit"),
                     "then": {"icon": icon(nxt), "cue": cue(nxt)} if nxt else None,
                     "left": round(max(0.0, total - along)), "off": round(off)}
        # prompts: one well ahead (further at speed), one just before the turn
        out = []
        v = max(0.0, speed or 0.0)
        near_d = min(150.0, max(45.0, v * 5))
        far_d = min(1000.0, max(300.0, v * 25))
        key = s["offset"]
        arrive = s.get("type") == "arrive"
        if arrive and quiet_arrive:        # a bus shift says its own stops
            return out
        if d <= near_d and (key, "near") not in self.spoken:
            self.spoken |= {(key, "near"), (key, "far")}
            words = "You have arrived" + (f". Your destination is on the {'left' if 'left' in (s.get('modifier') or '') else 'right'}"
                                          if ("left" in (s.get("modifier") or "") or "right" in (s.get("modifier") or "")) else "") \
                if arrive else cue(s)
            if nxt and not arrive:
                words += ", then " + cue(nxt)[0].lower() + cue(nxt)[1:]
            out.append(words + ".")
        elif near_d + 80 < d <= far_d and (key, "far") not in self.spoken:
            self.spoken.add((key, "far"))
            c = cue(s)
            words = ("Your destination is in " + spoken_distance(d)) if arrive else \
                f"In {spoken_distance(d)}, {c[0].lower() + c[1:]}"
            out.append(words + ".")
        return out
