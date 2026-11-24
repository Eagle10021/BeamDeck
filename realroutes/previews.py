"""Pictures of the trucks for the Garage.

BeamNG keeps a preview picture for every vehicle config: stock ones inside the vehicle's archive in the game's content
folder (content/vehicles/<model>.zip, vehicles/<model>/<config>.jpg), a player's saved ones next to the saved config in the
user folder (vehicles/<model>/<name>.jpg), and mods' ones in their own folders. A saved config without a thumbnail shows its
model's default picture. Only pictures are ever read, and only from those folders: model and config names that could point
anywhere else are refused.
"""
import glob, os, re, threading, time, zipfile

EXTS = (".jpg", ".jpeg", ".png")
MIME = {".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".png": "image/png"}
NAME_OK = re.compile(r"^[\w .()+\-]{1,80}$")
MISS_S = 60                  # a picture that was not found is looked for again after this long (the game may have saved it)
CACHE_MAX = 300


def _vdf_libraries(steam_root):
    """Steam's extra library folders, from its libraryfolders.vdf."""
    out = []
    try:
        for line in open(os.path.join(steam_root, "steamapps", "libraryfolders.vdf"), errors="ignore"):
            m = re.search(r'"path"\s+"([^"]+)"', line)
            if m:
                out.append(m.group(1).replace("\\\\", "\\"))
    except OSError:
        pass
    return out


class Previews:
    def __init__(self):
        self.lock = threading.Lock()
        self.hello_game = self.hello_user = ""
        self.cache = {}              # (model, config) -> (when, (bytes, mime) or None)
        self.zips = {}               # archive path -> (ZipFile, {lower-case name: real name}) or None

    def set_paths(self, game, user):
        """The game's own folders, as the mod reports them (the most reliable source)."""
        game, user = str(game or "")[:300], str(user or "")[:300]
        with self.lock:
            if (game, user) != (self.hello_game, self.hello_user):
                self.hello_game, self.hello_user = game, user
                self.cache.clear()

    # ------------------------------------------------------------------ where the game keeps things
    def game_dirs(self):
        home = os.path.expanduser("~")
        roots = [os.path.join(home, ".local", "share", "Steam"), os.path.join(home, ".steam", "steam"),
                 r"C:\Program Files (x86)\Steam", r"C:\Program Files\Steam"]
        libs = list(roots)
        for r in roots:
            libs += _vdf_libraries(r)
        cands = [self.hello_game, os.environ.get("BEAMNG_GAME_DIR", "")] + [os.path.join(x, "steamapps", "common", "BeamNG.drive") for x in libs]
        return [c for c in dict.fromkeys(cands) if c and os.path.isdir(os.path.join(c, "content"))]

    def user_dirs(self):
        home = os.path.expanduser("~")
        local = os.environ.get("LOCALAPPDATA", "")
        cands = [self.hello_user, os.environ.get("BEAMNG_USER_DIR", ""),
                 os.path.join(home, ".local", "share", "BeamNG", "BeamNG.drive", "current"),
                 os.path.join(local, "BeamNG", "BeamNG.drive", "current") if local else ""]
        return [c for c in dict.fromkeys(cands) if c and os.path.isdir(c)]

    # ------------------------------------------------------------------ finding a picture
    def _file(self, base):
        for ext in EXTS:
            p = base + ext
            if os.path.isfile(p):
                try:
                    return open(p, "rb").read(), MIME[ext]
                except OSError:
                    pass
        return None

    def _zip(self, path):
        if path not in self.zips:
            try:
                z = zipfile.ZipFile(path)
                self.zips[path] = (z, {n.lower(): n for n in z.namelist()})
            except (OSError, zipfile.BadZipFile):
                self.zips[path] = None
        return self.zips[path]

    def _from_zip(self, path, inner):
        zz = self._zip(path)
        if not zz:
            return None
        z, names = zz
        for ext in EXTS:
            real = names.get((inner + ext).lower())
            if real:
                try:
                    return z.read(real), MIME[ext]
                except (OSError, zipfile.BadZipFile, KeyError):
                    return None
        return None

    def _look(self, model, config):
        users, games = self.user_dirs(), self.game_dirs()
        for u in users:                                                  # the player's saved configs, then mods
            hit = self._file(os.path.join(u, "vehicles", model, config))
            if hit:
                return hit
            for d in glob.glob(os.path.join(glob.escape(u), "mods", "unpacked", "*", "vehicles", glob.escape(model))):
                hit = self._file(os.path.join(d, config))
                if hit:
                    return hit
        for g in games:                                                  # the game's own
            hit = self._from_zip(os.path.join(g, "content", "vehicles", model + ".zip"), f"vehicles/{model}/{config}") \
                or self._file(os.path.join(g, "content", "vehicles", model, config))
            if hit:
                return hit
        for g in games:                                                  # no picture of its own: the model's default
            hit = self._from_zip(os.path.join(g, "content", "vehicles", model + ".zip"), f"vehicles/{model}/default") \
                or self._file(os.path.join(g, "content", "vehicles", model, "default"))
            if hit:
                return hit
        return None

    def find(self, model, config):
        if not (NAME_OK.match(model or "") and NAME_OK.match(config or "")) or ".." in model or ".." in config:
            return None
        key, now = (model, config), time.time()
        with self.lock:
            hit = self.cache.get(key)
            if hit and (hit[1] is not None or now - hit[0] < MISS_S):
                return hit[1]
        res = self._look(model, config)
        with self.lock:
            if len(self.cache) >= CACHE_MAX:
                self.cache.pop(next(iter(self.cache)))
            self.cache[key] = (now, res)
        return res

    def respond(self, model, config):
        """(status, bytes, content type) for the helper's HTTP side."""
        res = self.find(model, config)
        if res:
            return 200, res[0], res[1]
        return 404, b'{"error": "no picture"}', "application/json"
