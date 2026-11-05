#!/usr/bin/env python3
"""BeamDeck launcher (Linux and Windows): starts the helper if needed, then opens the BeamDeck window as an
app window in a Chromium-based browser (Edge, Chrome, Chromium, Brave, Vivaldi), with its own profile."""
import json, os, shutil, subprocess, sys, time, urllib.request

HERE = os.path.dirname(os.path.realpath(__file__))
PORT = int(os.environ.get("BEAMDECK_PORT", "47130"))
WINDOWS = os.name == "nt"
if WINDOWS:
    CONFIG = os.path.join(os.environ.get("APPDATA", os.path.expanduser("~")), "BeamDeck", "config.json")
    DATA = os.path.join(os.environ.get("LOCALAPPDATA", os.path.expanduser("~")), "BeamDeck")
else:
    CONFIG = os.path.join(os.environ.get("XDG_CONFIG_HOME") or os.path.expanduser("~/.config"), "beamdeck", "config.json")
    DATA = os.path.join(os.environ.get("XDG_DATA_HOME") or os.path.expanduser("~/.local/share"), "beamdeck")
os.makedirs(DATA, exist_ok=True)


def helper_up():
    try:
        urllib.request.urlopen(f"http://127.0.0.1:{PORT}/", timeout=1)
        return True
    except Exception:
        return False


def start_helper():
    server = os.path.join(HERE, "beamdeck-server.py")
    if WINDOWS:
        pyw = os.path.join(os.path.dirname(sys.executable), "pythonw.exe")
        log = open(os.path.join(DATA, "helper.log"), "w")       # pythonw has no console: keep its errors
        subprocess.Popen([pyw if os.path.exists(pyw) else sys.executable, server],
                         creationflags=0x00000008 | 0x00000200,   # detached, new process group: survives this window
                         stdout=log, stderr=subprocess.STDOUT)
    else:
        if subprocess.run(["systemctl", "--user", "start", "beamdeck"], capture_output=True).returncode != 0:
            subprocess.Popen([sys.executable, server], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                             start_new_session=True)
    for _ in range(50):
        if helper_up():
            return True
        time.sleep(0.2)
    return False


def find_browser():
    try:
        b = json.load(open(CONFIG)).get("browser", "")
        if b:
            return b
    except Exception:
        pass
    if WINDOWS:
        roots = [os.environ.get(v, "") for v in ("PROGRAMFILES(X86)", "PROGRAMFILES", "LOCALAPPDATA")]
        for rel in (r"Microsoft\Edge\Application\msedge.exe", r"Google\Chrome\Application\chrome.exe",
                    r"BraveSoftware\Brave-Browser\Application\brave.exe", r"Vivaldi\Application\vivaldi.exe"):
            for r in roots:
                if r and os.path.exists(os.path.join(r, rel)):
                    return os.path.join(r, rel)
        return None
    for b in ("microsoft-edge-stable", "microsoft-edge", "google-chrome-stable", "google-chrome", "chromium",
              "chromium-browser", "brave", "brave-browser", "vivaldi-stable"):
        if shutil.which(b):
            return b
    return None


def fail(msg):
    print("BeamDeck: " + msg, file=sys.stderr)
    if WINDOWS:                                    # started by pythonw from the Start menu: nobody sees stderr
        import ctypes
        ctypes.windll.user32.MessageBoxW(None, msg, "BeamDeck", 0x10)
    sys.exit(1)


def raise_window(wait=0):
    """Windows: brings the BeamDeck window to the front, waiting up to `wait` s for it to appear. True if found."""
    import ctypes
    u = ctypes.windll.user32
    end = time.time() + wait
    while True:
        h = u.FindWindowW("Chrome_WidgetWin_1", "BeamDeck")  # the --app window (Edge / Chrome) titled by the page
        if h or time.time() >= end:
            break
        time.sleep(0.2)
    if not h:
        return False
    u.ShowWindow(h, 9)                                       # SW_RESTORE, in case it's minimised
    if not u.SetForegroundWindow(h) or u.GetForegroundWindow() != h:
        # Windows' foreground lock: a synthetic Alt press counts as input to this process, then the switch is allowed
        u.keybd_event(0x12, 0, 0, 0)                         # VK_MENU down
        u.keybd_event(0x12, 0, 2, 0)                         # KEYEVENTF_KEYUP
        u.SetForegroundWindow(h)
    return True


def main():
    if WINDOWS and helper_up() and raise_window():
        return
    if not helper_up() and not start_helper():
        log = os.path.join(DATA, "helper.log") if WINDOWS else "journalctl --user -u beamdeck"
        fail(f"The helper didn't start (is ffmpeg installed? see the docs). Its output: {log}")
    browser = find_browser()
    if not browser:
        fail(f'No Chromium-based browser found (set "browser" in {CONFIG})')
    if WINDOWS:
        # the launcher was started from the Start menu and may take the foreground; an already running browser that
        # opens the window may not, so its window would open behind everything else unless that right is passed on
        import ctypes
        ctypes.windll.user32.AllowSetForegroundWindow(-1)    # ASFW_ANY
    p = subprocess.Popen([browser, f"--app=http://127.0.0.1:{PORT}/", "--user-data-dir=" + os.path.join(DATA, "browser-profile"),
                          "--window-size=540,860", "--class=BeamDeck", "--no-first-run", "--no-default-browser-check",
                          # its own profile: let the car sound start without a click first (otherwise Chromium keeps the
                          # audio paused, and the music shared with other players is silence until a click)
                          "--autoplay-policy=no-user-gesture-required"],
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    open(os.path.join(DATA, "window.pid"), "w").write(str(p.pid))
    if WINDOWS:
        raise_window(wait=15)    # a running Edge opens it behind whatever has focus; bring it forward ourselves


if __name__ == "__main__":
    if WINDOWS:                  # pythonw shows nothing when a script crashes: log every start, show any error
        import traceback
        with open(os.path.join(DATA, "launcher.log"), "a") as log:
            log.write(time.strftime("%Y-%m-%d %H:%M:%S") + f" start {sys.executable} helper_up={helper_up()}\n")
        try:
            main()
        except SystemExit:
            raise
        except Exception:
            with open(os.path.join(DATA, "launcher.log"), "a") as log:
                traceback.print_exc(file=log)
            fail("The launcher failed:\n" + traceback.format_exc()[-800:])
    else:
        main()
