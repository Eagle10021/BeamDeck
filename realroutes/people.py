"""Riders for Rides: a built-in set of generic people, plus the player's own (added from the console).

Generic people have no picture files: their round picture and their roadside card are a plain pictogram (a head and
shoulders in the person's colour), drawn here pixel by pixel and written as PNG with the standard library only.
Their picture key is "_" + the name without the full stop ("_Sam R"); keys starting with "_" are never listed as
the player's own people. The player's own people are <avatars>/<Name>.jpg (square picture) and
<avatars>/standees/<Name>.png (roadside card), made in the console from an uploaded picture.
"""
import colorsys, os, re, struct, zlib

FIRST = ["Sam", "Priya", "Jordan", "Mei", "Alex", "Fatima", "Noah", "Chloe", "Liam", "Aisha", "Mateo", "Emma",
         "Omar", "Sophie", "Ethan", "Zoe", "Raj", "Hana", "Lucas", "Maya", "Ben", "Amelie", "Kai", "Nadia",
         "Diego", "Yuki", "Grace", "Tariq", "Elena", "Jonas", "Leila", "Marcus", "Ingrid", "Kofi", "Sara", "Andre",
         "Mina", "Oscar", "Tess", "Hugo", "Anika", "Felix", "Rosa", "Dmitri", "Lena", "Samir", "Clara", "Tomas",
         "Nia", "Viktor", "Isla", "Arjun", "Freya", "Malik", "Elise", "Pablo", "Suki", "Henrik", "Amara", "Luca",
         "Daria", "Kenji", "Olive", "Idris", "Petra", "Theo", "Zainab", "Milo", "Carmen", "Sven", "Layla", "Ravi",
         "Agnes", "Emeka", "Bianca", "Jun", "Marta", "Elias", "Thandi", "Rafael", "Kira", "Anton", "Selin", "Dev",
         "Nora", "Bruno", "Asha", "Stefan", "Lucia", "Hamid", "Ivy", "Mikael", "Farah", "Caleb", "Ines", "Tae",
         "Greta", "Yusuf", "Paloma", "Owen"]
LAST = "RKMTSBLDHCWPNGFAJEVO"


def generic():
    """The built-in riders: [{"name": "Sam R.", "avatar": "_Sam R", "credit": None}, ...] (100 of them)."""
    out = []
    for i, f in enumerate(FIRST):
        ini = LAST[(i * 7 + 3) % len(LAST)]
        out.append({"name": f"{f} {ini}.", "avatar": f"_{f} {ini}", "credit": None})
    return out


def is_generic(key):
    return key.startswith("_") and any(p["avatar"] == key for p in generic())


def clean_name(name):
    """A person's name as a safe file name: letters, digits, spaces and a few marks; never starts with "_" or "."."""
    n = re.sub(r"[^\w .,'()-]", "", str(name or ""), flags=re.UNICODE).strip().lstrip("_.").strip()[:40]
    return n


def _colours(key):
    """Background and figure colours for a person, steady for the same name."""
    h = (zlib.crc32(key.encode()) % 360) / 360
    bg = colorsys.hls_to_rgb(h, 0.80, 0.55)
    fg = colorsys.hls_to_rgb(h, 0.42, 0.50)
    to = lambda c: bytes(round(x * 255) for x in c)
    return to(bg), to(fg)


def _png(w, h, rows):
    raw = b"".join(b"\x00" + bytes(r) for r in rows)
    chunk = lambda t, d: struct.pack(">I", len(d)) + t + d + struct.pack(">I", zlib.crc32(t + d) & 0xFFFFFFFF)
    return b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", w, h, 8, 6, 0, 0, 0)) + \
        chunk(b"IDAT", zlib.compress(raw, 6)) + chunk(b"IEND", b"")


def _figure(w, h, key, border, radius, head, body_top):
    """Rows of RGBA for a pictogram: a tinted panel, a head (circle) and shoulders (the top of an ellipse)."""
    bg, fg = _colours(key)
    white, clear = b"\xff\xff\xff\xff", b"\x00\x00\x00\x00"
    cx = w / 2
    hx, hy, hr = cx, head[0], head[1]
    rx, ry, by = w * 0.45, h - body_top, h + (h - body_top) * 0.08     # shoulders: an ellipse centred below the card
    rows = []
    for y in range(h):
        row = bytearray((bg + b"\xff") * w)

        def span(x0, x1, px):
            x0, x1 = max(0, int(round(x0))), min(w, int(round(x1)))
            if x1 > x0:
                row[x0 * 4:x1 * 4] = px * (x1 - x0)

        dy = y + 0.5 - hy
        if abs(dy) < hr:
            half = (hr * hr - dy * dy) ** 0.5
            span(hx - half, hx + half, fg + b"\xff")
        dy = (y + 0.5 - by) / ry
        if y >= body_top and abs(dy) < 1:
            half = rx * (1 - dy * dy) ** 0.5
            span(cx - half, cx + half, fg + b"\xff")
        if border:
            if y < border or y >= h - border:
                span(0, w, white)
            else:
                span(0, border, white); span(w - border, w, white)
        if radius:                                  # rounded corners: clear outside the corner circles
            edge = radius - y - 0.5 if y < radius else (y + 0.5 - (h - radius) if y >= h - radius else -1)
            if edge > 0:
                cut = radius - (radius * radius - edge * edge) ** 0.5
                span(0, cut, clear); span(w - cut, w, clear)
        rows.append(row)
    return rows


_avatar_cache = {}


def avatar_png(key):
    """The round picture of a generic person (a 128 px square; the pages crop it to a circle)."""
    if key not in _avatar_cache:
        _avatar_cache[key] = _png(128, 128, _figure(128, 128, key, 0, 0, (50, 26), 84))
    return _avatar_cache[key]


def ensure_cards(folder):
    """The roadside cards of the generic people (<folder>/_<name>.png), written once."""
    os.makedirs(folder, exist_ok=True)
    made = 0
    for p in generic():
        f = os.path.join(folder, p["avatar"] + ".png")
        if not os.path.exists(f):
            w, h = 300, 550
            with open(f, "wb") as o:
                o.write(_png(w, h, _figure(w, h, p["avatar"], 7, 23, (170, 62), 262)))
            made += 1
    return made
