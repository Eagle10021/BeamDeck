"""Talks to the game's head tracking extension (the mod's beamdeckHead.lua, UDP 127.0.0.1:47152) for tests and for tuning.
   python3 tools/head_probe.py               print what the camera is doing
   python3 tools/head_probe.py yaw=30 ...    send one pose, wait a moment, print the camera
   python3 tools/head_probe.py cam=driver    switch the camera by name"""
import json, math, socket, sys, time

ADDR = ("127.0.0.1", 47152)


def ask(msg, wait=0.6):
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    s.settimeout(wait)
    try:
        s.sendto(json.dumps(msg).encode(), ADDR)
        if msg.get("type") != "probe":
            return None
        return json.loads(s.recvfrom(8192)[0])
    except (OSError, ValueError):
        return None
    finally:
        s.close()


def send_pose(**pose):
    ask({"type": "head", "on": True, **pose})


def probe():
    return ask({"type": "probe"})


def hold(seconds=0.5, **pose):
    """Sends a pose at 30 a second for a while (as the page does), then asks the camera."""
    end = time.time() + seconds
    while time.time() < end:
        send_pose(**pose)
        time.sleep(1 / 30)
    return probe()


def angle_between(a, b):
    dot = max(-1.0, min(1.0, sum(x * y for x, y in zip(a, b))))
    return math.degrees(math.acos(dot))


def yaw_pitch_of(fwd, up):
    """The camera's yaw and pitch relative to straight up: yaw from the horizontal part of the direction, pitch from its height."""
    return math.degrees(math.atan2(fwd[0], fwd[1])), math.degrees(math.asin(max(-1.0, min(1.0, fwd[2]))))


if __name__ == "__main__":
    pose = {}
    for a in sys.argv[1:]:
        k, _, v = a.partition("=")
        if k == "cam":
            ask({"type": "cam", "name": v}); time.sleep(0.5)
        else:
            pose[k] = float(v)
    r = hold(0.6, **pose) if pose else probe()
    print(json.dumps(r, indent=1) if r else "no answer: is BeamNG running with the BeamDeck mod?")
