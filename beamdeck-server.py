#!/usr/bin/env python3
"""BeamDeck helper: plays any video for BeamNG.

- Serves the BeamDeck control window at http://127.0.0.1:47130/ (open it as a browser app window).
- Streams YouTube etc. through yt-dlp (VP9/Opus DASH), or downloads + converts when a site can't stream.
- Talks to the game over UDP (game listens on 47131, we listen on 47132): the game sends camera/car
  info for the car-speaker audio effect; we send the car-screen state (on/off, file, position).
- Car screens can't load anything from the network (BeamNG's UI filter), so a small video-only copy
  is written into the mod folder and the game loads it from its own file system.
"""
import base64, hashlib, json, os, re, socket, struct, subprocess, threading, time
import urllib.error, urllib.parse, urllib.request
from http.server import ThreadingHTTPServer, BaseHTTPRequestHandler

HOST, PORT = "127.0.0.1", int(os.environ.get("BEAMDECK_PORT", "47130"))
GAME_PORT = int(os.environ.get("BEAMDECK_GAME_PORT", "47131"))
MY_UDP = int(os.environ.get("BEAMDECK_UDP", "47132"))
HERE = os.path.dirname(os.path.realpath(__file__))
PAGE = os.path.join(HERE, "beamdeck-ui.html")
WINDOWS = os.name == "nt"
if WINDOWS:
    # under pythonw every console tool (ffmpeg, ffprobe, yt-dlp, svcl) would open a console window of its own;
    # and text output would be decoded as the ANSI code page (cp1252), garbling every non-ASCII title
    class _NoConsolePopen(subprocess.Popen):
        def __init__(self, *a, **k):
            k["creationflags"] = k.get("creationflags", 0) | 0x08000000   # CREATE_NO_WINDOW
            if k.get("text") or k.get("universal_newlines"):
                k.setdefault("encoding", "utf-8")
                k.setdefault("errors", "replace")
            super().__init__(*a, **k)
    subprocess.Popen = _NoConsolePopen
    os.environ.setdefault("PYTHONIOENCODING", "utf-8")    # yt-dlp (a Python program) prints UTF-8 to these pipes


def _xdg(var, fallback):
    return os.environ.get(var) or os.path.expanduser(fallback)


# Per-user locations (nothing personal is hard-coded; everything else lives in config.json)
if WINDOWS:
    CONFIG_DIR = os.path.join(os.environ.get("APPDATA", os.path.expanduser("~")), "BeamDeck")
    CACHE = os.path.join(os.environ.get("LOCALAPPDATA", os.path.expanduser("~")), "BeamDeck", "cache")
    DATA_DIR = os.path.join(os.environ.get("LOCALAPPDATA", os.path.expanduser("~")), "BeamDeck")
else:
    CONFIG_DIR = os.path.join(_xdg("XDG_CONFIG_HOME", "~/.config"), "beamdeck")
    CACHE = os.path.join(_xdg("XDG_CACHE_HOME", "~/.cache"), "beamdeck")
    DATA_DIR = os.path.join(_xdg("XDG_DATA_HOME", "~/.local/share"), "beamdeck")
CONFIG_FILE = os.path.join(CONFIG_DIR, "config.json")


def find_beamng_userfolder():
    """BeamNG's user folder: native Linux, Proton, or Windows; the newest version folder ('current' if present)."""
    home = os.path.expanduser("~")
    bases = [os.path.join(_xdg("XDG_DATA_HOME", "~/.local/share"), "BeamNG", "BeamNG.drive"),
             os.path.join(home, ".local", "share", "BeamNG.drive")]
    if WINDOWS:
        bases.insert(0, os.path.join(os.environ.get("LOCALAPPDATA", ""), "BeamNG", "BeamNG.drive"))
        bases.insert(1, os.path.join(os.environ.get("LOCALAPPDATA", ""), "BeamNG.drive"))
        # a user folder moved in BeamNG's launcher is only recorded in BeamNG.drive.ini ("userFolder = D:\...")
        try:
            for line in open(os.path.join(os.environ.get("LOCALAPPDATA", ""), "BeamNG", "BeamNG.drive.ini"),
                             encoding="utf-8-sig", errors="replace"):
                k, _, v = line.partition("=")
                if k.strip().lower() == "userfolder" and v.strip():
                    bases.insert(0, v.strip().strip('"'))
        except OSError:
            pass
    for steam in ("~/.local/share/Steam", "~/.steam/steam"):
        bases.append(os.path.expanduser(steam + "/steamapps/compatdata/284160/pfx/drive_c/users/steamuser/"
                                        "AppData/Local/BeamNG/BeamNG.drive"))
    for b in bases:
        if not os.path.isdir(b):
            continue
        if os.path.isdir(os.path.join(b, "current")):
            return os.path.join(b, "current")
        versions = sorted((d for d in os.listdir(b) if d[:1].isdigit() and os.path.isdir(os.path.join(b, d))),
                          key=lambda d: [int(x) if x.isdigit() else 0 for x in d.split(".")])
        if versions:
            return os.path.join(b, versions[-1])
        return b
    return ""


CONFIG_DEFAULT = {
    "beamng_userfolder": "",            # empty = detect
    "music_folders": [],                # local music library
    "navidrome": {"url": "", "user": "", "password": ""},
    "browser": "",                      # empty = first Chromium-based browser found (for the launcher)
    "aa_transport": "usb",              # "usb" (like a real car, no developer mode) or "adb" (head unit server)
    "mp_relay": "",                     # BeamMP shared music: the relay's address, e.g. ws://server:47140
    "mp_share": True,                   # send the car's music to the other players
    "mp_share_screen": True,            # show the car screen on this player's car in the other players' games
    # Windows only: app sound and Android Auto's sound go through a virtual cable (VB-CABLE); NirSoft's
    # SoundVolumeCommandLine (svcl.exe) points the chosen app at it
    "windows": {"svcl": "svcl.exe", "cable_out": "CABLE Input", "cable_in": "CABLE Output (VB-Audio Virtual Cable)"},
}


def normalize_relay(v):
    """Accepts '100.x.y.z:47140', 'http://…', 'ws://…/' … and returns 'ws://host:port'."""
    v = (v or "").strip().strip('"').rstrip("/")
    if not v:
        return ""
    for pre in ("https://", "http://", "wss://", "ws://"):
        if v.lower().startswith(pre):
            v = v[len(pre):]
            break
    v = v.split("/")[0]                       # (drop any /players or path pasted along)
    if ":" not in v:
        v += ":47140"
    return "ws://" + v


def load_config():
    try:
        c = json.load(open(CONFIG_FILE))
    except Exception:
        c = {}
    cfg = json.loads(json.dumps(CONFIG_DEFAULT))
    cfg.update({k: v for k, v in c.items() if k in cfg})
    if isinstance(c.get("windows"), dict):
        cfg["windows"] = dict(CONFIG_DEFAULT["windows"], **c["windows"])
    if not cfg["music_folders"]:
        music = os.path.normpath(os.path.expanduser("~/Music"))
        cfg["music_folders"] = [music] if os.path.isdir(music) else []
    cfg["mp_relay"] = normalize_relay(cfg.get("mp_relay", ""))   # (older saves may lack ws:// etc.)
    return cfg


def save_config():
    os.makedirs(CONFIG_DIR, exist_ok=True)
    json.dump(config, open(CONFIG_FILE, "w"), indent=2)
    if not WINDOWS:
        os.chmod(CONFIG_FILE, 0o600)    # it can hold the Navidrome password


config = load_config()
USERFOLDER = config["beamng_userfolder"] or find_beamng_userfolder()
MOD = os.path.join(USERFOLDER, "mods", "unpacked", "beamdeck")
# Car-screen video copies go in BeamNG's user folder itself (= /beamdeck_media/ inside the game), not in the mod
# folder: BeamMP switches local mods off in multiplayer (only the server's copy of the mod runs), which would hide
# them. The user folder is always mounted.
MEDIA = os.path.join(USERFOLDER, "beamdeck_media")
META = os.path.join(CACHE, ".meta")                 # titles / links (hidden)
WORK = os.path.join(CACHE, ".work")                 # partial downloads (hidden)
for d in (CACHE, META, WORK, DATA_DIR) + ((MEDIA,) if USERFOLDER else ()):
    os.makedirs(d, exist_ok=True)
PLAYABLE_V = ("vp8", "vp9", "av1")
PLAYABLE_A = ("opus", "vorbis")
CHUNK = 8 << 20
HTTP_RE = "[protocol~='^https?$']"
STREAM_FMT = "/".join([
    f"bv*[vcodec^=vp][height<=1080]{HTTP_RE}+ba[acodec=opus]{HTTP_RE}",
    f"bv*[vcodec^=av01][height<=1080]{HTTP_RE}+ba[acodec=opus]{HTTP_RE}",
    f"bv*[vcodec^=vp][height<=1080]{HTTP_RE}+ba[acodec=vorbis]{HTTP_RE}",
    f"b[vcodec^=vp][acodec~='^(opus|vorbis)$']{HTTP_RE}",
])
SCREEN_FMT = "bv*[vcodec^=vp][height<=480]/bv*[vcodec^=av01][height<=480]/bv*[height<=480]/bv*/b"

lock = threading.Lock()
jobs = {}        # download jobs: id -> {state, progress, title, error}
screen_jobs = {} # key -> "working" | "ready" | "error"
streams = {}     # sid -> {title, src, at, v, a}
clients = set()  # websocket clients (control windows)
game = {"at": 0}  # last info from the game
player = {"key": None, "title": "", "pos": 0.0, "paused": True, "at": 0.0, "screens": False}


def ms():
    return int(time.time() * 1000)


def key_of(src):
    return hashlib.sha1(src.encode()).hexdigest()[:12]


def meta_path(i):
    return os.path.join(META, i + ".json")


def safe_name(title):
    t = re.sub(r'[\\/:*?"<>|\x00-\x1f]', "_", title or "").strip(" .")
    return t[:120] or "video"


def out_path(i):
    """Saved video for key i: '<title> [<key>].webm' in the cache folder."""
    tag = f"[{i}].webm"
    for f in os.listdir(CACHE):
        if f.endswith(tag):
            return os.path.join(CACHE, f)
    return os.path.join(CACHE, tag)


def migrate_old_files():
    for f in os.listdir(CACHE):
        full = os.path.join(CACHE, f)
        if re.fullmatch(r"[0-9a-f]{12}\.json", f):
            os.replace(full, os.path.join(META, f))
    for f in os.listdir(CACHE):
        m = re.fullmatch(r"([0-9a-f]{12})\.webm", f)
        if m:
            title = ""
            try:
                title = json.load(open(meta_path(m.group(1))))["title"]
            except Exception:
                pass
            os.replace(os.path.join(CACHE, f), os.path.join(CACHE, f"{safe_name(title)} [{m.group(1)}].webm"))
        elif ".dl." in f or f.endswith(".part.webm") or ".screen." in f:
            try:
                os.remove(os.path.join(CACHE, f))
            except OSError:
                pass


def folder_size(d):
    total = 0
    for root, _, files in os.walk(d):
        for f in files:
            try:
                total += os.path.getsize(os.path.join(root, f))
            except OSError:
                pass
    return total


def media_path(k):
    return os.path.join(MEDIA, k + ".webm")


def save_meta(i, **kw):
    try:
        old = json.load(open(meta_path(i)))
    except Exception:
        old = {}
    old.update(kw)
    json.dump(old, open(meta_path(i), "w"))


def probe(path):
    try:
        r = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "stream=codec_type,codec_name",
                            "-of", "json", path], capture_output=True, text=True, timeout=30)
        ss = json.loads(r.stdout).get("streams", [])
        v = next((s["codec_name"] for s in ss if s["codec_type"] == "video"), None)
        a = next((s["codec_name"] for s in ss if s["codec_type"] == "audio"), None)
        return v, a
    except Exception:
        return None, None


def duration(path):
    try:
        r = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration",
                            "-of", "default=nw=1:nk=1", path], capture_output=True, text=True, timeout=30)
        return float(r.stdout.strip())
    except Exception:
        return 0.0


def broadcast(obj):
    data = json.dumps(obj)
    for c in list(clients):
        try:
            c.send(data)
        except Exception:
            clients.discard(c)


def set_job(i, **kw):
    with lock:
        jobs.setdefault(i, {"state": "queued", "progress": 0, "title": "", "error": ""}).update(kw)
        j = dict(jobs[i])
    broadcast({"type": "job", "key": i, **j})


# ---------------------------------------------------------------- platform helpers (Linux / Windows)
DHU_TITLE = "Android Auto - Desktop Head Unit"


def find_adb():
    """adb, also where winget / the Android SDK put it (the helper may have started before adb was installed,
    so its PATH wouldn't have it yet)."""
    import shutil
    found = shutil.which("adb")
    if found:
        return found
    if WINDOWS:
        la = os.environ.get("LOCALAPPDATA", "")
        cands = [os.path.join(la, "Microsoft", "WinGet", "Links", "adb.exe"),
                 os.path.join(la, "Android", "Sdk", "platform-tools", "adb.exe")]
        pk = os.path.join(la, "Microsoft", "WinGet", "Packages")
        if os.path.isdir(pk):
            cands += [os.path.join(pk, d, "platform-tools", "adb.exe") for d in os.listdir(pk) if d.startswith("Google.PlatformTools")]
    else:
        cands = [os.path.expanduser("~/Android/Sdk/platform-tools/adb")]
    return next((c for c in cands if os.path.exists(c)), None)


def open_folder(path):
    if WINDOWS:
        os.startfile(path)
    else:
        subprocess.Popen(["xdg-open", path], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True)


def svcl(*args):
    """NirSoft SoundVolumeCommandLine (Windows): lists audio sessions and points apps at an output device."""
    exe = config["windows"]["svcl"]
    if not os.path.isabs(exe) and os.path.exists(os.path.join(HERE, exe)):
        exe = os.path.join(HERE, exe)            # next to BeamDeck's own files
    try:
        return subprocess.run([exe, *args], capture_output=True, text=True, timeout=10)
    except OSError:                              # not installed: no app sound, and Android Auto runs without sound
        return subprocess.CompletedProcess([exe, *args], 1, "", "svcl.exe not found")


def win_sessions():
    """Windows audio sessions as [{name, exe, pid, playing}] (svcl /scomma prints CSV)."""
    import csv, io, tempfile
    out = []
    try:
        f = os.path.join(tempfile.gettempdir(), "beamdeck-svcl.csv")
        svcl("/scomma", f)
        for row in csv.DictReader(io.StringIO(open(f, encoding="utf-8-sig", errors="ignore").read())):
            if row.get("Type") == "Application" and row.get("Direction") == "Render":
                exe = os.path.basename(row.get("Process Path") or row.get("Name") or "")
                out.append({"name": row.get("Name") or exe, "exe": exe, "pid": row.get("Process ID", ""),
                            "playing": (row.get("Device State") or "").lower() != "inactive"})
    except Exception:
        pass
    return out


def win_route(exe, to_cable):
    svcl("/SetAppDefault", config["windows"]["cable_out"] if to_cable else "DefaultRenderDevice", "all", exe)


def dhu_capture_fits():
    """Windows: whether a capture of exactly AA_W x AA_H fits inside the head unit's window as gdigrab sees it
    (its client size times the display scaling). Unknown (no window yet, no ctypes) counts as fitting."""
    try:
        import ctypes
        from ctypes import wintypes
        u, g = ctypes.windll.user32, ctypes.windll.gdi32
        hwnd = u.FindWindowW(None, DHU_TITLE)
        if not hwnd:
            return True
        r = wintypes.RECT()
        u.GetClientRect(hwnd, ctypes.byref(r))
        hdc = u.GetDC(None)
        try:
            sx = g.GetDeviceCaps(hdc, 118) / max(1, g.GetDeviceCaps(hdc, 8))      # DESKTOPHORZRES / HORZRES
            sy = g.GetDeviceCaps(hdc, 117) / max(1, g.GetDeviceCaps(hdc, 10))     # DESKTOPVERTRES / VERTRES
        finally:
            u.ReleaseDC(None, hdc)
        return (r.right - r.left) * sx >= AA_W and (r.bottom - r.top) * sy >= AA_H
    except Exception:
        return True


def grab_args(fps=None):
    """ffmpeg input arguments for the head unit's picture: the hidden X display, or its window on Windows."""
    rate = ["-framerate", str(fps)] if fps else []
    if WINDOWS:
        # The size is given explicitly: for a window, gdigrab multiplies its size by the display scaling (125 %,
        # 150 %…), while the head unit (not DPI-aware) still draws 1280x720. Without it the picture sits in the
        # top-left part of a larger, mostly black capture, which also throws off the Maps detection and the game
        # map. If the window is smaller than that (resized), gdigrab would refuse the size, so it's left out then.
        size = ["-video_size", f"{AA_W}x{AA_H}"] if dhu_capture_fits() else []
        return ["-f", "gdigrab", *rate, "-draw_mouse", "0", *size, "-i", "title=" + DHU_TITLE]
    return ["-f", "x11grab", "-draw_mouse", "0", *rate, "-video_size", f"{AA_W}x{AA_H}", "-i", AA_DISPLAY]


def audio_in_args(sink):
    """ffmpeg input arguments for captured sound: a virtual speaker's monitor, or the virtual cable on Windows."""
    if WINDOWS:
        return ["-f", "dshow", "-audio_buffer_size", "20", "-i", "audio=" + config["windows"]["cable_in"]]
    return ["-f", "pulse", "-fragment_size", "3840", "-i", sink + ".monitor"]


# ---------------------------------------------------------------- download / convert (fallback)
def ffmpeg_progress(i, args, total, state):
    set_job(i, state=state, progress=0)
    p = subprocess.Popen(["ffmpeg", "-y", "-v", "error", "-progress", "pipe:1", *args],
                         stdout=subprocess.PIPE, text=True)
    for line in p.stdout:
        if line.startswith("out_time_us="):
            try:
                set_job(i, progress=min(99, int(int(line.split("=")[1]) / 1e6 / (total or 1) * 100)))
            except ValueError:
                pass
    if p.wait() != 0:
        raise RuntimeError("ffmpeg failed to convert the video")


def to_webm(i, src, final):
    """Chromium-playable WebM (CEF has no H.264/AAC)."""
    v, a = probe(src)
    if src.endswith(".webm") and v in PLAYABLE_V and (a is None or a in PLAYABLE_A):
        if src.startswith(WORK):
            os.replace(src, final)
        elif WINDOWS:
            import shutil
            shutil.copyfile(src, final)          # symlinks need admin rights on Windows
        else:
            os.symlink(src, final)
        return
    tmp = os.path.join(WORK, i + ".part.webm")
    vargs = ["-c:v", "copy"] if v in PLAYABLE_V else [
        "-c:v", "libvpx-vp9", "-vf", "scale='min(1280,iw)':-2", "-b:v", "0", "-crf", "34",
        "-deadline", "realtime", "-cpu-used", "8", "-row-mt", "1"]
    aargs = ["-c:a", "copy"] if a in PLAYABLE_A else ["-c:a", "libopus", "-b:a", "128k"]
    ffmpeg_progress(i, ["-i", src, *vargs, *aargs, tmp], duration(src), "converting")
    os.replace(tmp, final)
    if src.startswith(WORK) and os.path.exists(src):
        os.remove(src)


def download_worker(i, src):
    try:
        local = os.path.expanduser(src[7:] if src.startswith("file://") else src)
        if os.path.isfile(local):
            title = os.path.basename(local)
            set_job(i, title=title)
            to_webm(i, local, os.path.join(CACHE, f"{safe_name(os.path.splitext(title)[0])} [{i}].webm"))
        else:
            set_job(i, state="downloading")
            r = subprocess.run(["yt-dlp", "--no-playlist", "--print", "title", "--skip-download", src],
                               capture_output=True, text=True, timeout=60)
            title = (r.stdout.strip().splitlines() or [src])[0]
            set_job(i, title=title)
            fmt = ("bv*[vcodec^=vp][height<=1080]+ba[acodec=opus]/bv*[vcodec^=vp][height<=1080]+ba/"
                   "bv*[vcodec^=av01][height<=1080]+ba[acodec=opus]/bv*[height<=1080]+ba/b")
            p = subprocess.Popen(["yt-dlp", "--no-playlist", "--newline", "-f", fmt, "--merge-output-format",
                                  "webm/mkv", "-o", os.path.join(WORK, i + ".dl.%(ext)s"), src],
                                 stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
            tail = []
            for line in p.stdout:
                tail = (tail + [line.strip()])[-4:]
                if m := re.search(r"\[download\]\s+([\d.]+)%", line):
                    set_job(i, progress=int(float(m.group(1))))
            if p.wait() != 0:
                raise RuntimeError("yt-dlp failed: " + " | ".join(tail))
            got = [f for f in os.listdir(WORK) if f.startswith(i + ".dl.") and not f.endswith(".part")]
            if not got:
                raise RuntimeError("yt-dlp produced no file")
            to_webm(i, os.path.join(WORK, got[0]), os.path.join(CACHE, f"{safe_name(title)} [{i}].webm"))
        save_meta(i, title=title, src=src, stream=False)
        set_job(i, state="ready", progress=100)
        ensure_screen_file(i, None, False)
    except Exception as e:
        set_job(i, state="error", error=str(e)[:400])


def job_status(i):
    with lock:
        if i in jobs and not (jobs[i]["state"] == "ready" and not os.path.exists(out_path(i))):
            return dict(jobs[i])
        jobs.pop(i, None)
    if os.path.exists(out_path(i)):
        title = ""
        try:
            title = json.load(open(meta_path(i)))["title"]
        except Exception:
            pass
        return {"state": "ready", "progress": 100, "title": title, "error": ""}
    return {"state": "unknown", "progress": 0, "title": "", "error": ""}


# ---------------------------------------------------------------- streaming
def resolve_stream(src):
    sid = key_of("stream:" + src)
    st = streams.get(sid)
    if st and time.time() - st["at"] < 3600:
        return sid, st
    r = subprocess.run(["yt-dlp", "-j", "--no-playlist", "-f", STREAM_FMT, src],
                       capture_output=True, text=True, timeout=90)
    if r.returncode != 0:
        return None, None
    d = json.loads(r.stdout.splitlines()[0])
    fmts = d.get("requested_formats") or [d]
    v = next((f for f in fmts if f.get("vcodec") not in (None, "none")), None)
    a = next((f for f in fmts if f is not v and f.get("acodec") not in (None, "none")), None)
    if not v:
        return None, None
    part = lambda f: {"url": f["url"], "headers": f.get("http_headers", {}), "size": f.get("filesize")}
    st = {"title": d.get("title") or src, "src": src, "at": time.time(), "v": part(v), "a": part(a) if a else None}
    streams[sid] = st
    save_meta(sid, title=st["title"], src=src, stream=True)
    return sid, st


def upstream(part, start, end):
    h = dict(part["headers"])
    h["Range"] = f"bytes={start}-{end}"
    return urllib.request.urlopen(urllib.request.Request(part["url"], headers=h), timeout=30)


def upstream_size(part):
    if not part.get("size"):
        with upstream(part, 0, 0) as r:
            part["size"] = int(r.headers["Content-Range"].split("/")[1])
    return part["size"]


# ---------------------------------------------------------------- car-screen copies
def prune_media(keep=8):
    files = sorted((f for f in os.listdir(MEDIA) if f.endswith(".webm") and ".part." not in f),
                   key=lambda f: os.path.getmtime(os.path.join(MEDIA, f)), reverse=True)
    for f in files[keep:]:
        if f[:-5] != player["key"]:
            try:
                os.remove(os.path.join(MEDIA, f))
            except OSError:
                pass


def screen_worker(key, src, is_stream):
    """Small video-only WebM for the in-car screens, written into the mod folder."""
    final = media_path(key)
    tmp = os.path.join(MEDIA, key + ".part.webm")  # same filesystem as the final file
    try:
        if is_stream:
            for f in os.listdir(WORK):
                if f.startswith(key + ".screen.dl"):
                    os.remove(os.path.join(WORK, f))
            r = subprocess.run(["yt-dlp", "--no-playlist", "-f", SCREEN_FMT, "-o",
                                os.path.join(WORK, key + ".screen.dl.%(ext)s"), src],
                               capture_output=True, text=True, timeout=900)
            got = [f for f in os.listdir(WORK) if f.startswith(key + ".screen.dl.") and not f.endswith(".part")]
            if r.returncode != 0 or not got:
                raise RuntimeError("screen download failed")
            source = os.path.join(WORK, got[0])
        else:
            source = out_path(key)
        v, _ = probe(source)
        vargs = ["-c:v", "copy"] if v in PLAYABLE_V else [
            "-c:v", "libvpx-vp9", "-vf", "scale='min(854,iw)':-2", "-b:v", "0", "-crf", "36",
            "-deadline", "realtime", "-cpu-used", "8", "-row-mt", "1"]
        subprocess.run(["ffmpeg", "-y", "-v", "error", "-i", source, "-an", *vargs, tmp], check=True, timeout=1800)
        os.replace(tmp, final)
        if is_stream:
            os.remove(source)
        screen_jobs[key] = "ready"
        prune_media()
    except Exception:
        screen_jobs[key] = "error"
    broadcast({"type": "screenfile", "key": key, "state": screen_jobs[key]})


def ensure_screen_file(key, src, is_stream):
    if screen_jobs.get(key) == "ready" and not os.path.exists(media_path(key)):
        screen_jobs.pop(key, None)
    if os.path.exists(media_path(key)):
        os.utime(media_path(key))
        screen_jobs[key] = "ready"
        return
    if screen_jobs.get(key) == "working":
        return
    screen_jobs[key] = "working"
    threading.Thread(target=screen_worker, args=(key, src, is_stream), daemon=True).start()


# ---------------------------------------------------------------- game link (UDP)
udp = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
udp.bind(("127.0.0.1", MY_UDP))


def player_pos():
    p = player["pos"]
    if not player["paused"]:
        p += time.time() - player["at"]
    return p


def udp_rx():
    while True:
        try:
            data, _ = udp.recvfrom(65535)
            d = json.loads(data)
            if "tap" in d:                       # a click on the car's screen
                aa_tap(float(d["tap"][0]), float(d["tap"][1]))
                continue
            if "cmd" in d:                       # a game control (push to talk, volume…): for the window
                broadcast({"type": "gamecmd", "cmd": str(d["cmd"]), "on": bool(d.get("on"))})
                continue
            d["at"] = ms()
            aa_daynight(bool(d.get("lights")))
            game.clear()
            game.update(d)
            broadcast({"type": "game", **d})
        except Exception:
            pass


def udp_tx():
    while True:
        key = player["key"]
        ready = key and screen_jobs.get(key) == "ready" and os.path.exists(media_path(key))
        msg = {"on": player["screens"], "aa": aa["want"], "calib": player.get("calib", 0),
               "gamemap": gamemap_shown(), "np": np_state(),
               "file": f"/beamdeck_media/{key}.webm" if ready else "",
               "pos": round(player_pos(), 3), "paused": player["paused"]}
        try:
            udp.sendto(json.dumps(msg).encode(), ("127.0.0.1", GAME_PORT))
        except Exception:
            pass
        time.sleep(0.2)


# ---------------------------------------------------------------- Android Auto (Google's Desktop Head Unit)
# The DHU runs on a hidden X display; its picture is captured as JPEG frames and sent to the car screens
# and the window; taps come back as mouse clicks on that display. The phone connects over adb (USB or
# Wireless debugging) with Android Auto's "head unit server" running. No Bluetooth.
DHU_DIR = os.path.join(DATA_DIR, "dhu")
AA_DISPLAY, AA_W, AA_H, AA_FPS = ":87", 1280, 720, 10
FRAME_PORT = int(os.environ.get("BEAMDECK_FRAME_PORT", "47133"))          # the game reads frames here over plain TCP (its web views can't load from the network)
game_frame_clients = set()
aa = {"want": False, "state": "off", "error": "", "procs": {}}
aa_log = []          # the head unit's latest messages (shown on /debug)

# colour correction for the car screen (its material glows): ffmpeg "eq" values, set from the window
LOOK_FILE = os.path.join(META, "screen-look.json")
LOOK_DEFAULT = {"brightness": 0.0, "contrast": 1.0, "saturation": 1.0, "gamma": 1.0, "tint": 1.0}   # on top of the page's look
try:
    look = dict(LOOK_DEFAULT, **json.load(open(LOOK_FILE)))
except Exception:
    look = dict(LOOK_DEFAULT)


def start_capture():
    aa_kill("ffmpeg")
    # tint < 1 pulls green down (towards magenta), > 1 adds green
    eq = "eq=brightness={brightness}:contrast={contrast}:saturation={saturation}:gamma={gamma}:gamma_g={tint}".format(**look)
    ff = subprocess.Popen(["ffmpeg", "-v", "error", *grab_args(AA_FPS),
                           "-vf", "scale=960:540," + eq, "-c:v", "mjpeg", "-q:v", "7", "-f", "image2pipe", "-"],
                          stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
    aa["procs"]["ffmpeg"] = ff
    threading.Thread(target=aa_frames, args=(ff,), daemon=True).start()


def set_look(m):
    for k, (lo, hi) in {"brightness": (-0.5, 0.5), "contrast": (0.5, 2.0),
                        "saturation": (0.0, 2.5), "gamma": (0.3, 2.0), "tint": (0.6, 1.4)}.items():
        if k in m:
            look[k] = round(max(lo, min(hi, float(m[k]))), 3)
    if m.get("reset"):
        look.update(LOOK_DEFAULT)
    json.dump(look, open(LOOK_FILE, "w"))
    if aa["procs"].get("ffmpeg"):
        start_capture()
    broadcast({"type": "look", **look})


def aa_set(state, error=""):
    aa["state"], aa["error"] = state, error
    broadcast({"type": "aa", "state": state, "error": error, "on": aa["want"]})


def aa_kill(name):
    p = aa["procs"].pop(name, None)
    if p and p.poll() is None:
        p.terminate()
        try:
            p.wait(3)
        except Exception:
            p.kill()


def aa_frames(proc):
    """Split ffmpeg's MJPEG stream into JPEGs and send each to the clients that want frames."""
    buf = b""
    while True:
        chunk = proc.stdout.read(65536)
        if not chunk:
            break
        buf += chunk
        while True:
            a = buf.find(b"\xff\xd8")
            b = buf.find(b"\xff\xd9", a + 2) if a >= 0 else -1
            if a < 0 or b < 0:
                break
            frame, buf = buf[a:b + 2], buf[b + 2:]
            if game_frame_clients:
                game_send(b"F", base64.b64encode(frame))
            for c in list(clients):
                if c.frames and not c.busy:
                    c.busy = True
                    try:
                        c.send(b"J" + frame, binary=True)
                    except Exception:
                        clients.discard(c)
                    c.busy = False
    # Windows: gdigrab fails at once while the head unit's window isn't open yet (or was closed); try again
    if WINDOWS and aa["want"] and aa["procs"].get("ffmpeg") is proc:
        dhu = aa["procs"].get("dhu")
        if dhu and dhu.poll() is None:
            time.sleep(1)
            if aa["procs"].get("ffmpeg") is proc:
                start_capture()


AA_SINK = "beamdeck_aa"


def aa_sink(on):
    """A silent virtual speaker for the head unit, so its sound goes through the window's car effect."""
    have = AA_SINK in subprocess.run(["pactl", "list", "short", "sinks"], capture_output=True, text=True).stdout
    if on and not have:
        r = subprocess.run(["pactl", "load-module", "module-null-sink", f"sink_name={AA_SINK}",
                            "sink_properties=device.description=BeamDeck-Android-Auto"], capture_output=True, text=True)
        aa["sink_module"] = r.stdout.strip()
    elif not on and have:
        mods = subprocess.run(["pactl", "list", "short", "modules"], capture_output=True, text=True).stdout
        for line in mods.splitlines():
            if f"sink_name={AA_SINK}" in line:
                subprocess.run(["pactl", "unload-module", line.split()[0]], capture_output=True)


def aa_route(dhu_pid):
    """Moves the head unit's sound stream onto the virtual speaker (it opens it whenever it likes)."""
    for line in subprocess.run(["pactl", "list", "short", "sinks"], capture_output=True, text=True).stdout.splitlines():
        if line.split("\t")[1:2] == [AA_SINK]:
            aa["sink_index"] = int(line.split("\t")[0])
    while aa["want"] and aa["procs"].get("dhu") and aa["procs"]["dhu"].poll() is None:
        try:
            out = subprocess.run(["pactl", "-f", "json", "list", "sink-inputs"], capture_output=True, text=True).stdout
            for si in json.loads(out or "[]"):
                props = si.get("properties", {})
                ours = str(props.get("application.process.id")) == str(dhu_pid) or \
                    "desktop-head-unit" in str(props.get("node.name", "")) + str(props.get("application.name", ""))
                if ours and si.get("sink") != aa.get("sink_index"):
                    subprocess.run(["pactl", "move-sink-input", str(si["index"]), AA_SINK], capture_output=True)
        except Exception:
            pass
        time.sleep(1.5)


def aa_daynight(night):
    """Headlights on → Android Auto's night theme (the DHU console's day/night commands)."""
    dhu = aa["procs"].get("dhu")
    if not dhu or dhu.poll() is not None or aa.get("night") == night or aa["state"] != "on":
        return
    try:
        dhu.stdin.write("night\n" if night else "day\n")
        dhu.stdin.flush()
        aa["night"] = night
    except Exception:
        pass


AA_KEYS = {"playpause": "media_play_pause", "next": "media_next", "prev": "media_previous"}


def aa_key(name):
    """A media key to the phone through the head unit (like a car's steering-wheel buttons). True if sent."""
    dhu = aa["procs"].get("dhu")
    if name not in AA_KEYS or not dhu or dhu.poll() is not None or aa["state"] != "on":
        return False
    try:
        dhu.stdin.write(f"keycode {AA_KEYS[name]}\n")
        dhu.stdin.flush()
        return True
    except Exception:
        return False


def aa_stop():
    aa.pop("night", None)
    for n in ("audio", "ffmpeg", "dhu"):
        aa_kill(n)


def aa_run():
    """Keeps the head unit running while Android Auto is switched on."""
    while True:
        if not aa["want"]:
            for n in ("audio", "ffmpeg", "dhu", "xvfb"):
                aa_kill(n)
            if not WINDOWS:
                aa_sink(False)
            if not WINDOWS and os.path.exists("/tmp/.X11-unix/X" + AA_DISPLAY[1:]):   # one not started by BeamDeck
                subprocess.run(["pkill", "-f", "^Xvfb " + AA_DISPLAY], capture_output=True)
            if aa["state"] != "off":
                aa_set("off")
            time.sleep(0.5)
            continue
        try:
            if not WINDOWS and not os.path.exists("/tmp/.X11-unix/X" + AA_DISPLAY[1:]):
                aa["procs"]["xvfb"] = subprocess.Popen(
                    ["Xvfb", AA_DISPLAY, "-screen", "0", f"{AA_W}x{AA_H}x24", "-nolisten", "tcp"],
                    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                time.sleep(1)
            usb = config.get("aa_transport", "usb") != "adb"
            exe = "desktop-head-unit.exe" if WINDOWS else "desktop-head-unit"
            if not os.path.exists(os.path.join(DHU_DIR, exe)):
                aa_set("error", "Android Auto's head unit isn't installed. Run the installer again with -AndroidAuto "
                                "(Windows) / install.sh (Linux).")
                time.sleep(5)
                continue
            if not usb:                       # developer route: Android Auto's head unit server, through adb
                adb = find_adb()
                if not adb:
                    aa_set("error", "adb wasn't found. Install it (winget install Google.PlatformTools), then restart "
                                    "BeamDeck (or the PC) so it's picked up.")
                    time.sleep(5)
                    continue
                dev = subprocess.run([adb, "get-state"], capture_output=True, text=True, timeout=10)
                if dev.stdout.strip() != "device":
                    aa_set("nophone", "Phone not connected over adb. Wireless: turn on Wireless debugging and run "
                                       "'adb connect <phone IP>:<port>' (shown on the phone); or use a USB cable.")
                    time.sleep(3)
                    continue
                subprocess.run([adb, "forward", "tcp:5277", "tcp:5277"], capture_output=True, timeout=10)
            aa_set("starting")
            env = dict(os.environ) if WINDOWS else dict(os.environ, DISPLAY=AA_DISPLAY, WAYLAND_DISPLAY="")
            # stdin must stay open: the DHU reads console commands and quits on end of input
            # USB (default): the phone switches to accessory mode like it does for a real car, so Android Auto's
            # developer mode isn't needed. adb: the developer "head unit server" (also works over Wi-Fi adb).
            dhu = subprocess.Popen([os.path.join(DHU_DIR, exe), "--usb" if usb else "--adb", "-c",
                                    os.path.join(DHU_DIR, "config", "default_720p.ini")],
                                   cwd=DHU_DIR, env=env, stdin=subprocess.PIPE,
                                   stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
            aa["procs"]["dhu"] = dhu
            if WINDOWS:                       # its sound into the virtual cable (app sound shares it)
                time.sleep(2)
                win_route("desktop-head-unit.exe", True)
            else:
                aa_sink(True)
                threading.Thread(target=aa_route, args=(dhu.pid,), daemon=True).start()
            aa["procs"]["audio"] = pcm_capture(AA_SINK, b"A")
            start_capture()
            for line in dhu.stdout:
                if line.strip() and "ALSA" not in line:     # the head unit's own messages, for troubleshooting
                    aa_log.append(time.strftime("%H:%M:%S ") + line.strip()[:200])
                    del aa_log[:-40]
                    print("DHU:", line.strip(), flush=True)  # (helper.log on Windows, the journal on Linux)
                if "negotiation finished" in line or "Verify returned: ok" in line:
                    aa_set("on")
                elif "No device found ready yet" in line and aa["state"] != "nophone":
                    aa_set("nophone", "Plug the phone in with a USB cable (Android Auto starts by itself).")
                elif "Failed to read from transport" in line:
                    aa_set("lost", "The phone dropped the connection. On the phone: unlock it and accept the new car; "
                                   "Android Auto settings > Previously connected cars > menu > 'Add new cars' on, and "
                                   "nothing under 'Rejected cars'. (Head unit server mode: is the server running?)")
                if not aa["want"]:
                    break
            reached = aa["state"] == "on"
            aa_stop()
            if aa["want"]:
                aa["fails"] = 0 if reached else aa.get("fails", 0) + 1
                if aa["fails"] >= 3:          # don't open and close the head unit forever: stop and say why
                    aa["want"], aa["fails"] = False, 0
                    save_session()
                    last = next((l[9:] for l in reversed(aa_log) if l[9:].strip()), "")
                    why = aa["error"] if aa["state"] == "lost" else f"The head unit kept closing. Its last message: {last}"
                    aa_set("error", "Android Auto stopped after 3 tries. " + why)
                    broadcast({"type": "aa", "state": "error", "error": aa["error"], "on": False})
                    continue
                if aa["state"] not in ("lost", "nophone"):
                    aa_set("lost", "The head unit stopped. Retrying…")
                time.sleep(3)
        except Exception as e:
            aa_stop()                         # don't leave a head unit running for the retry to start another
            if isinstance(e, FileNotFoundError):  # Windows doesn't say which file: name it (ffmpeg? adb? head unit?)
                missing = e.filename or (e.args[1] if len(e.args) > 1 else "")
                aa_set("error", f"Couldn't start '{missing}' (not found). If it's ffmpeg or adb, install it and restart "
                                "BeamDeck (or the PC); if it's the head unit, run the installer with -AndroidAuto.")
            else:
                aa_set("error", str(e)[:200])
            print("Android Auto:", repr(e), flush=True)          # into helper.log on Windows
            time.sleep(3)


def game_send(kind, b64):
    """To the game over TCP: 8 hex digits of length, then a type byte (F frame, C cover), then base64."""
    msg = b"%08x" % (len(b64) + 1) + kind + b64
    for g in list(game_frame_clients):
        try:
            g.sendall(msg)
        except Exception:
            game_frame_clients.discard(g)
            try:
                g.close()
            except Exception:
                pass


def frame_reaper():
    """Drops game connections the game has closed (it restarted or quit). The game never sends on them, so one
    that turns readable has been closed; otherwise they'd linger half-open until the next frame send failed."""
    import select
    while True:
        time.sleep(2)
        socks = list(game_frame_clients)
        if not socks:
            continue
        try:
            readable, _, _ = select.select(socks, [], [], 0)
        except (OSError, ValueError):
            readable = socks
        for g in readable:
            try:
                gone = g.recv(1, socket.MSG_PEEK) == b""
            except BlockingIOError:
                gone = False
            except OSError:
                gone = True
            if gone:
                game_frame_clients.discard(g)
                try:
                    g.close()
                except Exception:
                    pass


def frame_server():
    srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    srv.bind(("127.0.0.1", FRAME_PORT))
    srv.listen(4)
    while True:
        c, _ = srv.accept()
        c.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        c.settimeout(0.5)            # a stalled game is dropped (it reconnects) instead of holding up frames
        game_frame_clients.add(c)
        if np_cover["b64"]:
            game_send(b"C", np_cover["b64"])


# Where Google Maps is on Android Auto's screen, so the game map can cover it (and only it):
#   "left"  the normal split view (map left, card right)
#   "right" apps menu or another app open on the left, map in the small right pane
#   ""      no map showing (a full-screen app)
# Found by Maps' Search bar: a perfectly even pill (white by day, grey at night) in a known spot.
#   "full"  Maps on its own, full screen
PANES = {"left": (80, 10, 850, 710), "right": (860, 10, 1270, 710), "full": (80, 10, 1270, 710)}
map_pane = {"where": ""}


def find_map(raw):
    """Maps' Search bar: an even run of 200-450 px (white, or grey at night) just above and just below its
    text (rows 47 and 86). App icons are narrower, app title bars wider. The black gap at x 851-858 tells
    the split view from full-screen Maps."""
    W = AA_W

    def runs(y):
        row, out, start = raw[y * W:(y + 1) * W], [], None
        for x in range(81, W + 1):
            ok = x < W and abs(row[x] - row[x - 1]) <= 4 and (row[x] > 200 or 8 < row[x] < 100)   # night bar ~18
            if ok and start is None:
                start = x
            elif not ok and start is not None:
                if 200 <= x - start <= 450:
                    out.append((start, x))
                start = None
        return out

    tops = [r for y in (41, 43, 45, 47) for r in runs(y)]          # just above the text (it sits higher at night)
    bottoms = [r for y in (83, 85, 87, 89) for r in runs(y)]       # just below it
    for a0, a1 in tops:
        for b0, b1 in bottoms:
            if abs(a0 - b0) <= 40 and abs(a1 - b1) <= 40:
                if (a0 + a1) // 2 > 860:
                    return "right"
                gap = [raw[y * W + x] for y in range(100, 600, 25) for x in range(851, 859)]
                return "left" if sum(gap) / len(gap) < 8 else "full"
    return ""


def gamemap_shown():
    return map_pane["where"] if player.get("gamemap", False) else ""


def map_watch():
    """Twice a second: a small grey crop of the head unit's screen, checked for the Search bar."""
    while True:
        if aa["state"] != "on" or not player.get("gamemap"):
            map_pane["where"] = ""
            time.sleep(0.5)
            continue
        try:
            raw = subprocess.run(["ffmpeg", "-v", "error", *grab_args(), "-frames:v", "1",
                                  "-vf", f"scale={AA_W}:{AA_H},format=gray", "-f", "rawvideo", "-"],
                                 capture_output=True, timeout=5).stdout
            where = find_map(raw) if len(raw) == AA_W * AA_H else ""
            # privacy: if the map is lost for a moment (day/night switch, animations), keep covering where
            # it was for 2 s rather than showing the real map underneath
            if where:
                map_pane["seen"] = time.time()
            elif map_pane["where"] and time.time() - map_pane.get("seen", 0) < 2:
                where = map_pane["where"]
            if where != map_pane["where"]:
                broadcast({"type": "pane", "where": where if player.get("gamemap") else ""})
            map_pane["where"] = where
        except Exception:
            pass
        time.sleep(0.5)


taps = []          # recent taps, for /debug


def aa_tap(u, v):
    taps.append({"u": round(u, 3), "v": round(v, 3), "aa": aa["state"], "at": time.strftime("%H:%M:%S")})
    del taps[:-10]
    if aa["state"] != "on" or not (0 <= u <= 1 and 0 <= v <= 1):
        return
    pane = PANES.get(gamemap_shown())
    if pane and pane[0] <= u * AA_W <= pane[2] and pane[1] <= v * AA_H <= pane[3]:
        return                                   # the game map is drawn there; don't poke Google Maps under it
    x, y = int(u * AA_W), int(v * AA_H)
    if WINDOWS:
        # the head unit's SDL window ignores posted mouse messages; its console's "tap x y" (in the head unit's own
        # 1280x720 coordinates, whatever the window's size or focus) touches the screen directly
        dhu = aa["procs"].get("dhu")
        try:
            dhu.stdin.write(f"tap {x} {y}\n")
            dhu.stdin.flush()
        except Exception:
            pass
        return
    subprocess.Popen(["xdotool", "mousemove", str(x), str(y), "click", "1"],
                     env=dict(os.environ, DISPLAY=AA_DISPLAY), stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


# ---------------------------------------------------------------- websocket
WS_GUID = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"


class WS:
    def __init__(self, sock):
        self.sock, self.wlock = sock, threading.Lock()
        self.frames = False      # wants live Android Auto frames
        self.busy = False        # a frame is still being written (slow clients skip frames)

    def send(self, data, binary=False):
        data = data.encode() if isinstance(data, str) else data
        n = len(data)
        hdr = bytes([0x82 if binary else 0x81]) + (bytes([n]) if n < 126 else
                               bytes([126]) + struct.pack(">H", n) if n < 65536 else bytes([127]) + struct.pack(">Q", n))
        with self.wlock:
            self.sock.sendall(hdr + data)

    def _read(self, n):
        b = b""
        while len(b) < n:
            c = self.sock.recv(n - len(b))
            if not c:
                raise ConnectionError
            b += c
        return b

    def recv(self):
        while True:
            b0, b1 = self._read(2)
            op, n = b0 & 0x0F, b1 & 0x7F
            if n == 126:
                n = struct.unpack(">H", self._read(2))[0]
            elif n == 127:
                n = struct.unpack(">Q", self._read(8))[0]
            mask = self._read(4) if b1 & 0x80 else b"\0\0\0\0"
            data = bytes(x ^ mask[i % 4] for i, x in enumerate(self._read(n)))
            if op == 0x8:
                return None
            if op == 0x9:
                with self.wlock:
                    self.sock.sendall(bytes([0x8A, len(data)]) + data)
                continue
            if op == 0x1:
                return data.decode()
            if op == 0x2:
                return data


SESSION_FILE = os.path.join(META, "session.json")


def save_session():
    try:
        json.dump({"aa": aa["want"], "gamemap": player.get("gamemap", False)}, open(SESSION_FILE, "w"))
    except Exception:
        pass


def load_session():
    try:
        d = json.load(open(SESSION_FILE))
        aa["want"], player["gamemap"] = bool(d.get("aa")), bool(d.get("gamemap"))
    except Exception:
        pass


windows = []         # BeamDeck windows, oldest first; only the newest plays shared / captured sound


def set_active_window():
    """One active window: it gets Android Auto / app sound and does the multiplayer music. Older ones step back,
    so a second window never doubles the sound."""
    for w in list(windows):
        on = w is windows[-1]
        if getattr(w, "active", None) != on:
            w.active = on
            try:
                w.send(json.dumps({"type": "active", "on": on}))
            except Exception:
                pass


def window_gone():
    """The BeamDeck window closed: if no window comes back within 5 s (F5 reconnects), turn Android Auto off."""
    time.sleep(5)
    if aa["want"] and not any(getattr(c, "window", False) for c in list(clients)):
        aa["want"] = False
        aa_stop()
        save_session()
        broadcast({"type": "aa", "state": aa["state"], "error": "", "on": False})


def remote_screen(data):
    """Another player's car screen (BeamMP), passed on by the window: R + [name len][name][type][payload].
    To the game as kind R: "<name>\t<type>\t<base64 or JSON>"; it puts it on that player's car."""
    try:
        n = data[1]
        name, kind, payload = data[2:2 + n], data[2 + n:3 + n], data[3 + n:]
        body = payload if kind == b"N" else base64.b64encode(payload)
        game_send(b"R", name + b"\t" + kind + b"\t" + body)
    except Exception:
        pass


def on_ws_message(m, ws=None):
    if m.get("type") == "frames" and ws:
        ws.frames = bool(m.get("on"))
        return
    if m.get("type") == "audio" and ws:
        ws.audio = bool(m.get("on"))
        return
    if m.get("type") == "aakey":               # play / pause / next / previous for Android Auto's music
        if not aa_key(str(m.get("key", ""))):
            game_send(b"T", b"BeamDeck: Android Auto isn't connected")
        return
    if m.get("type") == "toast":               # a short in-game message from the window
        game_send(b"T", str(m.get("text", ""))[:120].encode())
        return
    if m.get("type") == "mpdebug":             # the window's multiplayer state, for /debug
        player["mpdebug"] = {**m, "at": time.strftime("%H:%M:%S")}
        return
    t = m.get("type")
    if t == "state":
        player.update(pos=float(m.get("pos", 0)), paused=bool(m.get("paused")), at=time.time())
        if "key" in m:
            player["key"], player["title"] = m["key"], m.get("title", "")
    elif t == "bass":
        player["bass"] = (round(float(m.get("v", 0)), 3), time.strftime("%H:%M:%S"))
        try:
            udp.sendto(json.dumps({"bass": round(float(m.get("v", 0)), 3)}).encode(), ("127.0.0.1", GAME_PORT))
        except Exception:
            pass
    elif t == "screens":
        player["screens"] = bool(m.get("on"))
        broadcast({"type": "screensState", "on": player["screens"]})
    elif t == "aa":
        aa["want"] = bool(m.get("on"))
        aa["fails"] = 0
        save_session()
        if not aa["want"]:
            aa_stop()
        if aa["want"]:
            player["screens"] = True
            broadcast({"type": "screensState", "on": True})
        broadcast({"type": "aa", "state": aa["state"], "error": aa["error"], "on": aa["want"]})
    elif t == "look":
        set_look(m)
    elif t == "gamemap":
        player["gamemap"] = bool(m.get("on"))
        save_session()
        broadcast({"type": "gamemap", "on": player["gamemap"]})
    elif t == "tap":
        aa_tap(float(m.get("u", -1)), float(m.get("v", -1)))
    elif t == "appsound":
        appcap["key"] = str(m.get("key") or "")
        if not appcap["key"] and np.get("source") == "app":
            np.update(title="", source="")
        broadcast({"type": "appsound", "key": appcap["key"]})
    elif t == "np":                      # what the window is playing (music library, Navidrome, a link)
        if not appcap["key"]:
            np.update(title=m.get("title", ""), artist=m.get("artist", ""), album=m.get("album", ""),
                      dur=float(m.get("dur") or 0), cover=m.get("cover", ""), source="window")
    elif t == "rescan":
        threading.Thread(target=lib_scan, daemon=True).start()
    elif t == "calibrate":
        player["calib"] = int(time.time())   # the game starts a set-up when this goes up


# ---------------------------------------------------------------- shared: virtual speakers + PCM to the window
def sink_on(name, desc):
    """A silent virtual speaker; whatever plays on it is captured and played through the window's car effect."""
    have = name in subprocess.run(["pactl", "list", "short", "sinks"], capture_output=True, text=True).stdout
    if not have:
        subprocess.run(["pactl", "load-module", "module-null-sink", f"sink_name={name}",
                        f"sink_properties=device.description={desc}"], capture_output=True, text=True)


def sink_off(name):
    mods = subprocess.run(["pactl", "list", "short", "modules"], capture_output=True, text=True).stdout
    for line in mods.splitlines():
        if f"sink_name={name}" in line:
            subprocess.run(["pactl", "unload-module", line.split()[0]], capture_output=True)


def sink_index(name):
    for line in subprocess.run(["pactl", "list", "short", "sinks"], capture_output=True, text=True).stdout.splitlines():
        parts = line.split("\t")
        if len(parts) > 1 and parts[1] == name:
            return int(parts[0])
    return None


def pcm_capture(sink, tag):
    """ffmpeg reading a virtual speaker's monitor as 48 kHz stereo s16le; chunks go to the window as tag + PCM."""
    proc = subprocess.Popen(["ffmpeg", "-v", "error", *audio_in_args(sink), "-ac", "2", "-ar", "48000", "-f", "s16le", "-"],
                            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)

    def pump():
        while True:
            chunk = proc.stdout.read(3840)
            if not chunk:
                break
            for c in list(clients):
                if getattr(c, "audio", False) and getattr(c, "active", True):
                    try:
                        c.send(tag + chunk, binary=True)
                    except Exception:
                        clients.discard(c)
    threading.Thread(target=pump, daemon=True).start()
    return proc


def sink_inputs():
    try:
        return json.loads(subprocess.run(["pactl", "-f", "json", "list", "sink-inputs"],
                                         capture_output=True, text=True, timeout=5).stdout or "[]")
    except Exception:
        return []


# ---------------------------------------------------------------- any app's sound through the car
APP_SINK = "beamdeck_car"
appcap = {"key": "", "proc": None, "moved": set()}


def window_pids():
    """BeamDeck's own window (its browser and children): left out of the app list so it can't loop."""
    if WINDOWS:
        return set()
    try:
        root = int(open(os.path.join(DATA_DIR, "window.pid")).read().strip())
    except Exception:
        return set()
    parent = {}
    for d in os.listdir("/proc"):
        if d.isdigit():
            try:
                parent[int(d)] = int(open(f"/proc/{d}/stat").read().rsplit(")", 1)[1].split()[1])
            except Exception:
                pass
    out, todo = {root}, [root]
    while todo:
        p = todo.pop()
        for c, pp in parent.items():
            if pp == p and c not in out:
                out.add(c)
                todo.append(c)
    return out


def app_key(props):
    return props.get("application.process.binary") or props.get("application.name") or props.get("node.name") or "?"


def window_browser_exe():
    """Windows: the browser BeamDeck's window runs in (its sound must not be routed back into itself)."""
    b = config.get("browser") or "msedge"
    return os.path.basename(b).lower().replace(".exe", "") + ".exe"


def list_apps():
    if WINDOWS:
        apps, skip = {}, {"beamng.drive.x64.exe", "beamng.drive.exe", "desktop-head-unit.exe", window_browser_exe()}
        for w in win_sessions():
            if w["exe"].lower() in skip:
                continue
            a = apps.setdefault(w["exe"], {"key": w["exe"], "name": w["name"], "streams": 0, "playing": False})
            a["streams"] += 1
            a["playing"] = a["playing"] or w["playing"]
        if appcap["key"] and appcap["key"] not in apps:
            apps[appcap["key"]] = {"key": appcap["key"], "name": appcap["key"], "streams": 0, "playing": False}
        return sorted(apps.values(), key=lambda a: (not a["playing"], a["name"].casefold()))
    mine, apps = window_pids(), {}
    for si in sink_inputs():
        pr = si.get("properties", {})
        try:
            pid = int(pr.get("application.process.id", 0))
        except ValueError:
            pid = 0
        node = str(pr.get("node.name", ""))
        if pid in mine or "desktop-head-unit" in node or pr.get("application.process.binary", "").startswith("BeamNG"):
            continue
        k = app_key(pr)
        a = apps.setdefault(k, {"key": k, "name": pr.get("application.name") or k, "streams": 0, "playing": False})
        a["streams"] += 1
        a["playing"] = a["playing"] or not si.get("corked", False)
    if appcap["key"] and appcap["key"] not in apps:
        apps[appcap["key"]] = {"key": appcap["key"], "name": appcap["key"], "streams": 0, "playing": False}
    return sorted(apps.values(), key=lambda a: (not a["playing"], a["name"].casefold()))


def appcap_loop_windows():
    """Windows: point the chosen app at the virtual cable and capture the cable (shared with Android Auto)."""
    routed = ""
    while True:
        key = appcap["key"]
        try:
            if key != routed:
                if routed:
                    win_route(routed, False)
                if key:
                    win_route(key, True)
                routed = key
            aa_capturing = aa["procs"].get("audio") and aa["procs"]["audio"].poll() is None
            if key and not aa_capturing and (not appcap["proc"] or appcap["proc"].poll() is not None):
                appcap["proc"] = pcm_capture(None, b"B")
            elif (not key or aa_capturing) and appcap["proc"]:
                appcap["proc"].terminate()
                appcap["proc"] = None
        except Exception:
            pass
        time.sleep(1)


def appcap_loop():
    if WINDOWS:
        return appcap_loop_windows()
    while True:
        key = appcap["key"]
        try:
            if key:
                sink_on(APP_SINK, "BeamDeck-Car")
                idx = sink_index(APP_SINK)
                for si in sink_inputs():       # apps open new streams (per song/tab), so keep moving them
                    if app_key(si.get("properties", {})) == key and si.get("sink") != idx:
                        subprocess.run(["pactl", "move-sink-input", str(si["index"]), APP_SINK], capture_output=True)
                        appcap["moved"].add(si["index"])
                if not appcap["proc"] or appcap["proc"].poll() is not None:
                    appcap["proc"] = pcm_capture(APP_SINK, b"B")
            elif appcap["proc"] or appcap["moved"]:
                if appcap["proc"]:
                    appcap["proc"].terminate()
                    appcap["proc"] = None
                idx = sink_index(APP_SINK)
                for si in sink_inputs():       # give the app its normal speakers back
                    if si.get("sink") == idx:
                        subprocess.run(["pactl", "move-sink-input", str(si["index"]), "@DEFAULT_SINK@"], capture_output=True)
                appcap["moved"].clear()
                sink_off(APP_SINK)
        except Exception:
            pass
        time.sleep(1)


# ---------------------------------------------------------------- now playing (title, artist, cover) for the car screen
np = {"title": "", "artist": "", "album": "", "dur": 0, "source": "", "cover": "", "pos": 0, "paused": True}
np_cover = {"key": None, "b64": b""}


def smtc_now(app):
    """Windows: the system media controls (needs the optional 'winrt' package: pip install winrt-runtime
    winrt-Windows.Media.Control winrt-Windows.Foundation)."""
    try:
        import asyncio
        from winrt.windows.media.control import GlobalSystemMediaTransportControlsSessionManager as M
    except Exception:
        return None

    async def get():
        mgr = await M.request_async()
        name = app.lower().replace(".exe", "")
        pick = next((x for x in mgr.get_sessions() if name in (x.source_app_user_model_id or "").lower()),
                    mgr.get_current_session())
        if not pick:
            return None
        props = await pick.try_get_media_properties_async()
        tl, info = pick.get_timeline_properties(), pick.get_playback_info()
        return {"title": props.title or "", "artist": props.artist or "", "album": props.album_title or "",
                "cover": "", "pos": tl.position.total_seconds() if hasattr(tl.position, "total_seconds") else 0,
                "dur": tl.end_time.total_seconds() if hasattr(tl.end_time, "total_seconds") else 0,
                "paused": int(info.playback_status) != 4, "source": "app"}
    try:
        return asyncio.run(get())
    except Exception:
        return None


def mpris_now(app):
    """playerctl metadata for the player that belongs to the captured app (Spotify, Feishin, a browser…)."""
    if WINDOWS:
        return smtc_now(app)
    players = subprocess.run(["playerctl", "-l"], capture_output=True, text=True).stdout.split()
    name = app.lower().split("/")[-1]
    pick = next((p for p in players if p.split(".")[0].lower() in name or name in p.lower()), None)
    if not pick:
        return None
    fmt = "{{status}}\t{{title}}\t{{artist}}\t{{album}}\t{{mpris:artUrl}}\t{{position}}\t{{mpris:length}}"
    out = subprocess.run(["playerctl", "-p", pick, "metadata", "--format", fmt], capture_output=True, text=True).stdout
    f = out.rstrip("\n").split("\t")
    if len(f) < 7:
        return None
    num = lambda x: (int(x) / 1e6) if x.strip().isdigit() else 0
    return {"title": f[1], "artist": f[2], "album": f[3], "cover": f[4], "pos": num(f[5]), "dur": num(f[6]),
            "paused": f[0] != "Playing", "source": "app"}


def cover_bytes(ref):
    """Cover image bytes for a reference: /lib/cover/<id>, /nd/cover/<id>, file://… or http(s)://…"""
    try:
        if ref.startswith("/lib/cover/"):
            return lib_cover(ref.rsplit("/", 1)[1])
        if ref.startswith("/nd/cover/"):
            return nd_raw("getCoverArt", id=ref.rsplit("/", 1)[1], size=300)[0]
        if ref.startswith("file://"):
            return open(urllib.parse.unquote(ref[7:]), "rb").read()
        if ref.startswith("http"):
            return urllib.request.urlopen(ref, timeout=10).read()
    except Exception:
        return None
    return None


def cover_update(ref):
    """Small square JPEG of the cover, sent to the game (car screens) when it changes."""
    if ref == np_cover["key"]:
        return
    np_cover["key"] = ref
    data = cover_bytes(ref) if ref else None
    b64 = b""
    if data:
        r = subprocess.run(["ffmpeg", "-v", "error", "-i", "pipe:0", "-vf",
                            "scale=256:256:force_original_aspect_ratio=increase,crop=256:256",
                            "-frames:v", "1", "-f", "image2pipe", "-c:v", "mjpeg", "-q:v", "4", "pipe:1"],
                           input=data, capture_output=True, timeout=20)
        b64 = base64.b64encode(r.stdout) if r.returncode == 0 and r.stdout else b""
    np_cover["b64"] = b64
    game_send(b"C", b64)


def np_loop():
    while True:
        try:
            if appcap["key"]:
                m = mpris_now(appcap["key"])
                if m:
                    np.update(m)
            if np.get("cover") != np_cover["key"]:
                threading.Thread(target=cover_update, args=(np.get("cover", ""),), daemon=True).start()
                np_cover["key"] = np.get("cover")
        except Exception:
            pass
        time.sleep(1)


def np_state():
    """What the car screen shows when no video or Android Auto is on it."""
    if not np.get("title"):
        return None
    if np.get("source") == "app":
        pos, paused = np.get("pos", 0), np.get("paused", True)
    else:
        pos, paused = player_pos(), player["paused"]
    return {"t": np["title"][:120], "a": np.get("artist", "")[:120], "al": np.get("album", "")[:120],
            "d": round(np.get("dur", 0) or 0, 1), "p": round(pos, 1), "z": paused}


# ---------------------------------------------------------------- local music library
AUDIO_EXT = {".mp3", ".flac", ".ogg", ".oga", ".opus", ".m4a", ".aac", ".wav", ".webm"}
LIB_FILE = os.path.join(CACHE, "library.json")
COVERS = os.path.join(CACHE, "covers")
os.makedirs(COVERS, exist_ok=True)
lib = {"tracks": {}, "scanning": False, "done": 0, "total": 0}
try:
    lib["tracks"] = json.load(open(LIB_FILE))
except Exception:
    pass


def lib_probe(path):
    try:
        r = subprocess.run(["ffprobe", "-v", "error", "-show_entries",
                            "format=duration:format_tags=title,artist,album,album_artist,track,disc,date",
                            "-of", "json", path], capture_output=True, text=True, timeout=20)
        f = json.loads(r.stdout).get("format", {})
        tags = {k.lower(): v for k, v in f.get("tags", {}).items()}
    except Exception:
        f, tags = {}, {}
    num = lambda x: int(re.match(r"\d*", str(x or "")).group() or 0)
    folder = os.path.basename(os.path.dirname(path))
    return {"path": path, "mtime": os.path.getmtime(path),
            "title": tags.get("title") or os.path.splitext(os.path.basename(path))[0],
            "artist": tags.get("artist") or tags.get("album_artist") or "Unknown artist",
            "albumartist": tags.get("album_artist") or tags.get("artist") or "Unknown artist",
            "album": tags.get("album") or folder, "track": num(tags.get("track")), "disc": num(tags.get("disc")),
            "year": num(tags.get("date"))  if tags.get("date") else 0, "dur": float(f.get("duration") or 0)}


def lib_scan():
    if lib["scanning"]:
        return
    lib["scanning"] = True
    try:
        files = []
        for top in config["music_folders"]:
            for root, _, names in os.walk(os.path.expanduser(top)):
                files += [os.path.join(root, n) for n in names if os.path.splitext(n)[1].lower() in AUDIO_EXT]
        old = {t["path"]: (i, t) for i, t in lib["tracks"].items()}
        lib["total"], lib["done"] = len(files), 0
        new, todo = {}, []
        for path in files:
            i = key_of("lib:" + path)
            o = old.get(path)
            if o and abs(o[1]["mtime"] - os.path.getmtime(path)) < 1:
                new[i] = o[1]
                lib["done"] += 1
            else:
                todo.append((i, path))
        from concurrent.futures import ThreadPoolExecutor
        with ThreadPoolExecutor(4) as ex:
            for (i, path), info in zip(todo, ex.map(lambda ip: lib_probe(ip[1]), todo)):
                new[i] = info
                lib["done"] += 1
        lib["tracks"] = new
        json.dump(new, open(LIB_FILE, "w"))
    finally:
        lib["scanning"] = False
        broadcast({"type": "library", "count": len(lib["tracks"])})


def lib_track(i, t):
    return {"id": i, "title": t["title"], "artist": t["artist"], "album": t["album"], "track": t["track"],
            "disc": t["disc"], "dur": t["dur"], "cover": f"/lib/cover/{i}", "src": f"/lib/file/{i}"}


def lib_cover(i):
    """Embedded cover art, else cover/folder/front image next to the file (cached per album folder)."""
    t = lib["tracks"].get(i)
    if not t:
        return None
    folder = os.path.dirname(t["path"])
    cache = os.path.join(COVERS, key_of("cover:" + folder + "|" + t["album"]) + ".jpg")
    if os.path.exists(cache):
        return open(cache, "rb").read() or None
    data = b""
    r = subprocess.run(["ffmpeg", "-v", "error", "-i", t["path"], "-an", "-frames:v", "1", "-f", "image2pipe",
                        "-c:v", "mjpeg", "-vf", "scale='min(600,iw)':-2", "pipe:1"], capture_output=True, timeout=20)
    if r.returncode == 0:
        data = r.stdout
    if not data:
        for n in sorted(os.listdir(folder)):
            if os.path.splitext(n)[0].lower() in ("cover", "folder", "front", "album") and \
                    os.path.splitext(n)[1].lower() in (".jpg", ".jpeg", ".png", ".webp"):
                data = open(os.path.join(folder, n), "rb").read()
                break
    open(cache, "wb").write(data)
    return data or None


def lib_query(path, q):
    T = lib["tracks"]
    fold = lambda x: str(x).casefold()
    if path == "/lib/status":
        return {"count": len(T), "scanning": lib["scanning"], "done": lib["done"], "total": lib["total"],
                "folders": config["music_folders"]}
    if path == "/lib/artists":
        counts = {}
        for t in T.values():
            a = counts.setdefault(t["albumartist"], set())
            a.add(t["album"])
        return [{"name": a, "albums": len(al)} for a, al in sorted(counts.items(), key=lambda x: fold(x[0]))]
    if path == "/lib/albums":
        albums = {}
        for i, t in T.items():
            if q.get("artist") and t["albumartist"] != q["artist"]:
                continue
            a = albums.setdefault((t["album"], t["albumartist"]), {"album": t["album"], "artist": t["albumartist"],
                                                                   "year": t["year"], "tracks": 0, "cover": f"/lib/cover/{i}"})
            a["tracks"] += 1
        return sorted(albums.values(), key=lambda a: (a["year"] or 9999, fold(a["album"])))
    if path == "/lib/tracks":
        out = [lib_track(i, t) for i, t in T.items()
               if t["album"] == q.get("album") and (not q.get("artist") or t["albumartist"] == q["artist"])]
        return sorted(out, key=lambda t: (t["disc"], t["track"], fold(t["title"])))
    if path == "/lib/search":
        words = fold(q.get("q", "")).split()
        out = [lib_track(i, t) for i, t in T.items()
               if all(w in fold(t["title"] + " " + t["artist"] + " " + t["album"]) for w in words)]
        return sorted(out, key=lambda t: (fold(t["artist"]), fold(t["album"]), t["disc"], t["track"]))[:200]
    return None


# ---------------------------------------------------------------- Navidrome (Subsonic API)
def nd_url(method, **params):
    c = config["navidrome"]
    if not c.get("url"):
        raise RuntimeError("Navidrome isn't set up (Settings)")
    salt = base64.b16encode(os.urandom(6)).decode().lower()
    token = hashlib.md5((c.get("password", "") + salt).encode()).hexdigest()
    q = dict(u=c.get("user", ""), t=token, s=salt, v="1.16.1", c="beamdeck", f="json", **params)
    return c["url"].rstrip("/") + "/rest/" + method + "?" + urllib.parse.urlencode(q)


def nd_raw(method, headers=None, **params):
    r = urllib.request.urlopen(urllib.request.Request(nd_url(method, **params), headers=headers or {}), timeout=20)
    return r.read(), r.headers


def nd(method, **params):
    data = json.loads(nd_raw(method, **params)[0]).get("subsonic-response", {})
    if data.get("status") != "ok":
        raise RuntimeError(data.get("error", {}).get("message", "Navidrome error"))
    return data


def nd_song(s):
    return {"id": s["id"], "title": s.get("title", ""), "artist": s.get("artist", ""), "album": s.get("album", ""),
            "track": s.get("track", 0), "disc": s.get("discNumber", 0), "dur": s.get("duration", 0),
            "cover": f"/nd/cover/{s.get('coverArt', s['id'])}", "src": f"/nd/stream/{s['id']}"}


def nd_query(path, q):
    if path == "/nd/ping":
        nd("ping")
        return {"ok": True}
    if path == "/nd/artists":
        out = []
        for idx in nd("getArtists").get("artists", {}).get("index", []):
            out += [{"id": a["id"], "name": a["name"], "albums": a.get("albumCount", 0)} for a in idx.get("artist", [])]
        return out
    if path == "/nd/albums":
        al = nd("getArtist", id=q.get("id", "")).get("artist", {}).get("album", [])
        return [{"id": a["id"], "album": a["name"], "artist": a.get("artist", ""), "year": a.get("year", 0),
                 "tracks": a.get("songCount", 0), "cover": f"/nd/cover/{a.get('coverArt', a['id'])}"} for a in al]
    if path == "/nd/tracks":
        return [nd_song(s) for s in nd("getAlbum", id=q.get("id", "")).get("album", {}).get("song", [])]
    if path == "/nd/search":
        return [nd_song(s) for s in nd("search3", query=q.get("q", ""), songCount=100, albumCount=0,
                                       artistCount=0).get("searchResult3", {}).get("song", [])]
    return None


# ---------------------------------------------------------------- settings
def audio_type(path):
    return {".mp3": "audio/mpeg", ".flac": "audio/flac", ".ogg": "audio/ogg", ".oga": "audio/ogg",
            ".opus": "audio/ogg", ".m4a": "audio/mp4", ".aac": "audio/aac", ".wav": "audio/wav",
            ".webm": "audio/webm"}.get(os.path.splitext(path)[1].lower(), "application/octet-stream")


def config_public():
    c = json.loads(json.dumps(config))
    c["navidrome"]["has_password"] = bool(c["navidrome"].pop("password", ""))
    c["detected_userfolder"] = USERFOLDER
    c["platform"] = "windows" if WINDOWS else "linux"
    return c


def config_update(d):
    folders_before = list(config["music_folders"])
    for k in ("beamng_userfolder", "browser", "aa_transport", "mp_relay"):
        if k in d:
            config[k] = str(d[k]).strip()
    config["mp_relay"] = normalize_relay(config.get("mp_relay", ""))
    if "mp_share" in d:
        config["mp_share"] = bool(d["mp_share"])
    if "mp_share_screen" in d:
        config["mp_share_screen"] = bool(d["mp_share_screen"])
    if "music_folders" in d:
        # strip quotes too: Explorer's "Copy as path" gives "D:\Music"
        config["music_folders"] = [os.path.expanduser(f.strip().strip('"').strip()) for f in d["music_folders"]
                                   if str(f).strip().strip('"').strip()]
    if "navidrome" in d:
        n = d["navidrome"]
        config["navidrome"]["url"] = str(n.get("url", config["navidrome"]["url"])).strip()
        config["navidrome"]["user"] = str(n.get("user", config["navidrome"]["user"])).strip()
        if n.get("password"):
            config["navidrome"]["password"] = str(n["password"])
    save_config()
    if config["music_folders"] != folders_before:
        threading.Thread(target=lib_scan, daemon=True).start()
    return config_public()


# ---------------------------------------------------------------- HTTP
class H(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *a):
        pass

    def json(self, obj, code=200):
        b = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(b)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(b)

    def do_GET(self):
        u = urllib.parse.urlparse(self.path)
        q = {k: v[0] for k, v in urllib.parse.parse_qs(u.query).items()}

        if self.headers.get("Upgrade", "").lower() == "websocket":
            key = self.headers.get("Sec-WebSocket-Key", "")
            self.send_response(101)
            self.send_header("Upgrade", "websocket")
            self.send_header("Connection", "Upgrade")
            self.send_header("Sec-WebSocket-Accept",
                             base64.b64encode(hashlib.sha1((key + WS_GUID).encode()).digest()).decode())
            self.end_headers()
            self.wfile.flush()
            self.close_connection = True
            ws = WS(self.connection)
            ws.agent = self.headers.get("User-Agent", "")[:80]
            ws.origin = self.headers.get("Origin", "")
            ws.since = time.time()
            ws.window = ws.origin.rstrip("/") in (f"http://127.0.0.1:{PORT}", f"http://localhost:{PORT}")   # BeamDeck's window
            # (some Edge builds on Windows send the origin with a trailing slash)
            clients.add(ws)
            if ws.window:
                windows.append(ws)
            try:
                ws.send(json.dumps({"type": "hello", "screens": player["screens"]}))
                ws.send(json.dumps({"type": "aa", "state": aa["state"], "error": aa["error"], "on": aa["want"]}))
                ws.send(json.dumps({"type": "gamemap", "on": player.get("gamemap", False)}))
                ws.send(json.dumps({"type": "look", **look}))
                if ws.window:
                    set_active_window()
                if game.get("at"):
                    ws.send(json.dumps({"type": "game", **game}))
                while (m := ws.recv()) is not None:
                    try:
                        if isinstance(m, bytes):
                            if m[:1] == b"R":
                                remote_screen(m)
                            continue
                        on_ws_message(json.loads(m), ws)
                    except Exception:
                        pass
            except Exception:
                pass
            clients.discard(ws)
            if ws.window:
                if ws in windows:
                    windows.remove(ws)
                set_active_window()
                threading.Thread(target=window_gone, daemon=True).start()
            return

        if u.path in ("/icon.svg", "/favicon.ico"):
            b = open(os.path.join(HERE, "beamdeck-icon.svg"), "rb").read()
            self.send_response(200)
            self.send_header("Content-Type", "image/svg+xml")
            self.send_header("Content-Length", str(len(b)))
            self.end_headers()
            return self.wfile.write(b)
        if u.path in ("/", "/index.html"):
            b = open(PAGE, "rb").read()
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(b)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            return self.wfile.write(b)
        if u.path == "/stream":
            src = q.get("src", "").strip()
            if not src or os.path.exists(os.path.expanduser(src)):
                return self.json({"fallback": True})
            try:
                sid, st = resolve_stream(src)
            except Exception as e:
                return self.json({"fallback": True, "why": str(e)[:200]})
            if not sid:
                return self.json({"fallback": True})
            ensure_screen_file(sid, src, True)
            return self.json({"key": sid, "title": st["title"],
                              "audio": f"/p/{sid}/a" if st["a"] else f"/p/{sid}/v"})
        if u.path == "/fetch":
            src = q.get("src", "").strip()
            if not src:
                return self.json({"error": "no src"}, 400)
            i = key_of(src)
            if job_status(i)["state"] in ("unknown", "error"):
                set_job(i, state="queued", progress=0, error="")
                threading.Thread(target=download_worker, args=(i, src), daemon=True).start()
            return self.json({"key": i})
        if u.path == "/status":
            i = q.get("id", "")
            st = job_status(i)
            if st["state"] == "ready":
                ensure_screen_file(i, None, False)
            return self.json(st)
        if u.path == "/screenstatus":
            return self.json({"state": screen_jobs.get(q.get("key", ""), "none")})
        if u.path == "/list":
            out = []
            for f in sorted((f for f in os.listdir(META) if f.endswith(".json")),
                            key=lambda f: -os.path.getmtime(os.path.join(META, f)))[:40]:
                try:
                    d = dict(json.load(open(os.path.join(META, f))), key=f[:-5])
                    if not d.get("stream") and not os.path.exists(out_path(d["key"])):
                        continue  # downloaded file was deleted
                    out.append(d)
                except Exception:
                    pass
            return self.json(out[:30])
        if u.path == "/debug":
            return self.json({"aa": {k: v for k, v in aa.items() if k not in ("procs",)}, "aa_log": aa_log, "taps": taps,
                              "map_pane": map_pane["where"], "gamemap": player.get("gamemap", False),
                              "bass_in": player.get("bass"), "car_shake": game.get("shake"), "mp": game.get("mp"), "tv": game.get("tv"), "car": {k: game.get(k) for k in ("hasCar", "inside", "dist", "vehName")}, "mp_window": player.get("mpdebug"),
                              "clients": [{"origin": getattr(c, "origin", ""), "agent": getattr(c, "agent", ""),
                                           "frames": c.frames, "busy": c.busy,
                                           "age": round(time.time() - getattr(c, "since", time.time()))}
                                          for c in list(clients)]})
        if u.path == "/np":
            d = {k: np.get(k) for k in ("title", "artist", "album", "dur", "source")}
            st = np_state() or {}
            d.update(pos=st.get("p", 0), paused=st.get("z", True),
                     cover_url=("/np/cover?k=" + key_of(np["cover"])) if np.get("cover") else "")
            return self.json(d)
        if u.path == "/np/cover":
            return self.image(cover_bytes(np.get("cover", "")))
        if u.path == "/apps":
            return self.json({"apps": list_apps(), "selected": appcap["key"]})
        if u.path == "/config":
            return self.json(config_public())
        if u.path.startswith("/lib/"):
            if m := re.fullmatch(r"/lib/file/([0-9a-f]{12})", u.path):
                t = lib["tracks"].get(m.group(1))
                return self.serve(t["path"], audio_type(t["path"])) if t else self.json({"error": "not found"}, 404)
            if m := re.fullmatch(r"/lib/cover/([0-9a-f]{12})", u.path):
                return self.image(lib_cover(m.group(1)))
            r = lib_query(u.path, q)
            return self.json(r) if r is not None else self.json({"error": "not found"}, 404)
        if u.path.startswith("/nd/"):
            try:
                if m := re.fullmatch(r"/nd/stream/([\w-]+)", u.path):
                    return self.pass_through(nd_url("stream", id=m.group(1), format="raw"))
                if m := re.fullmatch(r"/nd/cover/([\w-]+)", u.path):
                    return self.image(nd_raw("getCoverArt", id=m.group(1), size=300)[0])
                r = nd_query(u.path, q)
                return self.json(r) if r is not None else self.json({"error": "not found"}, 404)
            except Exception as e:
                return self.json({"error": str(e)[:200]}, 502)
        if u.path == "/files":
            return self.json({"saved": folder_size(CACHE), "screens": folder_size(MEDIA), "path": CACHE})
        if u.path == "/openfolder":
            open_folder(MEDIA if q.get("which") == "screens" else CACHE)
            return self.json({"ok": True})
        if m := re.fullmatch(r"/p/([0-9a-f]{12})/([va])", u.path):
            return self.proxy(m.group(1), m.group(2))
        if m := re.fullmatch(r"/v/([0-9a-f]{12})\.webm", u.path):
            return self.serve(out_path(m.group(1)))
        if m := re.fullmatch(r"/m/([0-9a-f]{12})\.webm", u.path):
            return self.serve(media_path(m.group(1)))
        self.json({"error": "not found"}, 404)

    def do_POST(self):
        u = urllib.parse.urlparse(self.path)
        n = int(self.headers.get("Content-Length", 0) or 0)
        try:
            body = json.loads(self.rfile.read(n) or b"{}")
        except Exception:
            return self.json({"error": "bad json"}, 400)
        if u.path == "/config":
            return self.json(config_update(body))
        self.json({"error": "not found"}, 404)

    def image(self, data):
        if not data:
            return self.json({"error": "no image"}, 404)
        self.send_response(200)
        self.send_header("Content-Type", "image/png" if data[:4] == b"\x89PNG" else "image/jpeg")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "max-age=86400")
        self.end_headers()
        self.wfile.write(data)

    def pass_through(self, url):
        """Streams a remote file (Navidrome) to the window, forwarding Range so seeking works."""
        h = {"Range": self.headers["Range"]} if self.headers.get("Range") else {}
        try:
            r = urllib.request.urlopen(urllib.request.Request(url, headers=h), timeout=30)
        except urllib.error.HTTPError as e:
            r = e
        self.send_response(r.status if hasattr(r, "status") else r.code)
        for k in ("Content-Type", "Content-Length", "Content-Range", "Accept-Ranges"):
            if r.headers.get(k):
                self.send_header(k, r.headers[k])
        self.end_headers()
        try:
            while b := r.read(1 << 16):
                self.wfile.write(b)
        except Exception:
            self.close_connection = True

    def ranged_headers(self, size, ctype):
        start, end = 0, size - 1
        rng = self.headers.get("Range")
        if rng and (m := re.match(r"bytes=(\d*)-(\d*)", rng)):
            if m.group(1):
                start = int(m.group(1))
                if m.group(2):
                    end = min(int(m.group(2)), size - 1)
            elif m.group(2):
                start = max(0, size - int(m.group(2)))
        self.send_response(206 if rng else 200)
        if rng:
            self.send_header("Content-Range", f"bytes {start}-{end}/{size}")
        self.send_header("Content-Type", ctype)
        self.send_header("Accept-Ranges", "bytes")
        self.send_header("Content-Length", str(end - start + 1))
        self.end_headers()
        return start, end

    def proxy(self, sid, kind):
        st = streams.get(sid)
        if not st:
            try:
                st = resolve_stream(json.load(open(meta_path(sid)))["src"])[1]
            except Exception:
                st = None
        part = st and (st["v"] if kind == "v" else st["a"])
        if not part:
            return self.json({"error": "unknown stream"}, 404)
        try:
            size = upstream_size(part)
        except Exception as e:
            streams.pop(sid, None)
            return self.json({"error": str(e)[:200]}, 502)
        start, end = self.ranged_headers(size, "video/webm" if kind == "v" else "audio/webm")
        pos = start
        try:
            while pos <= end:
                stop = min(end, pos + CHUNK - 1)
                with upstream(part, pos, stop) as r:
                    while b := r.read(1 << 16):
                        self.wfile.write(b)
                        pos += len(b)
                if pos <= stop:
                    break
        except Exception:
            self.close_connection = True

    def serve(self, path, ctype="video/webm"):
        if not os.path.exists(path):
            return self.json({"error": "not ready"}, 404)
        start, end = self.ranged_headers(os.path.getsize(path), ctype)
        try:
            with open(path, "rb") as f:
                f.seek(start)
                left = end - start + 1
                while left > 0 and (chunk := f.read(min(1 << 16, left))):
                    self.wfile.write(chunk)
                    left -= len(chunk)
        except Exception:
            self.close_connection = True


if __name__ == "__main__":
    migrate_old_files()
    load_session()
    threading.Thread(target=udp_rx, daemon=True).start()
    threading.Thread(target=udp_tx, daemon=True).start()
    threading.Thread(target=aa_run, daemon=True).start()
    threading.Thread(target=frame_server, daemon=True).start()
    threading.Thread(target=frame_reaper, daemon=True).start()
    threading.Thread(target=map_watch, daemon=True).start()
    threading.Thread(target=appcap_loop, daemon=True).start()
    threading.Thread(target=np_loop, daemon=True).start()
    if config["music_folders"]:
        threading.Thread(target=lib_scan, daemon=True).start()
    ThreadingHTTPServer.daemon_threads = True
    ThreadingHTTPServer((HOST, PORT), H).serve_forever()
