"""Freight multiplayer: this player's side of the BeamDeck freight hub (relay/freight-hub.py).

A background thread tells the hub where this player is and what job they are on (about every two seconds) and keeps a
copy of what comes back: the other drivers, the shared job board, convoys, chat and the leaderboard. Freight reads
that copy (the board, the convoy bonus) and the console shows it; nothing here touches the game.

Everything is switched by the "mpOn" preference; with it off the thread idles and no network call is made.
"""
import json, os, re, threading, time, urllib.error, urllib.request, uuid

POLL_S = 2.0
HTTP_S = 4.0
BOARD_TARGET = 14           # this player tops the shared board up to this many jobs
PUSH_EVERY_S = 25.0
STALE_S = 10.0              # no answer for this long: not connected
CHAT_KEEP = 60
DEFAULT_PORT = 47141


def normalize_hub(text):
    """'100.64.0.1', 'http://host:47141/', 'ws://host:47140' (the music relay) -> 'http://host:47141'."""
    t = (text or "").strip()
    if not t:
        return ""
    t = re.sub(r"^[a-z]+://", "", t, flags=re.I).split("/")[0]
    host, _, port = t.partition(":")
    if port == "47140" or not port:
        port = str(DEFAULT_PORT)                 # the relay's port: the hub runs next to it
    return f"http://{host}:{port}" if host else ""


class MP:
    def __init__(self, freight, prefs, data_dir, relay_hint=None):
        self.fr = freight
        self.prefs = prefs                       # a callable returning the current preferences dict
        self.relay_hint = relay_hint or (lambda: "")   # the music relay address from BeamDeck's own config, if any
        self.lock = threading.Lock()
        self.snap = None
        self.last_ok = 0.0
        self.error = ""
        self.chat = []
        self.chat_id = 0
        self.last_push = 0.0
        self.shared_ids = frozenset()
        self.name_seen = ""
        idf = os.path.join(data_dir, "mp_id")
        try:
            self.pid = open(idf).read().strip()
            assert re.fullmatch(r"[0-9a-f\-]{32,40}", self.pid)
        except Exception:
            self.pid = uuid.uuid4().hex
            try:
                os.makedirs(data_dir, exist_ok=True)
                open(idf, "w").write(self.pid)
            except OSError:
                pass
        self._stop = threading.Event()
        self._thread = None

    # ------------------------------------------------------------------ settings
    def enabled(self):
        return bool(self.prefs().get("mpOn"))

    def hub(self):
        return normalize_hub(self.prefs().get("mpHub")) or normalize_hub(self.relay_hint())

    def my_name(self):
        n = str(self.prefs().get("mpName") or "").strip()
        return n or getattr(self.fr, "player", "") or os.environ.get("USERNAME") or os.environ.get("USER") or "Driver"

    def active(self):
        return self.enabled() and self.snap is not None and time.time() - self.last_ok < STALE_S

    # ------------------------------------------------------------------ talking to the hub
    def _post(self, path, body):
        url = self.hub()
        if not url:
            raise OSError("No hub address: set it in the Multiplayer tab")
        req = urllib.request.Request(url + path, data=json.dumps(body).encode(), headers={"Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=HTTP_S) as r:
                return json.loads(r.read())
        except urllib.error.HTTPError as e:
            try:
                msg = json.loads(e.read()).get("error")
            except Exception:
                msg = None
            raise OSError(msg or f"hub answered {e.code}")
        except (urllib.error.URLError, TimeoutError, ConnectionError) as e:
            raise OSError("The hub isn't reachable (is Tailscale on?)") from e

    def sync(self):
        """One presence round: send where this player is, take in the shared state."""
        st = self.fr.mp_state()
        body = {"pid": self.pid, "name": self.my_name(), "since": self.chat_id, "ver": str(getattr(self.fr, "app_ver", ""))[:16]} | st
        snap = self._post("/api/presence", body)
        with self.lock:
            self.snap = snap
            self.last_ok = time.time()
            self.error = ""
            if snap.get("chat_id", 0) < self.chat_id:        # the hub restarted: start the chat afresh
                self.chat = []
            self.chat = (self.chat + [m for m in snap.get("chat", []) if m["id"] > self.chat_id])[-CHAT_KEEP:]
            self.chat_id = snap.get("chat_id", self.chat_id)
            ids = frozenset(o["id"] for o in snap.get("offers", []))
            if ids != self.shared_ids:
                self.shared_ids = ids
                self.fr.v += 1                               # the console refetches the board
        self._top_up(snap)

    def _top_up(self, snap):
        """Adds fresh random jobs to the shared board when it runs low, so there is always something to take."""
        if len(snap.get("offers", [])) >= BOARD_TARGET or time.time() - self.last_push < PUSH_EVERY_S:
            return
        self.last_push = time.time()
        try:
            offers = self.fr.make_shared(4)
        except Exception:                                    # a bug in making jobs must not stop multiplayer, and shows up in the log
            import traceback
            traceback.print_exc()
            self.last_push = time.time() - PUSH_EVERY_S + 6      # try again in a few seconds
            return
        if not offers:
            self.last_push = time.time() - PUSH_EVERY_S + 6
            return
        try:
            self._post("/api/offers", {"pid": self.pid, "offers": offers})
        except OSError:
            self.last_push = time.time() - PUSH_EVERY_S + 6

    def _loop(self):
        wait = POLL_S
        while not self._stop.is_set():
            if not self.enabled():
                with self.lock:
                    self.snap = None
                self._stop.wait(1.0)
                continue
            try:
                self.sync()
                wait = POLL_S
            except OSError as e:
                with self.lock:
                    self.error = str(e)
                wait = min(10.0, wait * 1.5)
            except Exception as e:                           # a bug here must not kill the thread
                with self.lock:
                    self.error = f"multiplayer error: {e}"
                wait = 10.0
            self._stop.wait(wait)

    def start(self):
        if self._thread is None:
            self._thread = threading.Thread(target=self._loop, daemon=True, name="freight-mp")
            self._thread.start()

    def stop(self):
        self._stop.set()

    # ------------------------------------------------------------------ what Freight and the console read
    def offers(self):
        if not self.active():
            return []
        return [dict(o) for o in (self.snap or {}).get("offers", [])]

    def offer(self, oid):
        return next((o for o in self.offers() if o["id"] == oid), None)

    def convoy_code(self):
        return (self.snap or {}).get("convoy", "") if self.active() else ""

    def convoy_size_on(self, offer_id):
        """How many drivers of this player's convoy (this player included) are on the given job right now."""
        if not self.active():
            return 0
        code = self.convoy_code()
        if not code:
            return 0
        n = 0
        for p in self.snap.get("players", []):
            if p.get("convoy") == code and (p.get("job") or {}).get("offer") == offer_id:
                n += 1
        return n

    def view(self):
        if not self.enabled():
            return {"on": False, "connected": False, "hub": self.hub(), "name": self.my_name(), "error": ""}
        s = self.snap or {}
        return {"on": True, "connected": self.active(), "error": self.error, "hub": self.hub(), "name": s.get("name") or self.my_name(),
                "players": s.get("players", []), "convoys": s.get("convoys", []), "convoy": s.get("convoy", ""),
                "chat": list(self.chat), "shared": len(s.get("offers", []))}

    # ------------------------------------------------------------------ actions (each returns (ok, message_or_data))
    def _act(self, path, body):
        if not self.enabled():
            return False, "Turn multiplayer on first"
        try:
            r = self._post(path, {"pid": self.pid} | body)
        except OSError as e:
            return False, str(e)
        return bool(r.get("ok")), r

    def _refresh(self):
        """Take the hub's answer in now rather than at the next poll, so the page shows a convoy change at once."""
        try:
            self.sync()
        except OSError:
            pass

    def convoy_create(self, offer):
        ok, r = self._act("/api/convoy", {"action": "create", "offer": offer})
        if ok:
            self._refresh()
        return (True, r) if ok else (False, r if isinstance(r, str) else r.get("error", "failed"))

    def convoy_join(self, code):
        ok, r = self._act("/api/convoy", {"action": "join", "code": code})
        if ok:
            self._refresh()
        return (True, r) if ok else (False, r if isinstance(r, str) else r.get("error", "failed"))

    def convoy_leave(self):
        ok, r = self._act("/api/convoy", {"action": "leave"})
        if ok:
            self._refresh()
        return ok, ("" if ok else r)

    def share(self, offer):
        """Puts a job this player is taking on the shared board (in the background), so friends can take the same one."""
        if not self.enabled() or offer.get("shared"):
            return
        self._background("/api/offers", {"pid": self.pid, "offers": [offer], "force": True})

    def say(self, text, convoy=False):
        ok, r = self._act("/api/chat", {"text": text, "convoy": bool(convoy)})
        return ok, ("" if ok else (r if isinstance(r, str) else r.get("error", "failed")))

    def done(self, pay, km, stars, title, convoy_n):
        """Reports a finished job in the background (it is called while Freight holds the helper's lock, and a hub that
        is slow or down must not hold up the delivery). flush() waits for it."""
        if not self.enabled():
            return
        self._background("/api/done", {"pid": self.pid, "pay": pay, "km": km, "stars": stars, "title": title, "convoy_n": convoy_n})

    def _background(self, path, body):
        """A call that must not hold anything up (Freight makes it while it holds the helper's lock)."""
        def run():
            try:
                self._post(path, body)
            except OSError:
                pass
        t = threading.Thread(target=run, daemon=True, name="freight-mp-bg")
        self._pending = [x for x in getattr(self, "_pending", []) if x.is_alive()] + [t]
        t.start()

    def flush(self, timeout=6.0):
        for t in list(getattr(self, "_pending", [])):
            t.join(timeout)
