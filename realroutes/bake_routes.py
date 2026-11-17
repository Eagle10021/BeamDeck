#!/usr/bin/env python3
"""Real Routes baker: a GTFS feed -> compact route packs for the BeamNG mod and the tablet console.

    python3 bake_routes.py                       # OC Transpo, every bus route
    python3 bake_routes.py --routes 111,189,53   # only these
    python3 bake_routes.py --gtfs path/or/url.zip --agency myCity

Output (default ~/.local/share/realroutes/<agency>/):
  index.json          routes -> directions -> patterns (stop counts, trips per weekday, colours)
  calendar.json       service_id -> weekdays/date range/exceptions
  routes/<route>.json one file per route: its patterns, each with stops, a simplified shape and every trip's times

A "pattern" is one exact stop sequence (most routes have 1-4 per direction: short turns, branches).
Trip times are stored as a start time plus an index into the pattern's list of distinct time profiles
(offsets from the first stop), which keeps a busy route like the 111 to a few hundred KB.
Standard library only.
"""
import argparse, csv, io, json, math, os, re, sys, time, urllib.request, zipfile
from collections import defaultdict

OC_GTFS = "https://oct-gtfs-emasagcnfmcgeham.z01.azurefd.net/public-access/GTFSExport.zip"
UA = "Mozilla/5.0 (X11; Linux x86_64) realroutes-baker"
BUS = {"3", "700", "702", "704", "715"}  # GTFS bus route_types (basic + extended)


def secs(t):
    h, m, s = t.split(":")
    return int(h) * 3600 + int(m) * 60 + int(s)


def fetch_gtfs(src, cache_dir):
    if os.path.exists(src):
        return src
    os.makedirs(cache_dir, exist_ok=True)
    path = os.path.join(cache_dir, "gtfs.zip")
    tag_file = path + ".etag"
    req = urllib.request.Request(src, method="HEAD", headers={"User-Agent": UA})
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            tag = r.headers.get("ETag") or r.headers.get("Last-Modified") or ""
    except Exception as e:
        print("HEAD failed, using the cached feed:", e, file=sys.stderr)
        tag = None
    old = open(tag_file).read() if os.path.exists(tag_file) else None
    if tag is not None and (not os.path.exists(path) or old != tag):
        print("downloading", src)
        req = urllib.request.Request(src, headers={"User-Agent": UA})
        with urllib.request.urlopen(req, timeout=600) as r, open(path + ".part", "wb") as f:
            while chunk := r.read(1 << 20):
                f.write(chunk)
        os.replace(path + ".part", path)
        open(tag_file, "w").write(tag)
    return path


def nice(name):
    """BASELINE / TUNNEY'S PASTURE -> Baseline / Tunney's Pasture (str.title() gives Tunney'S)."""
    t = name.title()
    t = re.sub(r"'S\b", "'s", t)
    return re.sub(r"\b(Hs|H\.S|Ii|Iii|Ne|Nw|Se|Sw|Cs|Ad)\b", lambda m: m.group(1).upper(), t)


def simplify(pts, tol_m=4.0):
    """Douglas-Peucker on lat/lon points (local equirectangular metres)."""
    if len(pts) < 3:
        return pts
    lat0 = math.radians(pts[0][0])
    kx, ky = 111320 * math.cos(lat0), 110574
    xy = [((p[1]) * kx, (p[0]) * ky) for p in pts]
    keep = [False] * len(pts)
    keep[0] = keep[-1] = True
    stack = [(0, len(pts) - 1)]
    while stack:
        a, b = stack.pop()
        ax, ay = xy[a]; bx, by = xy[b]
        dx, dy = bx - ax, by - ay
        L = math.hypot(dx, dy) or 1e-9
        best, bi = -1, -1
        for i in range(a + 1, b):
            px, py = xy[i]
            d = abs(dy * (px - ax) - dx * (py - ay)) / L
            if d > best:
                best, bi = d, i
        if best > tol_m:
            keep[bi] = True
            stack += [(a, bi), (bi, b)]
    return [p for p, k in zip(pts, keep) if k]


DEFAULT_OUT = os.path.expanduser("~/.local/share/realroutes")


def bake(gtfs=OC_GTFS, agency="octranspo", routes="", out_root=DEFAULT_OUT, cache_root=None):
    """Bake every bus route (or the comma list `routes`) of a GTFS feed. Returns the output folder."""
    a = argparse.Namespace(gtfs=gtfs, agency=agency, routes=routes, out=out_root)
    cache_root = cache_root or os.path.join(os.path.expanduser("~/.cache/realroutes"), agency)
    out = os.path.join(a.out, a.agency)
    os.makedirs(os.path.join(out, "routes"), exist_ok=True)
    t0 = time.time()
    z = zipfile.ZipFile(fetch_gtfs(a.gtfs, cache_root))
    rd = lambda n: csv.DictReader(io.TextIOWrapper(z.open(n), encoding="utf-8-sig"))
    names = set(z.namelist())

    want = {x.strip() for x in a.routes.split(",") if x.strip()}
    routes = {}
    for r in rd("routes.txt"):
        if r.get("route_type", "3") not in BUS:
            continue
        if want and r["route_short_name"] not in want:
            continue
        routes[r["route_id"]] = r
    trips = {r["trip_id"]: r for r in rd("trips.txt") if r["route_id"] in routes}
    stops = {r["stop_id"]: r for r in rd("stops.txt")}
    print(f"{len(routes)} routes, {len(trips)} trips")

    # stop_times: the big file, streamed once
    st = defaultdict(list)
    for r in rd("stop_times.txt"):
        if r["trip_id"] in trips:
            st[r["trip_id"]].append((int(r["stop_sequence"]), r["stop_id"],
                                     secs(r["arrival_time"] or r["departure_time"]),
                                     secs(r["departure_time"] or r["arrival_time"])))
    print(f"stop_times read ({time.time() - t0:.0f}s)")

    shapes = defaultdict(list)
    if "shapes.txt" in names:
        used = {t["shape_id"] for t in trips.values() if t.get("shape_id")}
        for r in rd("shapes.txt"):
            if r["shape_id"] in used:
                shapes[r["shape_id"]].append((int(r["shape_pt_sequence"]), float(r["shape_pt_lat"]), float(r["shape_pt_lon"])))

    cal = {}
    if "calendar.txt" in names:
        for r in rd("calendar.txt"):
            cal[r["service_id"]] = {"days": "".join(r[d] for d in ("monday", "tuesday", "wednesday", "thursday",
                                                                     "friday", "saturday", "sunday")),
                                    "start": r["start_date"], "end": r["end_date"], "add": [], "remove": []}
    if "calendar_dates.txt" in names:
        for r in rd("calendar_dates.txt"):
            c = cal.setdefault(r["service_id"], {"days": "0000000", "start": r["date"], "end": r["date"], "add": [], "remove": []})
            (c["add"] if r["exception_type"] == "1" else c["remove"]).append(r["date"])

    index = []
    by_route = defaultdict(list)
    for tid, t in trips.items():
        by_route[t["route_id"]].append(tid)

    for rid, r in sorted(routes.items(), key=lambda kv: (len(kv[1]["route_short_name"]), kv[1]["route_short_name"])):
        patterns = {}  # (direction, stop tuple) -> pattern
        for tid in by_route[rid]:
            lst = sorted(st.get(tid, []))
            if len(lst) < 2:
                continue
            t = trips[tid]
            key = (t.get("direction_id", "0"), tuple(x[1] for x in lst))
            p = patterns.get(key)
            if not p:
                p = patterns[key] = {"direction": key[0], "stop_ids": key[1], "headsigns": defaultdict(int),
                                     "shape_id": t.get("shape_id"), "profiles": {}, "trips": []}
            p["headsigns"][t.get("trip_headsign") or ""] += 1
            start = lst[0][3]
            prof = tuple(x[2] - start for x in lst) + tuple(x[3] - start for x in lst)
            pi = p["profiles"].setdefault(prof, len(p["profiles"]))
            p["trips"].append([tid, t["service_id"], start, pi])
        if not patterns:
            continue

        packs, idx_dirs = [], defaultdict(list)
        for n, ((direction, _), p) in enumerate(sorted(patterns.items(), key=lambda kv: (kv[0][0], -len(kv[1]["trips"])))):
            pid = f"{r['route_short_name']}-{direction}-{n}"
            headsign = max(p["headsigns"], key=p["headsigns"].get)
            sl = []
            for sid in p["stop_ids"]:
                s = stops[sid]
                sl.append({"id": sid, "code": s.get("stop_code", ""), "name": nice(s["stop_name"]),
                           "lat": round(float(s["stop_lat"]), 6), "lon": round(float(s["stop_lon"]), 6)})
            shape = []
            if p["shape_id"] in shapes:
                shape = [[round(la, 6), round(lo, 6)] for _, la, lo in sorted(shapes[p["shape_id"]])]
                shape = simplify(shape)
            nstop = len(sl)
            profiles = [None] * len(p["profiles"])
            for prof, i in p["profiles"].items():
                profiles[i] = {"arr": list(prof[:nstop]), "dep": list(prof[nstop:])}
            p["trips"].sort(key=lambda x: x[2])
            packs.append({"id": pid, "direction": direction, "headsign": headsign, "stops": sl, "shape": shape,
                          "profiles": profiles, "trips": p["trips"]})
            idx_dirs[direction].append({"id": pid, "headsign": headsign, "stops": nstop,
                                        "from": sl[0]["name"], "to": sl[-1]["name"], "trips": len(p["trips"]),
                                        "minutes": round(profiles[0]["arr"][-1] / 60)})
        route = {"route": r["route_short_name"], "name": r.get("route_long_name", ""),
                 "color": r.get("route_color") or "555555", "text": r.get("route_text_color") or "FFFFFF"}
        json.dump(route | {"patterns": packs}, open(os.path.join(out, "routes", f"{r['route_short_name']}.json"), "w"),
                  separators=(",", ":"))
        index.append(route | {"directions": [{"direction": d, "patterns": v} for d, v in sorted(idx_dirs.items())]})

    feed = {}
    if "feed_info.txt" in names:
        feed = next(iter(rd("feed_info.txt")), {})
    json.dump({"agency": a.agency, "built": time.strftime("%Y-%m-%dT%H:%M:%S"), "feed": feed, "routes": index},
              open(os.path.join(out, "index.json"), "w"), separators=(",", ":"))
    json.dump(cal, open(os.path.join(out, "calendar.json"), "w"), separators=(",", ":"))
    size = sum(os.path.getsize(os.path.join(out, "routes", f)) for f in os.listdir(os.path.join(out, "routes")))
    print(f"{len(index)} routes baked to {out} ({size / 1e6:.1f} MB) in {time.time() - t0:.0f}s")
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--gtfs", default=OC_GTFS)
    ap.add_argument("--agency", default="octranspo")
    ap.add_argument("--routes", default="", help="comma list of route short names (default: every bus route)")
    ap.add_argument("--out", default=DEFAULT_OUT)
    a = ap.parse_args()
    bake(a.gtfs, a.agency, a.routes, a.out)


if __name__ == "__main__":
    main()
