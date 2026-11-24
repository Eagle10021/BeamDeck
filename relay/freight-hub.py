#!/usr/bin/env python3
"""BeamDeck freight hub: the shared side of Real Routes freight, so players can haul together like in ETS2.

Run it where every player can reach it (the Tailscale network that hosts the BeamMP server). Only Python's
standard library is needed. Everything is JSON over HTTP; each player's BeamDeck calls /api/presence about every
two seconds and gets the whole picture back.

  POST /api/presence   {pid, name, truck, lat, lon, hdg, kmh, job, ver}  -> the shared state (drivers, jobs,
                       convoys, chat). The first call from a pid claims the name; a name another
                       online player holds is made unique.
  POST /api/offers     {pid, offers: [...], force?}   jobs added to the shared board (force: a job the player is taking,
                       which takes the place of the oldest one when the board is full)
  POST /api/convoy     {pid, action: create|join|leave, code, offer}
  POST /api/chat       {pid, text, convoy?}
  POST /api/done       {pid, pay, km, stars, title, convoy_n}   a finished job: a line in the chat
  GET  /api/ping       is the hub up
  GET  /update/manifest.json, /update/files/<name>   the Windows app's update channel: files are put in
                       <data>/update by publish-windows.sh; nothing here is executed by the hub

Players are trusted friends on a private network, so the checks here only keep malformed data and accidents out:
sizes, ranges, name characters and rates are limited; nothing a client sends is ever run as code.
"""
import json, os, re, secrets, threading, time, urllib.parse
from http.server import ThreadingHTTPServer, BaseHTTPRequestHandler

HOST = os.environ.get("BEAMDECK_HUB_HOST", "0.0.0.0")
PORT = int(os.environ.get("BEAMDECK_HUB_PORT", "47141"))
DATA = os.environ.get("BEAMDECK_HUB_DATA", os.path.join(os.path.dirname(os.path.abspath(__file__)), "hub-data"))
UPDATE_DIR = os.path.join(DATA, "update")

ONLINE_S = 25            # a player silent for longer is offline
CONVOY_AWAY_S = 90       # ... and leaves their convoy after this long
MAX_BODY = 96 * 1024
MAX_OFFERS = 60          # the shared board holds this many jobs
POST_OFFERS_MAX = 8      # most jobs one request may add
CHAT_KEEP = 150
CONVOY_LIFE_S = 3 * 3600
OFFER_KEYS = ("id", "cargo", "scenario", "icon", "title", "load", "kind", "from", "stops", "km", "mins", "pay", "weight",
              "needs", "fragile", "hazard", "rush", "blurb", "trailer", "contract", "contract_label", "created", "expires", "chain")
KINDS = {"local", "regional", "long", "scenario", "custom"}
CODE_CHARS = "ABCDEFGHJKLMNPQRSTUVWXYZ"          # no I or O: they read as 1 and 0

lock = threading.RLock()
players = {}      # pid -> {name, truck, lat, lon, hdg, kmh, job, convoy, seen, ver, last_chat}
offers = {}       # id -> offer (shared board)
convoys = {}      # code -> {code, host, offer_id, members: [name], created}
chat = []         # {id, t, name, text, convoy}
chat_seq = [0]


# ------------------------------------------------------------------ cleaning what clients send
def clean_name(s):
    s = re.sub(r"[^A-Za-z0-9 _.\-]", "", str(s or "")).strip()[:20]
    return s


def clean_text(s, n=200):
    return re.sub(r"[\x00-\x1f\x7f]", " ", str(s or "")).strip()[:n]


def num(v, lo, hi, default=0.0):
    try:
        v = float(v)
    except (TypeError, ValueError):
        return default
    if v != v:
        return default
    return max(lo, min(hi, v))


def scalar_dict(d, keys=12):
    out = {}
    for k, v in list(d.items())[:keys] if isinstance(d, dict) else []:
        k = clean_text(k, 24)
        if isinstance(v, bool) or isinstance(v, (int, float)):
            out[k] = v
        elif isinstance(v, str):
            out[k] = clean_text(v, 80)
    return out


def clean_point(p):
    if not isinstance(p, dict):
        return None
    q = scalar_dict(p)
    q["lat"], q["lon"] = num(p.get("lat"), -90, 90), num(p.get("lon"), -180, 180)
    q["name"] = clean_text(p.get("name"), 60)
    return q


def clean_offer(o):
    """A job from a client, reduced to the fields the board uses and bounded. None when it is not a job."""
    if not isinstance(o, dict):
        return None
    oid = str(o.get("id", ""))
    if not re.fullmatch(r"[A-Za-z0-9_\-]{3,24}", oid) or not isinstance(o.get("cargo"), str):
        return None
    origin = clean_point(o.get("from"))
    stops = [clean_point(s) for s in (o.get("stops") or [])[:8]]
    if not origin or not stops or any(s is None for s in stops):
        return None
    out = {}
    for k in OFFER_KEYS:
        v = o.get(k)
        if k in ("from", "stops"):
            continue
        if isinstance(v, bool) or isinstance(v, (int, float)):
            out[k] = v if k not in ("km", "pay", "mins", "weight", "fragile") else num(v, 0, 60000)
        elif isinstance(v, dict) and k == "chain":
            out[k] = {"id": re.sub(r"[^A-Za-z0-9_\-]", "", str(v.get("id", "")))[:24], "step": int(num(v.get("step"), 1, 9, 2)),
                      "from": clean_name(v.get("from")), "gave": clean_text(v.get("gave"), 40), "text": clean_text(v.get("text"), 200)}
        elif isinstance(v, str):
            out[k] = clean_text(v, 20 if k == "needs" else 160)
        elif v is None:
            out[k] = None
    out["id"], out["cargo"] = oid, clean_text(o["cargo"], 40)
    out["from"], out["stops"] = origin, stops
    if out.get("kind") not in KINDS:
        out["kind"] = "regional"
    if not isinstance(out.get("needs"), str):
        return None                                  # what truck a job needs is how it is matched: a job without it is not a job
    out.setdefault("title", out["cargo"])
    out["shared"] = True
    return out


# ------------------------------------------------------------------ state
def say(text, name="", convoy=""):
    chat_seq[0] += 1
    chat.append({"id": chat_seq[0], "t": time.time(), "name": name, "text": text, "convoy": convoy})
    del chat[:-CHAT_KEEP]


def online(p, now):
    return now - p["seen"] < ONLINE_S


def sweep(now):
    for oid in [k for k, o in offers.items() if o.get("expires") and o["expires"] < now]:
        offers.pop(oid, None)
    for pid in [k for k, p in players.items() if now - p["seen"] > 3600]:
        players.pop(pid, None)
    away = {p["name"] for p in players.values() if now - p["seen"] > CONVOY_AWAY_S}
    for code, c in list(convoys.items()):
        gone = [m for m in c["members"] if m in away]
        for m in gone:
            c["members"].remove(m)
            say(f"{m} left the convoy.", convoy=code)
        if c["members"] and c["host"] not in c["members"]:
            c["host"] = c["members"][0]
        if not c["members"] or now - c["created"] > CONVOY_LIFE_S:
            convoys.pop(code, None)


def unique_name(pid, want, now):
    want = clean_name(want) or "Driver"
    taken = {p["name"].lower() for k, p in players.items() if k != pid and online(p, now)}
    name, n = want, 1
    while name.lower() in taken:
        n += 1
        name = f"{want[:17]} {n}"
    return name


def convoy_of(name):
    return next((c for c in convoys.values() if name in c["members"]), None)


def snapshot(pid, since=0):
    now = time.time()
    sweep(now)
    me = players.get(pid)
    mine = convoy_of(me["name"]) if me else None
    drivers = {}
    plist = []
    for k, p in players.items():
        if not online(p, now):
            continue
        job = p.get("job") or {}
        if job.get("offer"):
            drivers[job["offer"]] = drivers.get(job["offer"], 0) + 1
        c = convoy_of(p["name"])
        plist.append({"name": p["name"], "me": k == pid, "truck": p.get("truck") or {}, "lat": p.get("lat"), "lon": p.get("lon"),
                      "hdg": p.get("hdg", 0), "kmh": p.get("kmh", 0), "job": job, "convoy": c["code"] if c else "",
                      "ver": p.get("ver", ""), "ago": round(now - p["seen"], 1)})
    olist = []
    for o in offers.values():
        olist.append(o | {"drivers": drivers.get(o["id"], 0)})
    clist = []
    for c in convoys.values():
        o = offers.get(c["offer_id"]) or {}
        clist.append({"code": c["code"], "host": c["host"], "members": list(c["members"]), "offer_id": c["offer_id"],
                      "title": o.get("title", ""), "km": o.get("km", 0), "pay": o.get("pay", 0),
                      "from": (o.get("from") or {}).get("name", ""), "to": ((o.get("stops") or [{}])[-1]).get("name", ""),
                      "mine": bool(mine and mine["code"] == c["code"])})
    mycode = mine["code"] if mine else ""
    msgs = [m for m in chat if m["id"] > since and (not m["convoy"] or m["convoy"] == mycode)]
    if not since:
        msgs = msgs[-40:]
    return {"ok": True, "now": now, "name": me["name"] if me else "", "players": plist, "offers": olist, "convoys": clist,
            "convoy": mycode, "chat": msgs, "chat_id": chat_seq[0], "hub": 1}


# ------------------------------------------------------------------ actions
def presence(b):
    pid = str(b.get("pid", ""))[:40]
    if not re.fullmatch(r"[A-Za-z0-9\-]{8,40}", pid):
        return 400, {"ok": False, "error": "bad pid"}
    now = time.time()
    p = players.get(pid)
    if not p:
        p = players[pid] = {"name": unique_name(pid, b.get("name"), now), "last_chat": 0.0}
        say(f"{p['name']} joined.")
    elif clean_name(b.get("name")) and clean_name(b.get("name")) != p["name"]:
        new = unique_name(pid, b.get("name"), now)
        old, p["name"] = p["name"], new
        for c in convoys.values():
            c["members"] = [new if m == old else m for m in c["members"]]
            if c["host"] == old:
                c["host"] = new
    truck = b.get("truck") if isinstance(b.get("truck"), dict) else {}
    job = b.get("job") if isinstance(b.get("job"), dict) else {}
    p.update(seen=now, truck={"name": clean_text(truck.get("name"), 40), "label": clean_text(truck.get("label"), 40)},
             lat=num(b.get("lat"), -90, 90, None) if b.get("lat") is not None else None,
             lon=num(b.get("lon"), -180, 180, None) if b.get("lon") is not None else None,
             hdg=round(num(b.get("hdg"), 0, 360)), kmh=round(num(b.get("kmh"), 0, 600)), ver=clean_text(b.get("ver"), 16),
             job={"title": clean_text(job.get("title"), 60), "phase": clean_text(job.get("phase"), 16),
                  "pct": round(num(job.get("pct"), 0, 100)), "offer": clean_text(job.get("offer"), 24),
                  "to": clean_text(job.get("to"), 60)} if job.get("title") else {})
    return 200, snapshot(pid, int(num(b.get("since"), 0, 1e9)))


def post_offers(b):
    pid = str(b.get("pid", ""))
    if pid not in players:
        return 403, {"ok": False, "error": "send presence first"}
    added = 0
    force = bool(b.get("force"))
    for raw in (b.get("offers") or [])[:POST_OFFERS_MAX]:
        o = clean_offer(raw)
        if not o or o["id"] in offers:
            continue
        if len(offers) >= MAX_OFFERS:
            if not force:
                break
            kept = {c["offer_id"] for c in convoys.values()}          # a convoy's job stays
            old = sorted((x for x in offers.values() if x["id"] not in kept), key=lambda x: x.get("created") or 0)
            if not old:
                break
            offers.pop(old[0]["id"], None)
        offers[o["id"]] = o
        added += 1
        if (o.get("chain") or {}).get("text"):
            say(o["chain"]["text"])                           # a delivery just made a job: the radio says so
    return 200, {"ok": True, "added": added, "count": len(offers)}


def convoy_action(b):
    pid = str(b.get("pid", ""))
    me = players.get(pid)
    if not me:
        return 403, {"ok": False, "error": "send presence first"}
    act, name = b.get("action"), me["name"]
    cur = convoy_of(name)
    if act == "leave":
        if cur:
            cur["members"].remove(name)
            say(f"{name} left the convoy.", convoy=cur["code"])
            if cur["members"] and cur["host"] == name:
                cur["host"] = cur["members"][0]
            if not cur["members"]:
                convoys.pop(cur["code"], None)
        return 200, {"ok": True}
    if act == "create":
        o = clean_offer(b.get("offer"))
        if not o:
            return 400, {"ok": False, "error": "that job can't be shared"}
        if cur:
            return 409, {"ok": False, "error": f"You are already in convoy {cur['code']}: leave it first"}
        offers.setdefault(o["id"], o)
        o = offers[o["id"]]
        o["expires"] = max(o.get("expires") or 0, time.time() + CONVOY_LIFE_S)
        code = "".join(secrets.choice(CODE_CHARS) for _ in range(4))
        while code in convoys:
            code = "".join(secrets.choice(CODE_CHARS) for _ in range(4))
        convoys[code] = {"code": code, "host": name, "offer_id": o["id"], "members": [name], "created": time.time()}
        say(f"{name} opened convoy {code}: {o.get('title', 'a job')}.")
        return 200, {"ok": True, "code": code, "offer": o}
    if act == "join":
        code = re.sub(r"[^A-Za-z]", "", str(b.get("code", ""))).upper()[:4]
        c = convoys.get(code)
        if not c:
            return 404, {"ok": False, "error": "No convoy with that code"}
        if cur and cur["code"] != code:
            return 409, {"ok": False, "error": f"You are already in convoy {cur['code']}: leave it first"}
        if len(c["members"]) >= 8:
            return 409, {"ok": False, "error": "That convoy is full"}
        if name not in c["members"]:
            c["members"].append(name)
            say(f"{name} joined the convoy.", convoy=code)
        return 200, {"ok": True, "code": code, "offer": offers.get(c["offer_id"])}
    return 400, {"ok": False, "error": "unknown action"}


def chat_post(b):
    pid = str(b.get("pid", ""))
    me = players.get(pid)
    if not me:
        return 403, {"ok": False, "error": "send presence first"}
    now = time.time()
    if now - me["last_chat"] < 1.0:
        return 429, {"ok": False, "error": "slow down"}
    text = clean_text(b.get("text"))
    if not text:
        return 400, {"ok": False, "error": "empty"}
    me["last_chat"] = now
    c = convoy_of(me["name"]) if b.get("convoy") else None
    say(text, me["name"], c["code"] if c else "")
    return 200, {"ok": True}


def done(b):
    pid = str(b.get("pid", ""))
    me = players.get(pid)
    if not me:
        return 403, {"ok": False, "error": "send presence first"}
    pay, km = num(b.get("pay"), 0, 60000), num(b.get("km"), 0, 6000)
    stars = num(b.get("stars"), 0, 5)
    name = me["name"]
    n = int(num(b.get("convoy_n"), 0, 8))
    say(f"{name} delivered {clean_text(b.get('title'), 50) or 'a job'}: ${pay:,.0f}, {stars:.1f} stars"
        + (f", convoy of {n}." if n > 1 else "."))
    return 200, {"ok": True}


ROUTES = {"/api/presence": presence, "/api/offers": post_offers, "/api/convoy": convoy_action, "/api/chat": chat_post,
          "/api/done": done}


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "BeamDeckHub/1"

    def log_message(self, *a):
        pass

    def reply(self, code, obj, ctype="application/json", raw=None):
        data = raw if raw is not None else json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        path = urllib.parse.urlparse(self.path).path
        if path == "/api/ping":
            with lock:
                n = sum(1 for p in players.values() if online(p, time.time()))
            return self.reply(200, {"ok": True, "hub": 1, "online": n})
        if path == "/update/manifest.json" or path.startswith("/update/files/"):
            rel = "manifest.json" if path.endswith("manifest.json") else os.path.basename(urllib.parse.unquote(path))
            full = os.path.join(UPDATE_DIR, "files", rel) if rel != "manifest.json" else os.path.join(UPDATE_DIR, rel)
            if not re.fullmatch(r"[A-Za-z0-9._\-]{1,80}", rel) or not os.path.isfile(full):
                return self.reply(404, {"ok": False, "error": "not found"})
            ctype = "application/json" if rel.endswith(".json") else "application/octet-stream"
            size = os.path.getsize(full)
            self.send_response(200)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(size))
            self.end_headers()
            with open(full, "rb") as f:
                while True:
                    chunk = f.read(1 << 16)
                    if not chunk:
                        break
                    self.wfile.write(chunk)
            return
        self.reply(404, {"ok": False, "error": "not found"})

    def do_POST(self):
        path = urllib.parse.urlparse(self.path).path
        fn = ROUTES.get(path)
        if not fn:
            return self.reply(404, {"ok": False, "error": "not found"})
        try:
            n = int(self.headers.get("Content-Length") or 0)
            if n > MAX_BODY:
                return self.reply(413, {"ok": False, "error": "too big"})
            body = json.loads(self.rfile.read(n) or b"{}")
            if not isinstance(body, dict):
                raise ValueError
        except Exception:
            return self.reply(400, {"ok": False, "error": "bad request"})
        with lock:
            code, obj = fn(body)
        self.reply(code, obj)


def main():
    os.makedirs(os.path.join(UPDATE_DIR, "files"), exist_ok=True)
    srv = ThreadingHTTPServer((HOST, PORT), Handler)
    srv.daemon_threads = True
    print(f"BeamDeck freight hub on {HOST}:{PORT} (data in {DATA})", flush=True)
    srv.serve_forever()


if __name__ == "__main__":
    main()
