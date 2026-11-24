"""The truck pictures: python3 tools/test_previews.py"""
import os, sys, tempfile, zipfile
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
import previews as P

bad = []
def ok(c, m):
    print(("PASS " if c else "FAIL ") + m)
    if not c: bad.append(m)

for k in ("BEAMNG_GAME_DIR", "BEAMNG_USER_DIR"):
    os.environ.pop(k, None)
root = tempfile.mkdtemp()
game, user = os.path.join(root, "game"), os.path.join(root, "user")
os.makedirs(os.path.join(game, "content", "vehicles")); os.makedirs(os.path.join(user, "vehicles", "truck"))
with zipfile.ZipFile(os.path.join(game, "content", "vehicles", "truck.zip"), "w") as z:
    z.writestr("vehicles/truck/stock_cfg.jpg", b"STOCK"); z.writestr("vehicles/truck/default.jpg", b"DEFAULT")
    z.writestr("vehicles/truck/Mixed_Case.PNG".lower(), b"MIXED")
open(os.path.join(user, "vehicles", "truck", "my cfg.jpg"), "wb").write(b"MINE")
open(os.path.join(user, "vehicles", "truck", "stock_cfg.png"), "wb").write(b"OVERRIDE")
os.makedirs(os.path.join(user, "mods", "unpacked", "somemod", "vehicles", "modtruck")); open(os.path.join(user, "mods", "unpacked", "somemod", "vehicles", "modtruck", "m1.jpg"), "wb").write(b"MOD")

p = P.Previews(); p.set_paths(game, user)
ok(p.find("truck", "my cfg") == (b"MINE", "image/jpeg"), "a saved config's own thumbnail (a name with a space) is found in the user folder")
ok(p.find("truck", "stock_cfg") == (b"OVERRIDE", "image/png"), "the user's picture wins over the game's")
os.remove(os.path.join(user, "vehicles", "truck", "stock_cfg.png")); p.set_paths(game, user + "/")
ok(p.find("truck", "stock_cfg") == (b"STOCK", "image/jpeg"), "a stock config's picture comes from the game's archive")
ok(p.find("truck", "mixed_case") == (b"MIXED", "image/png"), "names match whatever their case")
ok(p.find("truck", "unknown_cfg") == (b"DEFAULT", "image/jpeg"), "a config with no picture of its own shows the model's default")
ok(p.find("modtruck", "m1") == (b"MOD", "image/jpeg"), "a mod's own picture is found")
ok(p.find("nomodel", "x") is None, "an unknown vehicle has no picture")
for bad_name in ("../x", "a/b", "..", "a\\b", "x" * 200, "", "con\x00"):
    ok(p.find("truck", bad_name) is None and p.find(bad_name, "stock_cfg") is None, f"the name {bad_name[:12]!r} is refused")
ok(p.respond("truck", "stock_cfg")[0] == 200 and p.respond("truck", "stock_cfg")[2] == "image/jpeg", "the route answers with the picture and its type")
code, body, ctype = p.respond("nomodel", "x")
ok(code == 404 and ctype == "application/json", "and with a plain 404 when there is none")
ok(p.find("truck", "late") == (b"DEFAULT", "image/jpeg"), "a picture not there yet falls back to the default")
open(os.path.join(user, "vehicles", "truck", "late.jpg"), "wb").write(b"LATE")
ok(p.find("truck", "late") == (b"DEFAULT", "image/jpeg"), "which is remembered for a moment")
p.cache.clear()
ok(p.find("truck", "late") == (b"LATE", "image/jpeg"), "and the new picture appears once it is looked for again")
print("\nall preview tests passed" if not bad else f"\n{len(bad)} FAILED")
sys.exit(1 if bad else 0)
