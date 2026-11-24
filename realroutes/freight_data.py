"""Freight data: the towns jobs run between, the cargo catalogue (each kind with the rigs that can carry it), and how
a truck is classified from its model and config name. Everything here is plain data so it is easy to extend; the logic
lives in freight.py.

Rig vocabulary (what a truck can do):
  tractor   a semi tractor: pulls semi trailers through a fifth wheel (T-Series, Percheron, MD-Series fifth-wheel...)
  box       a rigid truck with a closed cargo box (the load rides in the body: no trailer)
  flatbed   a rigid flatbed
  tanker    a rigid tanker body
  dump      a tipper / dump body
  mixer     a concrete mixer
  drawbar   a rigid truck that can also tow a drawbar trailer (European combinations)
  light     pickups, vans and small trucks: tow a small utility trailer
"""
import re

# ---- towns and cities: name, lat, lon, tags. The Ottawa-Toronto corridor first (highway 416 / 401), then the
# neighbouring cities, then major cities worldwide so a market works wherever the player is. "port" marks harbours
# and rail-served yards (container jobs).
HUBS = [
    # Ottawa - Toronto and the towns between
    ("Ottawa", 45.4215, -75.6972, "corridor port"), ("Kanata", 45.3084, -75.8990, "corridor"),
    ("Smiths Falls", 44.9014, -76.0214, "corridor"), ("Perth", 44.8997, -76.2490, "corridor"),
    ("Brockville", 44.5895, -75.6843, "corridor port"), ("Prescott", 44.7153, -75.5159, "corridor port"),
    ("Cornwall", 45.0184, -74.7280, "corridor port"), ("Gananoque", 44.3303, -76.1613, "corridor"),
    ("Kingston", 44.2312, -76.4860, "corridor port"), ("Napanee", 44.2478, -76.9486, "corridor"),
    ("Belleville", 44.1628, -77.3832, "corridor"), ("Trenton", 44.1028, -77.5775, "corridor"),
    ("Brighton", 44.0422, -77.7400, "corridor"), ("Cobourg", 43.9594, -78.1677, "corridor port"),
    ("Port Hope", 43.9511, -78.2926, "corridor"), ("Bowmanville", 43.9129, -78.6874, "corridor"),
    ("Oshawa", 43.8971, -78.8658, "corridor port"), ("Whitby", 43.8975, -78.9429, "corridor"),
    ("Ajax", 43.8509, -79.0204, "corridor"), ("Pickering", 43.8384, -79.0868, "corridor"),
    ("Scarborough", 43.7731, -79.2578, "corridor"), ("Toronto", 43.6532, -79.3832, "corridor port"),
    ("Mississauga", 43.5890, -79.6441, "corridor"), ("Brampton", 43.7315, -79.7624, "corridor"),
    ("Peterborough", 44.3091, -78.3197, "corridor"), ("Lindsay", 44.3567, -78.7430, "corridor"),
    ("Kemptville", 45.0167, -75.6333, "corridor"), ("Merrickville", 44.9167, -75.8333, "corridor"),
    ("Winchester", 45.0928, -75.3500, "corridor"), ("Morrisburg", 44.9028, -75.1867, "corridor"),
    ("Cardinal", 44.7918, -75.3855, "corridor"), ("Mallorytown", 44.4833, -75.8667, "corridor"),
    ("Lansdowne", 44.4086, -76.0239, "corridor"), ("Deseronto", 44.2000, -77.0500, "corridor"),
    ("Colborne", 44.0050, -77.8850, "corridor"), ("Grafton", 44.0333, -78.0333, "corridor"),
    ("Markham", 43.8561, -79.3370, "corridor"), ("Vaughan", 43.8361, -79.4983, "corridor"),
    ("Oakville", 43.4675, -79.6877, "corridor"), ("Burlington", 43.3255, -79.7990, "corridor"),
    # around it
    ("Montreal", 45.5017, -73.5673, "port"), ("Hamilton", 43.2557, -79.8711, "port steel"),
    ("Niagara Falls", 43.0896, -79.0849, ""), ("Barrie", 44.3894, -79.6903, ""),
    ("Kitchener", 43.4516, -80.4925, ""), ("London", 42.9849, -81.2453, ""),
    ("Gatineau", 45.4765, -75.7013, ""), ("Quebec City", 46.8139, -71.2080, "port"),
    # North America
    ("New York", 40.7128, -74.0060, "port"), ("Boston", 42.3601, -71.0589, "port"),
    ("Philadelphia", 39.9526, -75.1652, "port"), ("Washington", 38.9072, -77.0369, ""),
    ("Detroit", 42.3314, -83.0458, ""), ("Chicago", 41.8781, -87.6298, "port"),
    ("Atlanta", 33.7490, -84.3880, ""), ("Miami", 25.7617, -80.1918, "port"),
    ("Dallas", 32.7767, -96.7970, ""), ("Houston", 29.7604, -95.3698, "port"),
    ("Denver", 39.7392, -104.9903, ""), ("Phoenix", 33.4484, -112.0740, ""),
    ("Los Angeles", 34.0522, -118.2437, "port"), ("San Francisco", 37.7749, -122.4194, "port"),
    ("Seattle", 47.6062, -122.3321, "port"), ("Vancouver", 49.2827, -123.1207, "port"),
    ("Calgary", 51.0447, -114.0719, ""), ("Edmonton", 53.5461, -113.4938, ""),
    ("Winnipeg", 49.8951, -97.1384, ""), ("Halifax", 44.6488, -63.5752, "port"),
    ("Mexico City", 19.4326, -99.1332, ""),
    # Europe
    ("London UK", 51.5074, -0.1278, "port"), ("Paris", 48.8566, 2.3522, ""), ("Berlin", 52.5200, 13.4050, ""),
    ("Hamburg", 53.5511, 9.9937, "port"), ("Munich", 48.1351, 11.5820, ""), ("Frankfurt", 50.1109, 8.6821, ""),
    ("Cologne", 50.9375, 6.9603, ""), ("Amsterdam", 52.3676, 4.9041, "port"), ("Rotterdam", 51.9244, 4.4777, "port"),
    ("Brussels", 50.8503, 4.3517, ""), ("Vienna", 48.2082, 16.3738, ""), ("Prague", 50.0755, 14.4378, ""),
    ("Warsaw", 52.2297, 21.0122, ""), ("Budapest", 47.4979, 19.0402, ""), ("Zurich", 47.3769, 8.5417, ""),
    ("Milan", 45.4642, 9.1900, ""), ("Rome", 41.9028, 12.4964, ""), ("Madrid", 40.4168, -3.7038, ""),
    ("Barcelona", 41.3851, 2.1734, "port"), ("Lyon", 45.7640, 4.8357, ""), ("Lisbon", 38.7223, -9.1393, "port"),
    ("Dublin", 53.3498, -6.2603, "port"), ("Copenhagen", 55.6761, 12.5683, "port"), ("Stockholm", 59.3293, 18.0686, "port"),
    ("Oslo", 59.9139, 10.7522, "port"), ("Helsinki", 60.1699, 24.9384, "port"), ("Istanbul", 41.0082, 28.9784, "port"),
    # Asia, Oceania, Africa, South America
    ("Tokyo", 35.6762, 139.6503, "port"), ("Osaka", 34.6937, 135.5023, "port"), ("Seoul", 37.5665, 126.9780, ""),
    ("Beijing", 39.9042, 116.4074, ""), ("Shanghai", 31.2304, 121.4737, "port"), ("Mumbai", 19.0760, 72.8777, "port"),
    ("Delhi", 28.7041, 77.1025, ""), ("Bangkok", 13.7563, 100.5018, ""), ("Singapore", 1.3521, 103.8198, "port"),
    ("Dubai", 25.2048, 55.2708, "port"), ("Sydney", -33.8688, 151.2093, "port"), ("Melbourne", -37.8136, 144.9631, "port"),
    ("Brisbane", -27.4698, 153.0251, "port"), ("Auckland", -36.8485, 174.7633, "port"),
    ("Cairo", 30.0444, 31.2357, ""), ("Johannesburg", -26.2041, 28.0473, ""), ("Cape Town", -33.9249, 18.4241, "port"),
    ("Sao Paulo", -23.5505, -46.6333, ""), ("Rio de Janeiro", -22.9068, -43.1729, "port"),
    ("Buenos Aires", -34.6037, -58.3816, "port"), ("Santiago", -33.4489, -70.6693, ""), ("Bogota", 4.7110, -74.0721, ""),
]
# the road, in driving order: highway 416 from Ottawa to the 401 at Prescott, then the 401 west to Toronto. Towns between
# two of these are the natural drops of a run along the corridor.
# Supply chains: what a delivery turns into at the drop-off. Delivering logs to a sawmill leaves lumber waiting there, lumber
# leaves building materials, and so on: the next job starts exactly where the last driver dropped off. Every chain ends.
CHAINS = {"logs": "lumber", "lumber": "rigid_flatbed", "steel": "pipes", "pipes": "rigid_flatbed", "gravel": "concrete",
          "milk": "dryvan", "container": "dryvan", "dryvan": "parcels"}

CORRIDOR = ["Ottawa", "Kemptville", "Prescott", "Brockville", "Mallorytown", "Lansdowne", "Gananoque", "Kingston", "Napanee",
            "Deseronto", "Belleville", "Trenton", "Brighton", "Colborne", "Cobourg", "Port Hope", "Bowmanville", "Oshawa",
            "Whitby", "Ajax", "Pickering", "Scarborough", "Toronto"]
HUB = {h[0]: {"name": h[0], "lat": h[1], "lon": h[2], "tags": h[3].split()} for h in HUBS}

# ---- where cargo comes from and goes to (BeamNG.Earth place categories; unknown names simply match nothing, and
# the job falls back to the town's centre) ----
SUPPLY = ["wholesale*", "warehouse*", "storage*", "manufactur*", "industrial*", "building_supply*", "hardware*",
          "grocery*", "supermarket", "group:shop"]
SHOPS = ["group:shop", "group:food", "grocery*", "supermarket", "hardware*", "department_store"]
FUEL = ["group:fuel", "gas_station"]
PORTS = ["port*", "harbor*", "harbour*", "marina*", "shipping*", "transportation*", "storage*", "industrial*", "group:transit"]
FARMS = ["farm*", "dairy*", "agricultur*", "food_processing*", "group:food"]
SITES = ["construction*", "building_supply*", "hardware*", "industrial*", "group:shop"]
QUARRY = ["quarr*", "mining*", "gravel*", "construction*", "industrial*"]
MILLS = ["sawmill*", "lumber*", "wood*", "forest*", "manufactur*", "industrial*"]

# ---- the cargo catalogue ----
# needs    which rig class carries it (see the module docstring)
# rate     pay per km before bonuses; weight  tonnes (low, high); fragile 0-1; hazard  dangerous goods
# trailers per truck family ("eu" Percheron and friends, "us" T-Series and friends): (model, config) in order of
#          preference; the mod tries them in turn against the truck's actual hitch. Rigid jobs have none.
# scenario themed job: fixed shape, shown in the Scenarios list
CARGO = [
    dict(id="dryvan", name="General freight", icon="📦", needs="tractor", rate=2.1, weight=(8, 20), fragile=0.25,
         loads=["Packaged goods", "Household appliances", "Office supplies", "Canned food", "Retail stock", "Furniture",
                "Paper rolls", "Electronics"],
         trailers=dict(eu=[("eu_trailers", "box3a")],
                       us=[("dryvan", "48ft_19200kg"), ("dryvan", "53ft_13500kg"), ("dryvan", "53ft_22400kg"),
                           ("dryvan", "28ft_9600kg")]),
         src=SUPPLY, dst=SHOPS),
    dict(id="drinks", name="Soft drinks", icon="🥤", needs="tractor", rate=2.0, weight=(12, 22), fragile=0.2,
         loads=["Cola pallets", "Bottled water", "Juice", "Beer kegs"],
         trailers=dict(eu=[("eu_trailers", "box3a_cola"), ("eu_trailers", "box3a_muhten"), ("eu_trailers", "box3a")],
                       us=[("dryvan", "48ft_19200kg"), ("dryvan", "53ft_22400kg")]),
         src=SUPPLY, dst=SHOPS),
    dict(id="steel", name="Steel coils", icon="🔩", needs="tractor", rate=2.7, weight=(18, 27), fragile=0.1,
         loads=["Steel coils", "Rolled steel", "Sheet metal"],
         trailers=dict(eu=[("eu_trailers", "flatbed3a")],
                       us=[("flatbed", "48ft_coil"), ("flatbed", "53ft_coils_double"), ("flatbed", "28ft_double_coil")]),
         src=SUPPLY, dst=SITES, tags=["steel"]),
    dict(id="pipes", name="Pipes", icon="🛢️", needs="tractor", rate=2.5, weight=(14, 24), fragile=0.1,
         loads=["Steel pipes", "Concrete pipe", "Pipeline sections"],
         trailers=dict(eu=[("eu_trailers", "flatbed3a_pipes")],
                       us=[("flatbed", "48ft_pipes"), ("flatbed", "53ft_pipes"), ("flatbed", "32ft_pipes")]),
         src=SUPPLY, dst=SITES),
    dict(id="lumber", name="Lumber", icon="🪵", needs="tractor", rate=2.3, weight=(10, 22), fragile=0.1,
         loads=["Dimensional lumber", "Timber", "Plywood"],
         trailers=dict(eu=[("eu_trailers", "flatbed3a_wood")],
                       us=[("flatbed", "48ft_wood"), ("flatbed", "53ft_wood"), ("flatbed", "32ft_wood")]),
         src=MILLS, dst=SITES),
    dict(id="fuel", name="Fuel", icon="⛽", needs="tractor", rate=3.0, weight=(20, 28), fragile=0.0, hazard=True,
         loads=["Petrol", "Diesel", "Heating oil"],
         trailers=dict(eu=[], us=[("tanker", "petroleum_3"), ("tanker", "diesel_3"), ("tanker", "petroleum_short_3")]),
         src=["industrial*", "storage*", "group:fuel"], dst=FUEL),
    dict(id="milk", name="Milk", icon="🥛", needs="tractor", rate=2.4, weight=(18, 26), fragile=0.3,
         loads=["Raw milk", "Fresh milk"],
         trailers=dict(eu=[], us=[("tanker", "milk_3"), ("tanker", "milk_short_3")]),
         src=FARMS, dst=["dairy*", "food_processing*", "manufactur*", "group:food"]),
    dict(id="container", name="Container", icon="🚢", needs="tractor", rate=2.6, weight=(10, 26), fragile=0.1,
         loads=["Shipping container", "Two 20 ft containers", "Export container"],
         trailers=dict(eu=[("eu_trailers", "flatbed3a_container_40ft"), ("eu_trailers", "flatbed3a_container_20ftx2")],
                       us=[("containerTrailer", "40ft_3axle_40ft"), ("containerTrailer", "40ft_3axle_20ft"),
                           ("containerTrailer", "20ft_2axle_20ft")]),
         src=PORTS, dst=SUPPLY, port=True),
    dict(id="logs", name="Logs", icon="🌲", needs="tractor", rate=2.6, weight=(16, 28), fragile=0.0,
         loads=["Sawlogs", "Pulpwood", "Hardwood logs"],
         trailers=dict(eu=[("log_trailer", "40ft_12logs")], us=[("log_trailer", "40ft_12logs"), ("log_trailer", "27ft_12logs")]),
         src=MILLS, dst=MILLS),
    dict(id="gravel", name="Gravel", icon="🪨", needs="tractor", rate=2.2, weight=(18, 30), fragile=0.0,
         loads=["Crushed gravel", "Sand", "Aggregate"],
         trailers=dict(eu=[("frameless_dump", "double")], us=[("frameless_dump", "double"), ("frameless_dump", "triple")]),
         src=QUARRY, dst=SITES),
    # rigid-truck jobs: the load rides in the truck's own body
    dict(id="parcels", name="Parcels", icon="📬", needs="box", rate=3.2, weight=(1, 6), fragile=0.5, stops=(3, 5),
         loads=["Parcels", "Online orders", "Courier round", "Grocery delivery"], trailers={},
         src=SUPPLY, dst=SHOPS + ["group:lodging"], local=True),
    dict(id="gravel_rigid", name="Aggregate", icon="🚧", needs="dump", rate=2.8, weight=(8, 18), fragile=0.0,
         loads=["Crushed stone", "Topsoil", "Sand"], trailers={}, src=QUARRY, dst=SITES, local=True),
    dict(id="concrete", name="Concrete", icon="🧱", needs="mixer", rate=4.5, weight=(6, 9), fragile=0.6, rush=True,
         loads=["Ready-mix concrete"], trailers={}, src=["industrial*", "construction*", "building_supply*"], dst=SITES,
         local=True),
    dict(id="rigid_flatbed", name="Building materials", icon="🏗️", needs="flatbed", rate=3.0, weight=(4, 12), fragile=0.2,
         loads=["Bricks", "Scaffolding", "Roofing", "Timber", "Steel beams", "Precast panels", "Lumber packs", "Pipes", "Marble slabs"],
         trailers={}, src=SUPPLY, dst=SITES, local=True),
    dict(id="rigid_tanker", name="Liquid cargo", icon="💧", needs="tanker", rate=3.4, weight=(8, 18), fragile=0.2,
         loads=["Water", "Milk", "Heating oil"], trailers={}, src=FARMS, dst=SHOPS, local=True),
    # drawbar combinations: a rigid truck plus a drawbar trailer (the European "Hängerzug")
    dict(id="drawbar_box", name="Drawbar box train", icon="🚛", needs="drawbar", rate=3.4, weight=(10, 22), fragile=0.25,
         loads=["Pallet freight", "Soft drinks", "Retail stock"],
         trailers=dict(eu=[("eu_trailers", "boxdrawbar2aspread"), ("eu_trailers", "boxdrawbar2aspread_cola"),
                           ("eu_trailers", "boxdrawbar2aspread_muhten")], us=[]),
         src=SUPPLY, dst=SHOPS),
    dict(id="drawbar_flat", name="Drawbar flatbed", icon="🚛", needs="drawbar", rate=3.3, weight=(8, 20), fragile=0.1,
         loads=["Pipes", "Timber", "Marble blocks"],
         trailers=dict(eu=[("eu_trailers", "flatbeddrawbar2a"), ("eu_trailers", "flatbeddrawbar2a_pipes"),
                           ("eu_trailers", "flatbeddrawbar2a_wood"), ("eu_trailers", "flatbeddrawbar3a_marble")], us=[]),
         src=SUPPLY, dst=SITES),
    dict(id="drawbar_tanker", name="Drawbar tanker train", icon="⛽", needs="drawbar", rate=3.6, weight=(16, 26), fragile=0.1,
         hazard=True, loads=["Diesel", "Petrol", "Heating oil"],
         trailers=dict(eu=[("eu_trailers", "tanker3a"), ("eu_trailers", "tanker2a"), ("eu_trailers", "tanker3a_apex"),
                           ("eu_trailers", "tanker2a_apex")], us=[]),
         src=["industrial*", "storage*", "group:fuel"], dst=FUEL),
    dict(id="drawbar_milk", name="Drawbar milk train", icon="🥛", needs="drawbar", rate=3.3, weight=(16, 26), fragile=0.3,
         loads=["Raw milk", "Fresh milk"],
         trailers=dict(eu=[("eu_trailers", "tankermilk3a"), ("eu_trailers", "tankermilk2a"),
                           ("eu_trailers", "tankermilk3a_muhten"), ("eu_trailers", "tankermilk2a_muhten")], us=[]),
         src=FARMS, dst=["dairy*", "food_processing*", "manufactur*", "group:food"]),
    # small trucks and pickups with a utility trailer
    dict(id="light", name="Furniture & goods", icon="🛋️", needs="light", rate=3.6, weight=(0.3, 1.5), fragile=0.5,
         loads=["Sofa delivery", "Armchairs", "Planks", "Home move"],
         trailers=dict(us=[("cargotrailer", "loaded_couch"), ("cargotrailer", "loaded_armchairs"), ("cargotrailer", "loaded_planks"),
                           ("boxutility_large", "loaded_400"), ("boxutility_large", "loaded_couch")],
                       eu=[("cargotrailer", "loaded_couch"), ("boxutility_large", "loaded_400")]),
         src=SUPPLY, dst=SHOPS, local=True),
]
CARGO_BY_ID = {c["id"]: c for c in CARGO}

# ---- themed scenarios: a story, a shape, and the cargo it uses. "from"/"to" are hub names (None: around the market
# town). stops: number of drops. The market fills in the rest. ----
SCENARIOS = [
    dict(id="express", name="Ottawa → Toronto Express", icon="🏁", cargo="dryvan", frm="Ottawa", to="Toronto", pay=1.25,
         blurb="The classic 450 km: a dry van of retail stock down the 416 and the 401, delivered on a deadline."),
    dict(id="corridor_run", name="Corridor run", icon="🛣️", cargo="dryvan", frm="Ottawa", to="Toronto", via=4, pay=1.3,
         blurb="One load, several drops: a dry van delivered town by town along the 416 and 401, "
               "Brockville, Kingston, Belleville and on, in driving order."),
    dict(id="express_flatbed", name="Ottawa → Toronto Flatbed", icon="🏗️", cargo="rigid_flatbed", frm="Ottawa", to="Toronto", pay=1.25,
         blurb="Steel, lumber and building supplies on a flatbed truck, the full 450 km on a deadline."),
    dict(id="express_drawbar", name="Ottawa → Toronto Drawbar Flatbed", icon="🚛", cargo="drawbar_flat", frm="Ottawa", to="Toronto", pay=1.3,
         blurb="A rigid truck with a drawbar flatbed trailer (the eye hitch), pipes or timber from Ottawa to Toronto."),
    dict(id="express_steel", name="Ottawa → Toronto Steel", icon="🔩", cargo="steel", frm="Ottawa", to="Toronto", pay=1.3,
         blurb="Heavy coils on a semi flatbed trailer, down the 416 and the 401."),
    dict(id="corridor_flatbed", name="Corridor flatbed run", icon="🛣️", cargo="rigid_flatbed", frm="Ottawa", to="Toronto", via=4, pay=1.35,
         blurb="Building materials dropped in Brockville, Kingston, Belleville and on, in driving order, on a flatbed truck."),
    dict(id="express_box", name="Ottawa → Toronto Box Express", icon="📬", cargo="parcels", frm="Ottawa", to="Toronto", pay=1.25,
         blurb="Parcels and pallets in a box truck, Ottawa to Toronto."),
    dict(id="fuel_run", name="Fuel run", icon="⛽", cargo="fuel", frm=None, to=None, stops=(2, 3), pay=1.4,
         blurb="Load at the depot and fill two or three gas stations. Dangerous goods: take the corners gently."),
    dict(id="last_mile", name="Last-mile parcels", icon="📬", cargo="parcels", frm=None, to=None, stops=(4, 5), pay=1.0,
         blurb="Four or five drops across town in a box truck. Fragile: no braking at the last second."),
    dict(id="concrete_rush", name="Concrete rush", icon="🧱", cargo="concrete", frm=None, to=None, pay=1.3,
         blurb="The mix is already turning: get it to the site before it sets, and don't slop it round the corners."),
    dict(id="log_haul", name="Log haul", icon="🌲", cargo="logs", frm=None, to=None, minkm=40, maxkm=140, pay=1.15,
         blurb="A full load of sawlogs from the forest to the mill. Heavy and high."),
    dict(id="port_run", name="Port run", icon="🚢", cargo="container", frm=None, to=None, minkm=20, maxkm=250, pay=1.15,
         blurb="Pick a container up at the port and take it to the warehouse inland.", needs_tag="port"),
    dict(id="milk_run", name="Milk run", icon="🥛", cargo="milk", frm=None, to=None, minkm=30, maxkm=160, pay=1.2,
         blurb="Collect from the dairy farms and deliver to the processing plant. Smooth driving keeps it fresh."),
    dict(id="steel_mill", name="Steel from the mill", icon="🔩", cargo="steel", frm="Hamilton", to=None, minkm=60, maxkm=500,
         pay=1.2, blurb="Heavy coils from Hamilton's steel mills to a customer along the corridor."),
    dict(id="euro_train", name="European combination", icon="🚛", cargo="drawbar_box", frm=None, to=None, minkm=30, maxkm=300,
         pay=1.2, blurb="A rigid truck plus a drawbar trailer: the European way. Needs a truck with a drawbar hitch, "
                        "such as a rigid Percheron."),
]

# ---- truck classification ----
# Configs that are not freight trucks at all
NOT_FREIGHT = re.compile(r"police|ambul|fire|derby|drag|race|rally|drift|bus|armored|military|swat|ramplow|destroyer|"
                         r"wrecker|towtruck|rollback|rockbouncer|parkranger|taxi|hearse|limo|crawler|jato|stripped", re.I)
BUS_MODELS = {"citybus", "citybus2", "citybus3", "citybus5", "citybus6", "citybus7", "ecitybus", "zmkcitybus"}


# What a config's own parts say it is. Custom configs have names that say nothing ("Home", "ruck"), but the parts they
# are built from do: a fifth wheel plate, a trailer hitch, a flatbed deck, a tanker body, a bus body.
NOT_FREIGHT_PARTS = re.compile(r"schoolbus|citybus|ambulance|police|firetruck", re.I)


def classify_parts(parts):
    """Rig classes from the key part names the mod reports for a config (empty set: nothing recognisable)."""
    caps = set()
    for p in parts or []:
        q = p.lower()
        if "interior" in q or "licenseplate" in q or "lights" in q or "clearance" in q or "strap" in q:
            continue                                            # a switch, a plate or a lamp, not the part itself
        if "fifthwheel" in q:
            caps.add("tractor")
        if "trailerhitch" in q or "pintle" in q:
            caps.add("drawbar")
        if "towhitch" in q or "ballhitch" in q or "gooseneck" in q:
            caps.add("light")
        if "flatbed" in q:
            caps.add("flatbed")
        if "cargobox" in q or "upfit_box" in q:
            caps.add("box")
        if "tanker" in q:
            caps.add("tanker")
        if "dump" in q:
            caps.add("dump")
        if "mixer" in q:
            caps.add("mixer")
        if "logging" in q:
            caps.add("tractor")
    return caps


def is_bus_like(parts):
    return any(NOT_FREIGHT_PARTS.search(p or "") for p in parts or [])


def _by_name(model, key, name="", model_type="Truck", parts=None):
    """What this truck config can do, as a set of rig classes (see the module docstring). Empty: not a freight truck.
    Unknown mods fall back to their name; the console lets the player override the result."""
    k = f"{key} {name}".lower()
    m = (model or "").lower()
    caps = set()
    if m in BUS_MODELS or "bus" in m or is_bus_like(parts):
        return caps
    if NOT_FREIGHT.search(k) and not re.search(r"fifth|fift|cargobox|box|flatbed|tanker|mixer|dump", k):
        return caps
    if m == "us_semi":
        if re.match(r"^tc?8\d(_|$)", key):                      # t82_base, tc83_longhaul...: no vocational "s"
            caps.add("tractor")
        elif re.search(r"s_cargobox", key):
            caps.add("box")
        elif re.search(r"s_flatbed", key):
            caps.add("flatbed")
        elif re.search(r"s_tanker", key):
            caps.add("tanker")
        elif re.search(r"s_dump", key):
            caps.add("dump")
        elif re.search(r"s_mixer", key):
            caps.add("mixer")
        elif re.search(r"logging", key):
            caps.add("tractor")                              # logging trucks pull the log trailer
    elif m == "percheron":
        if re.search(r"fift?hwheel|fiftwheel", k):
            caps.add("tractor")
        elif "box" in k:
            caps |= {"box", "drawbar"}
        elif "flatbed" in k:
            caps |= {"flatbed", "drawbar"}
        elif "tanker" in k:
            caps |= {"tanker", "drawbar"}          # every rigid Percheron has the pintle (eye hitch) at the back
        elif "mixer" in k:
            caps.add("mixer")
    elif m == "md_series":
        if "fifthwheel" in k:
            caps.add("tractor")
        elif "cargobox" in k:
            caps.add("box")
        elif "flatbed" in k:
            caps.add("flatbed")
        elif "dump" in k:
            caps.add("dump")
        else:
            caps.add("light")
    elif m in ("dumptruck", "frameless_dump"):
        caps.add("dump")
    elif m == "van":
        caps.add("box" if "delivery" in k else "light")
    elif m in ("pickup", "roamer", "midtruck", "hopper"):
        caps.add("light")
    else:                                                       # a mod truck: guess from its name
        if re.search(r"fift?hwheel|tractor|semi|sleeper|longhaul|t-series", k):
            caps.add("tractor")
        if re.search(r"cargobox|box|van|delivery", k):
            caps.add("box")
        if "flatbed" in k:
            caps.add("flatbed")
        if "tanker" in k:
            caps.add("tanker")
        if re.search(r"dump|tipper", k):
            caps.add("dump")
        if "mixer" in k:
            caps.add("mixer")
        if not caps and model_type in ("Truck", "Heavy Machinery"):
            caps.add("light")
    return caps


def classify(model, key, name="", model_type="Truck", parts=None):
    """What this truck config can do, as a set of rig classes (see the module docstring). The name and the parts are
    both used: the parts decide for custom configs, the name fills in what a stock config's parts don't spell out.
    Empty: not a freight truck, or a custom config nothing is known about (the console lets the player set it)."""
    caps = _by_name(model, key, name, model_type, parts)
    if is_bus_like(parts) or (model or "").lower() in BUS_MODELS:
        return set()
    return caps | classify_parts(parts)


def family(model):
    """Which trailer family a truck prefers: the European FPU set for the Percheron, the Randolph set otherwise."""
    return "eu" if (model or "").lower() == "percheron" else "us"


def trailer_candidates(cargo, fam):
    """Trailers for a cargo, the truck's own family first, then the other (the hitches are the same)."""
    t = cargo.get("trailers") or {}
    other = "us" if fam == "eu" else "eu"
    seen, out = set(), []
    for f in (fam, other):
        for model, config in t.get(f, []):
            if (model, config) not in seen:
                seen.add((model, config))
                out.append({"model": model, "config": config, "family": f})
    return out


def can_carry(caps, cargo):
    """Whether a truck with these capabilities can carry the cargo."""
    need = cargo["needs"]
    if need == "light":
        return "light" in caps or "box" in caps
    return need in caps
