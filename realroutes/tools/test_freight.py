"""Headless test of Freight with a fake game: python3 tools/test_freight.py"""
import json, os, sys, tempfile, time
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
import types
import freight as F, freight_data as D
clock = [time.time()]                       # a clock the test controls, so seconds of driving take no time
F.time = types.SimpleNamespace(time=lambda: clock[0], strftime=time.strftime)

sent = []
fr = F.Freight(lambda cmds: sent.extend(cmds), tempfile.mkdtemp())
def hold(lat, lon, secs, speed=0.0, **kw):
    """Stand (or drive) at a point for some seconds of the test clock, 10 poses a second."""
    for _ in range(int(secs * 10)):
        clock[0] += 0.1
        fr.update({"type": "pose", "lat": lat, "lon": lon, "speed": speed, "gx": 0, "gy": 0, "heading": 90, **kw})
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

# --- custom configs saved by the player: arbitrary names, recognised from their parts
def custom(model_name, key, parts, brand="B", name="N"):
    return {"model": model_name, "brand": brand, "name": name, "type": "Truck",
            "configs": [{"key": key, "name": key, "source": "Custom", "parts": parts, "specs": {}}]}
fr2 = F.Freight(lambda c: None, tempfile.mkdtemp())
cm = [custom("percheron", "ruck", ["percheron_trailerhitch", "percheron_upfit_platform_980_flatbed", "percheron_airtanks_R"], "Cherrier", "Percheron"),
      custom("percheron", "bof road", ["percheron_upfit_cargobox_clearance_F"], "Cherrier", "Percheron"),
      custom("us_semi", "Long", ["conventional_interior_fifthwheel", "us_semi_fifthwheel_top"], "Gavril", "T-Series"),
      custom("us_semi", "Home", ["conventional_interior_fifthwheel", "us_semi_ballhitch", "us_semi_flatbed_lights", "us_semi_flatbed_rear_guard", "us_semi_flatbed_softtop"], "Gavril", "T-Series"),
      custom("md_series", "busss", ["schoolbus_airtank", "schoolbus_body_c"], "Gavril", "MD-Series")]
for i, md in enumerate(cm):
    fr2.on_vehicles({"type": "vehicles", "first": i == 0, "last": i == len(cm) - 1, "model": md})
cg = {(x["model"], x["key"]): x for x in fr2.garage()}
ok(set(cg[("percheron", "ruck")]["caps"]) == {"flatbed", "drawbar"} and cg[("percheron", "ruck")]["custom"], "custom Percheron flatbed with an eye hitch is recognised from its parts")
ok(cg[("us_semi", "Long")]["caps"] == ["tractor"], "custom T-Series with a fifth wheel plate is a tractor")
ok(set(cg[("us_semi", "Home")]["caps"]) == {"flatbed", "light"}, "custom T-Series flatbed with a ball hitch")
ok(("percheron", "bof road") in cg and cg[("percheron", "bof road")]["caps"] == [], "a custom config nothing is known about is still listed, to be set by hand")
ok(("md_series", "busss") not in cg, "a custom school bus is not offered as a freight truck")
fr2.set_override("percheron", "bof road", ["tractor"])
ok(fr2.caps_of("percheron", "bof road") == {"tractor"}, "the player can set what an unknown custom config can do")
fr2.select_truck("percheron", "ruck")
ok(D.can_carry(fr2.truck_caps(), D.CARGO_BY_ID["drawbar_flat"]) and not D.can_carry(fr2.truck_caps(), D.CARGO_BY_ID["dryvan"]), "the custom flatbed takes drawbar jobs, not semi jobs")

# --- an older mod in the game is spotted instead of leaving a job stuck
fr3 = F.Freight(lambda c: None, tempfile.mkdtemp()); fr3.pose = {"lat": 45.4, "lon": -75.7, "_at": clock[0]}
fr3.models = fr.models
fr3.select_truck("percheron", "xl500_6x4_fifthwheel_m"); fr3.set_market("Ottawa", refresh=True)
ok(not fr3.mod_old(), "right after the helper starts the mod's version is simply not known yet")
fr3.on_hello({"type": "hello"})                                          # an old mod sends no version
ok(fr3.mod_old() and fr3.view()["mod_old"], "a game whose mod sends no version is reported as running an older mod")
fitting = next(o for o in fr3.market["offers"] if fr3.fits(o)[0])           # a job this truck can carry, so only the mod check can refuse it
okk, why = fr3.start(fitting["id"])
ok(not okk and "Ctrl+L" in why, "a job is not started against an older mod, and the reason says what to do")
fr3.on_hello({"type": "hello", "rr": F.MOD_REQUIRED})
ok(not fr3.mod_old(), "the current mod is accepted")

# --- the board: Ottawa market, the corridor towns, scenarios
pose = lambda lat, lon, **k: {"type": "pose", "lat": lat, "lon": lon, "speed": 0, "gx": 0, "gy": 0, "heading": 90, **k}
fr.update(pose(45.4215, -75.6972))
fr.on_hello({"type": "hello", "rr": F.MOD_REQUIRED})                     # the game's mod is current
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

# --- jobs made for the selected truck are mixed in with the general ones
tail = [o for o in b["offers"] if o.get("tailored")]
ok(len(tail) >= F.TAILORED_MIN and all(o["fits"] for o in tail), f"{len(tail)} jobs are made for the truck and every one fits it")
ok(len({o["cargo"] for o in tail}) >= 3, f"...in a variety of cargo ({sorted({o['cargo'] for o in tail})})")
ok(any(not o["fits"] for o in b["offers"]) and any(not o.get("tailored") and o["fits"] for o in b["offers"]), "...mixed in with general jobs (some fit by chance, some don't)")
fr_none = F.Freight(lambda c: None, tempfile.mkdtemp()); fr_none.pose = {"lat": 45.4, "lon": -75.7, "_at": clock[0]}
fr_none.set_market("Ottawa", refresh=True)
ok(not any(o.get("tailored") for o in fr_none.market["offers"]), "with no truck picked nothing is tailored")

# --- pay is worth the drive, and the market is alive
reg = [o for o in b["offers"] if o["kind"] != "scenario"]
loc = [o["pay"] for o in reg if o["kind"] == "local"]; lng = [o["pay"] for o in reg if o["kind"] == "long"]
ok(min(loc) >= 150 and min(lng) >= 1000, f"pay is worth it: local from ${min(loc)}, long haul from ${min(lng)}")
ok(by_id["express"]["pay"] >= 2500, f"the Ottawa → Toronto Express pays ${by_id['express']['pay']}")
ok(any(o["contract"] for o in reg), "some jobs carry an express or high-value contract")
n0 = len(fr.market["offers"]); ids0 = {o["id"] for o in fr.market["offers"]}; v0 = fr.v
clock[0] += 120; fr.view()
ok(len(fr.market["offers"]) > n0 and fr.v > v0, f"new random jobs turn up over time ({n0} → {len(fr.market['offers'])})")
clock[0] += 2000; fr.view()
ok(not any(o["expires"] and o["expires"] < clock[0] for o in fr.market["offers"]) and any(o["created"] > clock[0] - 5 for o in fr.market["offers"]), "old jobs leave the board and fresh ones replace them")
ok(any(o["scenario"] for o in fr.market["offers"]), "themed scenarios stay on the board")
fr.set_market("Ottawa", refresh=True)

# --- plan a custom job through Brockville
okp, off = fr.plan({"from": "Ottawa", "to": "Kingston", "via": ["Brockville"], "cargo": "dryvan"})
ok(okp and [s["name"] for s in off["stops"]] == ["Brockville", "Kingston"], "custom route Ottawa → Brockville → Kingston")

# --- the custom flatbed with an eye hitch gets Ottawa → Toronto work, and the others say who can do them
fr.models[0]["configs"].append({"key": "ruck", "name": "Ruck", "source": "Custom", "specs": {}, "parts": ["percheron_trailerhitch", "percheron_upfit_platform_980_flatbed"]})
fr.select_truck("percheron", "ruck"); bb = fr.board()
fl = {o["scenario"]: o for o in bb["offers"] if o["scenario"]}
ok(all(fl[k]["fits"] and fl[k]["stops"][-1]["name"] == "Toronto" for k in ("express_flatbed", "express_drawbar")), "Ottawa → Toronto flatbed and drawbar-flatbed jobs fit the Ruck")
ok(fl["corridor_flatbed"]["fits"] and len(fl["corridor_flatbed"]["stops"]) >= 4, "a corridor run with several drops fits the flatbed too")
ok(not fl["express"]["fits"] and fl["express"]["alts"]["n"] >= 1 and fl["express"]["alts"]["top"][0]["key"] == "xl500_6x4_fifthwheel_m", "the semi job says which of the player's trucks can do it")

# --- a different truck: the rigid flatbed sees the drawbar jobs, not semi jobs
fr.select_truck("percheron", "s310_4x2_flatbed_M")
b2 = fr.board()
ok(any(o["fits"] and o["needs"] == "drawbar" for o in b2["offers"]) or any(c["fits"] and c["needs"] == "drawbar" for c in b2["cargo"]), "flatbed truck fits drawbar cargo")
ok(not [c for c in b2["cargo"] if c["needs"] == "tractor" and c["fits"]], "flatbed truck does not fit semi cargo")

# --- run a job end to end
fr.select_truck("percheron", "xl500_6x4_fifthwheel_m")               # a new truck rebuilds the board
b = fr.board()
by_id = {o["scenario"]: o for o in b["offers"] if o["scenario"]}
offer = by_id["corridor_run"]
fr.update(pose(45.4215, -75.6972))                                    # the game is still talking (the clock jumped above)
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
ok(sp["deferLoad"] and 1400 <= F.dist_m((sp["at"]["lat"], sp["at"]["lon"]), (fr.job["pts"][0]["lat"], fr.job["pts"][0]["lon"])) <= 2700,
   "the truck is set down 1.5-2.6 km short of the pickup, with no load yet")
fr.on_game({"type": "freight", "id": offer["id"], "event": "ready"})
ok(fr.state == "to_pickup", "the truck is in place: drive to the pickup")
gp = cmds("gpsTo")[-1]; mk = cmds("markers")[-1]["stops"]
ok(gp["name"].startswith("Pickup:") and mk[0]["kind"] == "pickup", "GPS and a pickup marker point at the pickup")
ok(not cmds("freightLoad"), "nothing is loaded yet")
pk = fr.job["pts"][0]
hold(pk["lat"], pk["lon"], 1.0, speed=12.0)
ok(fr.state == "to_pickup", "driving through the pickup does not load")
hold(pk["lat"], pk["lon"], 0.3)
ok(fr.state == "loading" and cmds("freightLoad"), "stopping at the pickup starts loading and asks the game for the trailer")
ld = cmds("freightLoad")[-1]
ok(ld["trailers"][0]["model"] == "eu_trailers" and ld["kind"] == "none", "the trailer is hooked on at the pickup (a semi carries its load in the trailer)")
hold(pk["lat"], pk["lon"], 2.0, speed=8.0)                                      # rolling away while it loads
left0 = fr.job["load_left"]
hold(pk["lat"], pk["lon"], 1.0, speed=8.0)
ok(abs(fr.job["load_left"] - left0) < 0.01, "loading waits while the truck moves")
fr.on_game({"type": "freight", "id": offer["id"], "event": "loaded", "coupled": True, "trailer": "box3a", "trailerName": "eu_trailers box3a", "hitch": "fifthwheel_v2"})
ok(fr.state == "loading", "loading is not finished until the time is up")
hold(pk["lat"], pk["lon"], fr.job["load_s"] + 1)
ok(fr.state == "driving" and fr.job.get("started"), "loaded: the clock starts and the drive begins")
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
    hold(s["lat"], s["lon"], 0.5, speed=15.0)                # arrives at speed: not a stop
    ok(fr.job["idx"] == k, "no delivery while still moving")
    hold(s["lat"], s["lon"], 1.0)                            # stopped, still unloading
    ok(fr.job["idx"] == k and fr.view()["job"]["unload_pct"] > 0, "standing still at the drop starts the unloading")
    hold(s["lat"], s["lon"], F.UNLOAD_S)                     # the load is off
    if k < len(fr.job["pts"]) - 1:
        ok(fr.job["idx"] == k + 1, f"delivered drop {k}, now heading for the next")
ok(fr.state == "done", "last drop delivered: done")
sm = fr.summary
print("   summary:", {k: sm["score"][k] for k in ("base", "cond_f", "time_f", "fines", "pay", "stars")}, f"balance ${fr.stats['balance']}")
ok(sm["score"]["pay"] > 0 and fr.stats["jobs"] == 1 and fr.stats["history"], "paid, stats and history updated")
sent.clear(); fr.dismiss()
ok(cmds("freightClear") and fr.state == "idle", "dismiss removes the trailer and returns to the board")

# --- the truck's own built-in loads, by cargo
lk = F.load_kind
ok(lk("Steel pipes", "flatbed") == "pipes" and lk("Timber", "flatbed") == "wood" and lk("Marble slabs", "flatbed") == "marble" and lk("Bricks", "flatbed") == "boxes",
   "a rigid flatbed carries pipes, wood, a marble block or storage boxes to match the cargo")
ok(lk("Parcels", "box") == "pallets" and lk("Crushed stone", "dump") == "rocks", "a box body carries pallets, a tipper rocks")
ok(lk("Raw milk", "tanker") == "tanker:milk" and lk("Heating oil", "tanker") == "tanker:diesel" and lk("Petrol", "tanker") == "tanker:petroleum" and lk("Water", "tanker") == "tanker:water",
   "a tanker carries the right liquid")
ok(lk("Packaged goods", "tractor") == "none" and lk("Ready-mix concrete", "mixer") == "none", "a trailer or a mixer carries its load itself")

# --- failures and rough driving
fr.set_market("Kingston", refresh=True)
o2 = next(o for o in fr.board()["offers"] if o["fits"] and o["kind"] != "scenario")
fr.start(o2["id"]); fr.on_game({"id": o2["id"], "event": "fail", "why": "No trailer fits this truck's hitch."})
ok(fr.state == "idle" and fr.summary and fr.summary["failed"], "a failed spawn returns to the board with the reason")
fr.staging = False                                           # start at the pickup: it loads straight away
fr.update(pose(45.4215, -75.6972))
fr.start(o2["id"])
for c in cmds("places")[-(1 + len(o2["stops"])):]:
    fr.on_places({"id": c["id"], "complete": True, "pending": False, "places": []})
fr.on_game({"id": o2["id"], "event": "ready"})
ok(fr.state == "loading" and fr.job["pts"][0]["lat"] == [c for c in cmds("freightSpawn")][-1]["at"]["lat"], "starting at the pickup loads at once")
fr.on_game({"id": o2["id"], "event": "loaded", "coupled": True, "trailer": "box3a"})
hold(45.0, -75.0, fr.job["load_s"] + 1)
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

# --- options: tickets and penalties (ETS2/ATS style)
ok(fr.view()["tickets_on"] and fr.view()["damage_on"] and fr.view()["late_on"], "fines, damage and late rules are on by default")
fr.act({"action": "tickets", "on": False})
fr.act({"action": "damage", "on": False})
fr.act({"action": "late", "on": False})
ok(not fr.tickets and not fr.damage and not fr.late and not fr.view()["tickets_on"] and not fr.view()["damage_on"]
   and not fr.view()["late_on"], "each rule can be switched off on its own like ETS2/ATS")
fr.start(o2["id"])
for c in cmds("places")[-(1 + len(o2["stops"])):]:
    fr.on_places({"id": c["id"], "complete": True, "pending": False, "places": []})
fr.on_game({"id": o2["id"], "event": "ready"})
fr.on_game({"id": o2["id"], "event": "loaded", "coupled": True, "trailer": "box3a"})
hold(45.0, -75.0, fr.job["load_s"] + 1)
for _ in range(150):
    clock[0] += 0.1
    fr.update(pose(45.0, -75.0, speed=35.0, limit=10.0))
ok(fr.job["tickets"] == 0, "with tickets off, speeding does not issue tickets or fines")
fr.job["cond"] = 50.0
fr.job["started"] = time.time() - (fr.job["mins"] + 30) * 60
sc = fr._score(fr.job)
ok(sc["fines"] == 0 and sc["cond_f"] == 1.0 and sc["time_f"] == 1.0 and sc["stars"] == 5.0 and sc["pay"] == o2["pay"],
   "with every rule off, damaged cargo and late delivery still receive full pay and 5 stars")
# one rule at a time: only that part of the pay and stars is forgiven
# --- spare time on every job, and a clock that follows the game's pause
base_of = lambda o: (o["km"] / (36 if o["km"] < 30 else 58 if o["km"] < 150 else 76) * 60 + 10 + 4 * len(o["stops"])) * F.SLACK
plain = [o for o in fr.market["offers"] if not o.get("contract") and o["kind"] in ("local", "regional", "long")]
ok(plain and all(o["mins"] - base_of(o) >= 55 for o in plain), "every job has at least an hour of spare time on top of the driving time")
ok(F.extra_min(430) == 120 and 60 <= F.extra_min(20) < 70 and F.extra_min(430, "rush") == 60, "the spare time is an hour on a short job, two on a long haul, half on an express")
left0 = fr._job_view()["left"]
hold(45.0, -75.0, 120, speed=5.0, paused=True)
ok(abs(fr._job_view()["left"] - left0) <= 1 and fr._job_view()["paused"], "pausing the game stops the job's clock")
hold(45.0, -75.0, 30, speed=5.0)
ok(29 <= left0 - fr._job_view()["left"] <= 31 and not fr._job_view()["paused"], "and it runs again afterwards")
t_gone = clock[0]; clock[0] += 600                                     # ten minutes with the game closed or frozen
fr.update(pose(45.0, -75.0, speed=5.0))
ok(left0 - fr._job_view()["left"] <= 32, "the clock does not run while the game is not talking")
fr.act({"action": "late", "on": True})
sc = fr._score(fr.job)
ok(sc["cond_f"] == 1.0 and sc["time_f"] < 1.0 and sc["stars"] < 5.0, "late delivery on alone: the time penalty applies, damage is free")
fr.act({"action": "late", "on": False})
fr.act({"action": "damage", "on": True})
sc = fr._score(fr.job)
ok(sc["cond_f"] < 1.0 and sc["time_f"] == 1.0 and sc["stars"] < 5.0, "damage on alone: the damage penalty applies, lateness is free")
fr.cancel()
fr.act({"action": "tickets", "on": True})
fr.act({"action": "damage", "on": True})
fr.act({"action": "late", "on": True})
# an older save with the single "penalties" switch carries over to both new ones
_d = tempfile.mkdtemp()
json.dump({"penalties": False}, open(os.path.join(_d, "freight.json"), "w"))
_old = F.Freight(lambda c: None, _d)
ok(not _old.damage and not _old.late and _old.tickets, "an older save's single penalties switch carries over to damage and late")

# --- supply chains: a delivery becomes the next job, right where it was dropped off
drop = {"name": "Cardinal Sawmill", "lat": 44.79, "lon": -75.38, "hub": "Cardinal"}
chain_job = lambda cargo, chain=None: {"offer": {"id": "ochain001", "cargo": cargo, "chain": chain} if chain else {"id": "ochain001", "cargo": cargo},
                                       "pts": [{"name": "Start", "lat": 45.0, "lon": -75.0}, drop]}
nxt = fr._chain_offer(chain_job("logs"))
ok(nxt and nxt["cargo"] == "lumber" and nxt["from"]["name"] == "Cardinal Sawmill" and nxt["chain"]["step"] == 2,
   "delivering logs leaves lumber waiting at the drop-off")
ok("Cardinal Sawmill" in nxt["blurb"] and nxt["title"].startswith("Supply chain") and nxt["expires"] > time.time() + 3600, "the job says where it came from and stays around a while")
plain_pay = fr._make_offer(__import__("random").Random(1), D.CARGO_BY_ID["lumber"], nxt["from"], nxt["stops"], nxt["kind"])["pay"]
ok(nxt["pay"] > plain_pay, "and it pays more than the same haul would on its own")
nxt3 = fr._chain_offer(chain_job("lumber", nxt["chain"]))
ok(nxt3 and nxt3["cargo"] == "rigid_flatbed" and nxt3["chain"]["step"] == 3 and nxt3["chain"]["id"] == nxt["chain"]["id"], "the next link carries the chain on")
ok(nxt3["pay"] / max(1, fr._make_offer(__import__("random").Random(1), D.CARGO_BY_ID["rigid_flatbed"], nxt3["from"], nxt3["stops"], nxt3["kind"])["pay"]) > 1.2, "and later links pay more still")
ok(fr._chain_offer(chain_job("parcels")) is None and fr._chain_offer(chain_job("rigid_flatbed")) is None, "a chain ends: parcels and building materials leave nothing behind")
ends = set(D.CHAINS)
ok(all(v in D.CARGO_BY_ID for v in D.CHAINS.values()) and all(k in D.CARGO_BY_ID for k in D.CHAINS), "every chain link is a real cargo")
seen = set()
for k in D.CHAINS:                                                        # no chain loops
    x, n = k, 0
    while x in D.CHAINS and n < 10:
        x = D.CHAINS[x]; n += 1
    seen.add(n < 10)
ok(seen == {True}, "no chain goes round in a circle")
before = len(fr.market["offers"])
fr.job = chain_job("gravel") | {"cond": 100.0, "started": time.time() - 600, "dist_m": 20000, "tickets": 0, "jolts": 0, "crash": 0, "max_kmh": 70,
                                 "fuel0": None, "drop_times": [], "rig": None, "log": [], "mins": 60, "truck": {"name": "T"}, "idx": 2,
                                 "offer": {"id": "ochain002", "cargo": "gravel", "title": "Gravel", "icon": "", "load": "gravel", "km": 20, "pay": 800,
                                           "weight": 20, "stops": [drop], "needs": "tractor", "mins": 60, "hazard": False, "fragile": 0.1, "rush": False}}
fr.mp = None
fr._finish()
made = [o for o in fr.market["offers"] if o.get("chain")]
ok(len(fr.market["offers"]) == before + 1 and made and made[-1]["cargo"] == "concrete", "finishing a job puts its follow-up on the board")

print("\nall freight tests passed")
