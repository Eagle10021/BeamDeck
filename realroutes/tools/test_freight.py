"""Headless test of Freight with a fake game: python3 tools/test_freight.py"""
import os, sys, tempfile, time
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
import types
import freight as F, freight_data as D
clock = [time.time()]                       # a clock the test controls, so seconds of driving take no time
F.time = types.SimpleNamespace(time=lambda: clock[0], strftime=time.strftime)

sent = []
fr = F.Freight(lambda cmds: sent.extend(cmds), tempfile.mkdtemp())
ok = lambda cond, msg: print(("PASS " if cond else "FAIL ") + msg) or (cond or sys.exit(1))
cmds = lambda name: [c for c in sent if c.get("cmd") == name]

# --- the game's truck list (the mod sends one model per message)
def model(m, brand, name, keys, typ="Truck"):
    return {"model": m, "brand": brand, "name": name, "type": typ,
            "configs": [{"key": k, "name": k, "specs": {"power": 400, "weight": 9000, "drive": "RWD", "trans": "Automatic"}} for k in keys]}
models = [
    model("percheron", "Cherrier", "Percheron", ["xl500_6x4_fifthwheel_m", "s310_4x2_flatbed_M", "xl500_6x4_box_M", "s410_6x4_tanker_M", "s310_6x6_mixer_A"]),
    model("us_semi", "Gavril", "T-Series", ["t83_longhaul", "t83s_flatbed", "t83s_dump", "t82_ramplow"]),
    model("pickup", "Gavril", "D-Series", ["d10_4wd_A", "d10_4wd_parkranger"]),
    model("citybus", "Wentward", "City Bus", ["city"]),
]
for i, md in enumerate(models):
    fr.on_vehicles({"type": "vehicles", "first": i == 0, "last": i == len(models) - 1, "model": md})
g = {(t["model"], t["key"]): t for t in fr.garage()}
ok(("percheron", "xl500_6x4_fifthwheel_m") in g and "tractor" in g[("percheron", "xl500_6x4_fifthwheel_m")]["caps"], "Percheron fifth-wheel config is a tractor")
ok(set(g[("percheron", "s310_4x2_flatbed_M")]["caps"]) >= {"flatbed", "drawbar"}, "rigid Percheron flatbed can pull drawbar trailers (eye hitch)")
ok(("us_semi", "t82_ramplow") not in g and not any(k[0] == "citybus" for k in g), "ramplow and buses are not freight trucks")
ok(g[("percheron", "xl500_6x4_fifthwheel_m")]["fav"] and fr.garage()[0]["model"] == "percheron", "Percheron is the favourite, listed first")

# --- the board: Ottawa market, the corridor towns, scenarios
pose = lambda lat, lon, **k: {"type": "pose", "lat": lat, "lon": lon, "speed": 0, "gx": 0, "gy": 0, "heading": 90, **k}
fr.update(pose(45.4215, -75.6972))
ok(fr.select_truck("percheron", "xl500_6x4_fifthwheel_m")[0], "select the Percheron tractor")
ok(fr.set_market("Ottawa", refresh=True)[0], "market for Ottawa")
b = fr.board()
by_id = {o["scenario"]: o for o in b["offers"] if o["scenario"]}
ok("express" in by_id and by_id["express"]["stops"][-1]["name"] == "Toronto", "Ottawa → Toronto Express is on the board")
ok("corridor_run" in by_id, "Corridor run is on the board")
via = [s["name"] for s in by_id["corridor_run"]["stops"]]
print("   corridor run drops:", via, f"{by_id['corridor_run']['km']} km, {by_id['corridor_run']['mins']} min, ${by_id['corridor_run']['pay']}")
ok(len(via) >= 4 and via[-1] == "Toronto" and all(v in D.HUB for v in via), "Corridor run drops in towns between Ottawa and Toronto")
ok("Brockville" in via and "Kingston" in via, "...including Brockville and Kingston")
ok(via == sorted(via, key=D.CORRIDOR.index), "...in driving order")
ok("Brockville" in D.HUB and "Kingston" in D.HUB and "Belleville" in D.HUB, "in-between towns (Brockville, Kingston, Belleville) exist")
ok(any(o["kind"] == "regional" for o in b["offers"]) and any(o["kind"] == "long" for o in b["offers"]), "regional and long-haul jobs generated")
ok(all(o["fits"] == (D.can_carry(fr.truck_caps(), D.CARGO_BY_ID[o["cargo"]])) for o in b["offers"]), "every offer says whether the truck fits")

# --- plan a custom job through Brockville
okp, off = fr.plan({"from": "Ottawa", "to": "Kingston", "via": ["Brockville"], "cargo": "dryvan"})
ok(okp and [s["name"] for s in off["stops"]] == ["Brockville", "Kingston"], "custom route Ottawa → Brockville → Kingston")

# --- a different truck: the rigid flatbed sees the drawbar jobs, not semi jobs
fr.select_truck("percheron", "s310_4x2_flatbed_M")
b2 = fr.board()
ok(any(o["fits"] and o["needs"] == "drawbar" for o in b2["offers"]) or any(c["fits"] and c["needs"] == "drawbar" for c in b2["cargo"]), "flatbed truck fits drawbar cargo")
ok(not [c for c in b2["cargo"] if c["needs"] == "tractor" and c["fits"]], "flatbed truck does not fit semi cargo")

# --- run a job end to end
fr.select_truck("percheron", "xl500_6x4_fifthwheel_m")
offer = by_id["corridor_run"]
sent.clear()
ok(fr.start(offer["id"]) == (True, ""), "start the corridor run")
ok(fr.state == "resolving" and len(cmds("places")) == 1 + len(offer["stops"]), "asks the game for a place at the pickup and each drop")
for c in cmds("places"):
    fr.on_places({"type": "places", "id": c["id"], "complete": True, "pending": False,
                  "places": [{"id": "x", "name": f"Business near {c['lat']:.2f}", "lat": c["lat"] + 0.001, "lon": c["lon"], "confidence": 0.9}]})
ok(fr.state == "spawning", "all places found: spawning")
sp = cmds("freightSpawn")[0]
ok(sp["truck"]["model"] == "percheron" and sp["needs"] == "tractor", "spawn request names the truck")
ok(sp["trailers"][0]["family"] == "eu" and sp["trailers"][0]["model"] == "eu_trailers", "Percheron gets the European trailers first")
ok(any(t["family"] == "us" for t in sp["trailers"]), "...then the US ones as a fallback")
fr.on_game({"type": "freight", "id": offer["id"], "event": "ready", "coupled": True, "trailer": "box3a", "trailerName": "eu_trailers box3a", "hitch": "fifthwheel_v2"})
ok(fr.state == "driving" and cmds("gpsTo") and cmds("markers"), "rig ready: driving, GPS and markers sent")
ok(fr.view()["job"]["rig"]["hitch"] == "fifthwheel_v2", "the view shows the hitch")

# telemetry flows through to the view
stops = fr.job["pts"][1:]
cur = (fr.job["pts"][0]["lat"], fr.job["pts"][0]["lon"])
fr.update(pose(*cur, speed=22.0, rpm=1450, gear=7, fuel=0.8, fuelCap=400, wT=88, oT=95, odo=123456, mass=36000, tr=1,
               has={"ret": True, "cb": True, "la": False}, ret=2, retOn=True, cc=True, ccT=22.0, ptoRpm=1500))
t = fr.view()["tel"]
ok(t["rpm"] == 1450 and t["water"] == 88 and t["oil"] == 95 and t["has"]["ret"] and t["ret"] == 2 and t["pto"] == 1500, "live truck stats and control states reach the view")
ok(fr.view()["tel"]["speed"] == 79 and fr.view()["tel"]["ccT"] == 79, "speed and cruise target in km/h")

# drive to each drop
for k, s in enumerate(fr.job["pts"][1:], start=1):
    ok(fr.job["idx"] == k, f"heading for drop {k}: {s['name']}")
    fr.update(pose(s["lat"], s["lon"], speed=15.0))          # arrives at speed: not a stop
    ok(fr.job["idx"] == k, "no delivery while still moving")
    fr.update(pose(s["lat"], s["lon"], speed=0.2))           # stopped at the drop
    if k < len(fr.job["pts"]) - 1:
        ok(fr.job["idx"] == k + 1, f"delivered drop {k}, now heading for the next")
ok(fr.state == "done", "last drop delivered: done")
sm = fr.summary
print("   summary:", {k: sm["score"][k] for k in ("base", "cond_f", "time_f", "fines", "pay", "stars")}, f"balance ${fr.stats['balance']}")
ok(sm["score"]["pay"] > 0 and fr.stats["jobs"] == 1 and fr.stats["history"], "paid, stats and history updated")
sent.clear(); fr.dismiss()
ok(cmds("freightClear") and fr.state == "idle", "dismiss removes the trailer and returns to the board")

# --- failures and rough driving
fr.set_market("Kingston", refresh=True)
o2 = next(o for o in fr.board()["offers"] if o["fits"] and o["kind"] != "scenario")
fr.start(o2["id"]); fr.on_game({"id": o2["id"], "event": "fail", "why": "No trailer fits this truck's hitch."})
ok(fr.state == "idle" and fr.summary and fr.summary["failed"], "a failed spawn returns to the board with the reason")
fr.start(o2["id"])
for c in cmds("places")[-(1 + len(o2["stops"])):]:
    fr.on_places({"id": c["id"], "complete": True, "pending": False, "places": []})
fr.on_game({"id": o2["id"], "event": "ready", "coupled": True, "trailer": "box3a"})
start_cond = fr.job["cond"]
for _ in range(240):                                           # twenty-four seconds of hard cornering, 10 Hz
    clock[0] += 0.1
    fr.update(pose(45.0, -75.0, speed=20.0, gx=9.0, gy=0, tr=1))
ok(fr.job["cond"] < start_cond and fr.job["jolts"] >= 1, f"hard moments hurt the load ({start_cond:.0f} → {fr.job['cond']:.0f})")
for _ in range(60):
    clock[0] += 0.1
    fr.update(pose(45.0, -75.0, speed=20.0, tr=0))
ok(fr.job["lost_s"] > 3 and fr.job.get("_lost_said"), "an unhooked trailer is noticed")
ok(fr.cancel()[0] and fr.state == "idle" and cmds("gpsClear"), "cancel clears the route")
print("\nall freight tests passed")
