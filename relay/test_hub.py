#!/usr/bin/env python3
"""Runs the freight hub on a free port and checks what players see: names, the shared board, convoys, chat, the leaderboard,
and that bad input is refused. Usage: python3 relay/test_hub.py"""
import importlib.util, json, os, sys, tempfile, threading, urllib.request, urllib.error

here = os.path.dirname(os.path.abspath(__file__))
spec = importlib.util.spec_from_file_location("hub", os.path.join(here, "freight-hub.py"))
hub = importlib.util.module_from_spec(spec)
os.environ["BEAMDECK_HUB_DATA"] = tempfile.mkdtemp()
spec.loader.exec_module(hub)
hub.DATA = os.environ["BEAMDECK_HUB_DATA"]; hub.UPDATE_DIR = os.path.join(hub.DATA, "update")
os.makedirs(os.path.join(hub.UPDATE_DIR, "files"), exist_ok=True)
srv = hub.ThreadingHTTPServer(("127.0.0.1", 0), hub.Handler); srv.daemon_threads = True
threading.Thread(target=srv.serve_forever, daemon=True).start()
BASE = f"http://127.0.0.1:{srv.server_address[1]}"
bad = []


def call(path, body=None, raw=None):
    req = urllib.request.Request(BASE + path, data=raw if raw is not None else (json.dumps(body).encode() if body is not None else None),
                                 headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=5) as r:
            return r.status, r.read()
    except urllib.error.HTTPError as e:
        return e.code, e.read()


def j(path, body=None):
    code, data = call(path, body)
    try:
        return code, json.loads(data)
    except Exception:
        return code, data


def ok(c, m):
    print(("PASS " if c else "FAIL ") + m)
    if not c:
        bad.append(m)


def offer(oid="o1234abcd", pay=900):
    return {"id": oid, "cargo": "box", "title": "Boxes", "load": "boxes", "kind": "regional", "km": 120, "mins": 90, "pay": pay,
            "weight": 12, "needs": "box", "from": {"name": "Ottawa", "lat": 45.4, "lon": -75.7},
            "stops": [{"name": "Kingston", "lat": 44.2, "lon": -76.5}], "evil": "<script>", "expires": None}


A, B = "aaaaaaaa-1111", "bbbbbbbb-2222"
c, s = j("/api/presence", {"pid": A, "name": "Eagle", "lat": 45.4, "lon": -75.7, "kmh": 80, "truck": {"name": "Percheron", "label": "Box"}})
ok(c == 200 and s["name"] == "Eagle" and len(s["players"]) == 1, "a first player joins and sees themselves")
c, s = j("/api/presence", {"pid": B, "name": "Eagle"})
ok(s["name"] != "Eagle" and s["name"].startswith("Eagle"), f"a second player with the same name is made unique ({s['name']})")
ok(len(s["players"]) == 2, "both players see each other")
ok(call("/api/presence", raw=b"{nope")[0] == 400, "malformed JSON is refused")
ok(j("/api/presence", {"pid": "x"})[0] == 400, "a bad pid is refused")
ok(call("/api/presence", raw=b"x" * (hub.MAX_BODY + 10))[0] in (413, 400), "an oversized body is refused")
ok(j("/api/offers", {"pid": "zzzzzzzz-0000", "offers": [offer()]})[0] == 403, "a player that never said hello can't post jobs")

c, r = j("/api/offers", {"pid": A, "offers": [offer(), offer("bad id!"), {"id": "o2", "cargo": 1}, offer("o999aaaa", 99999999)]})
ok(r["added"] == 2, "only well-formed jobs are added (bad id and non-job refused)")
c, s = j("/api/presence", {"pid": B, "name": s["name"]})
shared = {o["id"]: o for o in s["offers"]}
ok("o1234abcd" in shared and "evil" not in shared["o1234abcd"] and shared["o1234abcd"]["shared"], "the board shows the job, stripped of unknown fields")
ok(shared["o1234abcd"]["needs"] == "box", "and what truck it needs stays a plain word")
ok(call("/api/offers", raw=json.dumps({"pid": A, "offers": [offer("ononeeds1") | {"needs": ["box"]}]}).encode())[1].find(b'"added": 0') >= 0, "a job whose 'needs' is not a word is refused")
ok(shared["o999aaaa"]["pay"] <= 60000, "an absurd pay is clamped")

c, r = j("/api/convoy", {"pid": A, "action": "create", "offer": offer("oconvoy01")})
code = r["code"]
ok(c == 200 and len(code) == 4 and code.isalpha() and code == code.upper(), f"a convoy gets a 4-letter code ({code})")
ok(j("/api/convoy", {"pid": A, "action": "create", "offer": offer("oconvoy02")})[0] == 409, "one convoy at a time")
ok(j("/api/convoy", {"pid": B, "action": "join", "code": "ZZZZ"})[0] == 404, "a wrong code is refused")
c, r = j("/api/convoy", {"pid": B, "action": "join", "code": code.lower()})
ok(c == 200 and r["offer"]["id"] == "oconvoy01", "a friend joins with the code (any case) and gets the job")
c, s = j("/api/presence", {"pid": A, "name": "Eagle", "job": {"title": "Boxes", "phase": "driving", "pct": 40, "offer": "oconvoy01"}})
cv = next(x for x in s["convoys"] if x["code"] == code)
ok(len(cv["members"]) == 2 and cv["mine"] and s["convoy"] == code, "the convoy lists both drivers")
ok(next(o for o in s["offers"] if o["id"] == "oconvoy01")["drivers"] == 1, "the job shows how many are driving it")

j("/api/chat", {"pid": A, "text": "global hello"})
import time; time.sleep(1.1)
j("/api/chat", {"pid": A, "text": "convoy only", "convoy": True})
c, s = j("/api/presence", {"pid": B, "name": "x", "since": 0})
texts = [m["text"] for m in s["chat"]]
ok("convoy only" in texts and "global hello" in texts, "convoy members see convoy chat and global chat")
C = "cccccccc-3333"
j("/api/presence", {"pid": C, "name": "Third"})
c, s = j("/api/presence", {"pid": C, "name": "Third"})
ok("convoy only" not in [m["text"] for m in s["chat"]] and "global hello" in [m["text"] for m in s["chat"]], "outsiders don't see convoy chat")
ok(j("/api/chat", {"pid": A, "text": "again"})[0] == 429, "chat is rate limited")
ok(j("/api/chat", {"pid": A, "text": "  \x00\x01 "})[0] in (400, 429), "empty chat is refused")

c, r = j("/api/done", {"pid": A, "pay": 1500, "km": 200, "stars": 4.5, "title": "Boxes", "convoy_n": 2})
c, s = j("/api/presence", {"pid": A, "name": "Eagle"})
ok(any("delivered Boxes" in m["text"] and "convoy of 2" in m["text"] for m in s["chat"]), "a delivery is announced on the radio")

# a chain job (made by a delivery) is kept with its chain, and the radio says what happened
cj = offer("ochain0001") | {"chain": {"id": "x", "step": 2, "from": "Eagle", "gave": "logs", "text": "Eagle delivered logs to Cardinal. Lumber is ready there.", "evil": "<b>"}}
c, r = j("/api/offers", {"pid": A, "offers": [cj], "force": True})
c, s = j("/api/presence", {"pid": A, "name": "Eagle"})
got = next((o for o in s["offers"] if o["id"] == "ochain0001"), None)
ok(got and got["chain"]["step"] == 2 and got["chain"]["from"] == "Eagle", "a job made by a delivery keeps its chain")
ok(got and set(got["chain"]) == {"id", "step", "from", "gave", "text"}, "only the chain's own fields are kept")
ok(any("Lumber is ready there" in m["text"] for m in s["chat"]), "and the radio announces it")
ok("leaderboard" not in s, "there is no leaderboard")

# a job a player is taking goes on the shared board even when it is full, replacing the oldest (a convoy's job stays)
hub.offers.clear()
for i in range(hub.MAX_OFFERS):
    hub.offers[f"ofill{i:04d}"] = hub.clean_offer(offer(f"ofill{i:04d}") | {"created": 1000 + i})
c, r = j("/api/offers", {"pid": A, "offers": [offer("onew00001")]})
ok(r["added"] == 0, "a full board refuses ordinary new jobs")
before = dict(hub.offers); oldest = min(before.values(), key=lambda x: x.get("created") or 0)["id"]
c, r = j("/api/offers", {"pid": A, "offers": [offer("onew00001")], "force": True})
ok(r["added"] == 1 and "onew00001" in hub.offers and oldest not in hub.offers and len(hub.offers) == hub.MAX_OFFERS, "a job being taken replaces the oldest one")

j("/api/convoy", {"pid": B, "action": "leave"})
j("/api/convoy", {"pid": A, "action": "leave"})
c, s = j("/api/presence", {"pid": A, "name": "Eagle"})
ok(not any(x["code"] == code for x in s["convoys"]), "an empty convoy is removed")

open(os.path.join(hub.UPDATE_DIR, "manifest.json"), "w").write('{"version": "1.0.0"}')
open(os.path.join(hub.UPDATE_DIR, "files", "app.zip"), "wb").write(b"zipdata")
ok(call("/update/manifest.json")[1] == b'{"version": "1.0.0"}', "the update manifest is served")
ok(call("/update/files/app.zip")[1] == b"zipdata", "an update file is served")
ok(call("/update/files/..%2F..%2Fhub.json")[0] == 404, "path tricks can't read other files")
ok(call("/update/files/hub.json")[0] == 404, "only files in the update folder are served")

hub.players[A]["seen"] -= 200
c, s = j("/api/presence", {"pid": B, "name": "x"})
ok(all(p["name"] != "Eagle" for p in s["players"]), "a silent player drops off the list")
print("\nall hub tests passed" if not bad else f"\n{len(bad)} FAILED")
sys.exit(1 if bad else 0)
