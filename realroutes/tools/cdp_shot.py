"""Drive headless Edge over the DevTools protocol: load a page, wait, optionally run steps, screenshot, report JS errors.
    from cdp_shot import Browser
    with Browser(1334, 800) as b:
        b.goto(url); b.wait(4); b.shot(path); print(b.errors)
Steps can run JavaScript (b.js("...")), press and hold a point (b.press(x, y, hold)), or drag (b.drag(...)).
"""
import asyncio, atexit, base64, json, os, shutil, socket, subprocess, tempfile, time, urllib.request
import websockets


class Browser:
    def __init__(self, w=1334, h=800, port=0, camera=None, headless=True, real_gpu=False):
        """camera: None, "fake" (a made-up test picture) or "real" (the computer's webcam, allowed without asking).
        headless=False shows a window; real_gpu=True lets the browser use the graphics chip instead of the software renderer."""
        self.w, self.h = w, h
        self.camera, self.headless, self.real_gpu = camera, headless, real_gpu
        if not port:                                   # a free port, so an old browser can never be picked up by mistake
            with socket.socket() as sk:
                sk.bind(("127.0.0.1", 0)); port = sk.getsockname()[1]
        self.port = port
        self.errors, self.logs = [], []
        self._open = False

    def __enter__(self):
        self.dir = tempfile.mkdtemp()
        self._open = True
        atexit.register(self.__exit__)
        args = ["microsoft-edge-stable"]
        if self.headless:
            args.append("--headless=new")
        if not self.real_gpu:
            args += ["--disable-gpu", "--use-gl=angle", "--use-angle=swiftshader", "--enable-unsafe-swiftshader"]
        if self.camera:
            args.append("--use-fake-ui-for-media-stream")                       # the camera is allowed without a prompt
            if self.camera == "fake":
                args.append("--use-fake-device-for-media-stream")
        args += ["--hide-scrollbars", f"--window-size={self.w},{self.h}", f"--remote-debugging-port={self.port}",
                 f"--user-data-dir={self.dir}", "--no-first-run", "about:blank"]
        self.proc = subprocess.Popen(args, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        for _ in range(60):
            try:
                tabs = json.loads(urllib.request.urlopen(f"http://127.0.0.1:{self.port}/json", timeout=1).read())
                self.ws_url = next(t for t in tabs if t["type"] == "page")["webSocketDebuggerUrl"]
                break
            except Exception:
                time.sleep(0.25)
        self.loop = asyncio.new_event_loop()
        self.loop.run_until_complete(self._connect())
        return self

    def __exit__(self, *a):
        if not self._open:
            return
        self._open = False
        try:
            self.loop.run_until_complete(self.ws.close())
        except Exception:
            pass
        self.proc.terminate()
        try:
            self.proc.wait(5)
        except Exception:
            self.proc.kill()
        shutil.rmtree(self.dir, ignore_errors=True)

    async def _connect(self):
        self.ws = await websockets.connect(self.ws_url, max_size=64 * 1024 * 1024)
        self.id = 0
        for m in ("Page.enable", "Runtime.enable", "Log.enable"):
            await self._call(m)
        await self._call("Emulation.setDeviceMetricsOverride", {"width": self.w, "height": self.h, "deviceScaleFactor": 1, "mobile": False})

    async def _call(self, method, params=None, wait=True):
        self.id += 1
        mid = self.id
        await self.ws.send(json.dumps({"id": mid, "method": method, "params": params or {}}))
        while wait:
            msg = json.loads(await self.ws.recv())
            self._event(msg)
            if msg.get("id") == mid:
                return msg.get("result", {})

    def _event(self, msg):
        m = msg.get("method")
        if m == "Runtime.exceptionThrown":
            d = msg["params"]["exceptionDetails"]
            self.errors.append(f"{d.get('text')}: {d.get('exception', {}).get('description', '')[:300]} @ {d.get('url', '')}:{d.get('lineNumber')}")
        elif m == "Runtime.consoleAPICalled" and msg["params"]["type"] in ("error", "warning"):
            self.logs.append(" ".join(str(a.get("value", a.get("description", ""))) for a in msg["params"]["args"])[:300])

    def run(self, coro):
        return self.loop.run_until_complete(coro)

    def goto(self, url):
        self.run(self._call("Page.navigate", {"url": url}))

    def wait(self, secs):
        async def w():
            end = time.time() + secs
            while time.time() < end:
                try:
                    self._event(json.loads(await asyncio.wait_for(self.ws.recv(), 0.2)))
                except asyncio.TimeoutError:
                    pass
        self.run(w())

    def js(self, expr):
        r = self.run(self._call("Runtime.evaluate", {"expression": expr, "returnByValue": True, "awaitPromise": True}))
        if "exceptionDetails" in r:
            self.errors.append("eval: " + str(r["exceptionDetails"].get("exception", {}).get("description", ""))[:300])
        return r.get("result", {}).get("value")

    def shot(self, path):
        r = self.run(self._call("Page.captureScreenshot", {"format": "png"}))
        open(path, "wb").write(base64.b64decode(r["data"]))

    def mouse(self, kind, x, y, button="left"):
        return self._call("Input.dispatchMouseEvent", {"type": kind, "x": x, "y": y, "button": button, "buttons": 1 if kind != "mouseReleased" else 0, "clickCount": 1})

    def press(self, x, y, hold=0.0):
        async def go():
            await self.mouse("mouseMoved", x, y); await self.mouse("mousePressed", x, y)
            end = time.time() + hold
            while time.time() < end:
                try:
                    self._event(json.loads(await asyncio.wait_for(self.ws.recv(), 0.1)))
                except asyncio.TimeoutError:
                    pass
            await self.mouse("mouseReleased", x, y)
        self.run(go())

    def drag(self, x0, y0, x1, y1, hold=0.0, steps=8):
        async def go():
            await self.mouse("mouseMoved", x0, y0); await self.mouse("mousePressed", x0, y0)
            end = time.time() + hold
            while time.time() < end:
                try:
                    self._event(json.loads(await asyncio.wait_for(self.ws.recv(), 0.1)))
                except asyncio.TimeoutError:
                    pass
            for i in range(1, steps + 1):
                await self.mouse("mouseMoved", x0 + (x1 - x0) * i / steps, y0 + (y1 - y0) * i / steps)
                await asyncio.sleep(0.04)
            await self.mouse("mouseReleased", x1, y1)
        self.run(go())

    def center(self, selector):
        r = self.js(f"(() => {{ const e = document.querySelector({json.dumps(selector)}); if (!e) return null; const b = e.getBoundingClientRect(); return [b.left + b.width / 2, b.top + b.height / 2, b.left, b.width]; }})()")
        return r
