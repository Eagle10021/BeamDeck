#!/usr/bin/env python3
"""BeamDeck multiplayer relay: passes each player's car music (WebM/Opus from MediaRecorder) to everyone else.

  ws /pub?player=NAME   a player's BeamDeck sends its music; the first message is the stream header
  ws /pub?player=NAME&kind=screen   what's on a player's car screen: binary messages [type][payload] with
                        type F (a JPEG frame), N (JSON: mode, now playing, map pane) or C (cover JPEG)
  ws /sub               a player's BeamDeck receives everyone's music and screens: binary messages are
                        [kind: M music | S screen][1 byte name length][name, UTF-8][data]; the header of every
                        music stream and the latest N / C / F of every screen are sent first, so late joiners
                        can start straight away
  ws /pub?player=NAME&kind=voice   proximity voice chat: binary messages are Opus packets (20 ms each), passed
                        straight on as kind V; nothing is kept (late joiners just hear what's said next)
                        a listener that still can't play a stream after resyncs sends {"type":"restart","player"}:
                        the relay asks that player's BeamDeck for a fresh stream (a new generation for everyone)
  GET /players          who is sharing right now, with each stream's generation and age

Staying in sync: each new stream from a player (first connect, reconnect, BeamDeck restart) gets a new
generation number, announced as {"type":"start","player","gen"} before its header, so listeners rebuild that
player cleanly; a listener whose player broke sends {"type":"resync","player"} and gets the header again, then
the live data. Everything is live (no backlog), so a frozen listener rejoins at the current point.

Run it where all players can reach it (e.g. a server on the same Tailscale network as the BeamMP server).
Only Python's standard library is needed.
"""
import base64, hashlib, json, os, struct, threading, time, urllib.parse
from http.server import ThreadingHTTPServer, BaseHTTPRequestHandler

HOST = os.environ.get("BEAMDECK_RELAY_HOST", "0.0.0.0")
PORT = int(os.environ.get("BEAMDECK_RELAY_PORT", "47140"))
WS_GUID = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"

lock = threading.Lock()
streams = {}        # name -> {"header": bytes, "last": time, "gen": int, "bytes": int}   (music)
screens = {}        # name -> {"ws": publisher, "last": time, "F"/"N"/"C": latest of each}         (car screens)
gens = {}           # name -> last generation handed out
subs = set()
voices = {}         # name -> {"ws": publisher, "last": time}                                          (voice chat)


class WS:
    def __init__(self, sock):
        self.sock, self.wlock = sock, threading.Lock()

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
        """Returns (is_text, data) or None when closed."""
        while True:
            b0, b1 = self._read(2)
            op, n = b0 & 0x0F, b1 & 0x7F
            if n == 126:
                n = struct.unpack(">H", self._read(2))[0]
            elif n == 127:
                n = struct.unpack(">Q", self._read(8))[0]
            if n > 4 << 20:
                return None
            mask = self._read(4) if b1 & 0x80 else b"\0\0\0\0"
            raw = self._read(n)
            data = bytes(x ^ mask[i % 4] for i, x in enumerate(raw))
            if op == 0x8:
                return None
            if op == 0x9:
                with self.wlock:
                    self.sock.sendall(bytes([0x8A, len(data)]) + data)
                continue
            if op in (0x1, 0x2):
                return op == 0x1, data


def framed(name, data, kind=b"M"):
    nb = name.encode()[:255]
    return kind + bytes([len(nb)]) + nb + data


def to_subs(msg, binary=True):
    for s in list(subs):
        try:
            s.send(msg, binary=binary)
        except Exception:
            subs.discard(s)


class H(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *a):
        pass

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
            try:
                if u.path == "/pub" and q.get("player") and q.get("kind") == "voice":
                    self.publish_voice(ws, q["player"][:64])
                elif u.path == "/pub" and q.get("player") and q.get("kind") == "screen":
                    self.publish_screen(ws, q["player"][:64])
                elif u.path == "/pub" and q.get("player"):
                    self.publish(ws, q["player"][:64])
                elif u.path == "/sub":
                    self.subscribe(ws)
            except Exception:
                pass
            return
        if u.path == "/players":
            now = time.time()
            body = json.dumps([{"player": n, "gen": s["gen"], "age": round(now - s["last"], 1), "bytes": s["bytes"]}
                               for n, s in streams.items()] +
                              [{"player": n, "screen": True, "age": round(now - sc["last"], 1)}
                               for n, sc in screens.items()] +
                              [{"player": n, "voice": True, "age": round(now - vc["last"], 1)}
                               for n, vc in voices.items()]).encode()
        else:
            body = b'{"beamdeck_relay": true}'
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(body)

    def publish(self, ws, name):
        first, my_gen = True, None
        try:
            while (m := ws.recv()) is not None:
                is_text, data = m
                if is_text:
                    continue
                with lock:
                    if first:                      # a new stream: new generation + its header for late joiners
                        gens[name] = my_gen = gens.get(name, 0) + 1
                        streams[name] = {"header": data, "last": time.time(), "gen": my_gen, "bytes": 0, "ws": ws}
                        start = json.dumps({"type": "start", "player": name, "gen": gens[name]})
                    elif streams.get(name, {}).get("gen") == my_gen:
                        streams[name]["last"] = time.time()
                        streams[name]["bytes"] += len(data)
                    else:
                        continue                   # a newer connection from this player took over
                if first:
                    to_subs(start, binary=False)
                    first = False
                to_subs(framed(name, data))
        finally:
            with lock:
                gone = my_gen is not None and streams.get(name, {}).get("gen") == my_gen
                if gone:                           # (a newer connection from the same player may have taken over)
                    streams.pop(name, None)
            if gone:
                to_subs(json.dumps({"type": "gone", "player": name}), binary=False)

    def publish_screen(self, ws, name):
        """A player's car screen: kept as the latest frame / info / cover, passed on to everyone."""
        with lock:
            screens[name] = {"ws": ws, "last": time.time()}      # a newer connection takes over
        try:
            while (m := ws.recv()) is not None:
                is_text, data = m
                if is_text or not data or data[:1] not in (b"F", b"N", b"C"):
                    continue
                with lock:
                    sc = screens.get(name)
                    if not sc or sc["ws"] is not ws:
                        continue
                    sc[data[:1].decode()] = data
                    sc["last"] = time.time()
                to_subs(framed(name, data, b"S"))
        finally:
            with lock:
                gone = screens.get(name, {}).get("ws") is ws
                if gone:
                    screens.pop(name, None)
            if gone:
                to_subs(json.dumps({"type": "screengone", "player": name}), binary=False)

    def publish_voice(self, ws, name):
        """A player's microphone: Opus packets, passed on as they come (only while they're talking)."""
        with lock:
            voices[name] = {"ws": ws, "last": time.time()}       # a newer connection takes over
        try:
            while (m := ws.recv()) is not None:
                is_text, data = m
                if is_text or not data or len(data) > 4000:
                    continue
                with lock:
                    vc = voices.get(name)
                    if not vc or vc["ws"] is not ws:
                        continue
                    vc["last"] = time.time()
                to_subs(framed(name, data, b"V"))
        finally:
            with lock:
                if voices.get(name, {}).get("ws") is ws:
                    voices.pop(name, None)

    def subscribe(self, ws):
        def send_start(name, s):
            ws.send(json.dumps({"type": "start", "player": name, "gen": s["gen"]}))
            ws.send(framed(name, s["header"]), binary=True)
        with lock:
            for name, s in streams.items():
                send_start(name, s)
            for name, sc in screens.items():               # what each shared screen shows right now
                for t in ("N", "C", "F"):
                    if sc.get(t):
                        ws.send(framed(name, sc[t], b"S"), binary=True)
            subs.add(ws)
        try:
            while (m := ws.recv()) is not None:
                is_text, data = m
                if not is_text:
                    continue
                try:
                    req = json.loads(data)
                except Exception:
                    continue
                if req.get("type") == "restart":   # a listener can't join this stream: ask its sender for a new one
                    with lock:
                        pub = streams.get(str(req.get("player")), {}).get("ws")
                    if pub:
                        try:
                            pub.send(json.dumps({"type": "restart"}))
                        except Exception:
                            pass
                elif req.get("type") == "resync":  # a listener's player broke: the header again, then live data
                    with lock:
                        s = streams.get(str(req.get("player")))
                        if s:
                            send_start(req["player"], s)
        finally:
            subs.discard(ws)


if __name__ == "__main__":
    ThreadingHTTPServer.daemon_threads = True
    print(f"BeamDeck relay on {HOST}:{PORT}", flush=True)
    ThreadingHTTPServer((HOST, PORT), H).serve_forever()
