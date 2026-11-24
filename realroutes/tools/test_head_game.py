"""Head tracking in the real game: start BeamNG with the BeamDeck mod in any level with a vehicle, then
   python3 tools/test_head_game.py                 the views head tracking must leave alone (chase and orbit)
   python3 tools/test_head_game.py --first-person  ... and then wait for the first-person view (press 2 or C in the game) and check
                                                   every movement there: the only view head tracking turns
Sends known head poses and checks what the camera does."""
import math, os, sys, time
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import head_probe as H

bad = []
def ok(c, m):
    print(("PASS " if c else "FAIL ") + m)
    if not c: bad.append(m)

def cross(a, b):
    return [a[1] * b[2] - a[2] * b[1], a[2] * b[0] - a[0] * b[2], a[0] * b[1] - a[1] * b[0]]

def dot(a, b): return sum(x * y for x, y in zip(a, b))

def horizon_roll(f, u):
    """How far the camera is rolled from a level horizon: + is clockwise, seen from behind."""
    right = cross(f, [0, 0, 1])
    n = math.sqrt(dot(right, right)) or 1.0
    return math.degrees(math.asin(max(-1.0, min(1.0, dot(u, [c / n for c in right])))))

def turn_between(base, now):
    """What a person sees between two probes: yaw (the heading, right is +), pitch (the height of the view, up is +) and roll (the
    horizon, clockwise is +), in degrees."""
    heading = lambda f: math.degrees(math.atan2(f[0], f[1]))
    yaw = (heading(now["fwd"]) - heading(base["fwd"]) + 180) % 360 - 180
    pitch = math.degrees(math.asin(now["fwd"][2])) - math.degrees(math.asin(base["fwd"][2]))
    roll = horizon_roll(now["fwd"], now["up"]) - horizon_roll(base["fwd"], base["up"])
    return yaw, pitch, roll

def settle():
    H.send_pose(); time.sleep(1.8)                                                       # no packets: it eases back to the centre

def leave_alone(cam):
    print(f"\n--- {cam}: head tracking must do nothing outside the first-person view")
    H.ask({"type": "cam", "name": cam}); time.sleep(1.5); settle()
    base = H.probe()
    ok(base and base.get("cam") == cam and base.get("firstPerson") is False, f"the camera is {cam}, which is not a first-person view")
    if not base or base.get("cam") != cam:
        return
    now = H.hold(0.8, yaw=40, pitch=25, roll=20, x=15, y=8, z=12)
    y, p, r = turn_between(base, now)
    d = [a - b for a, b in zip(now["pos"], base["pos"])]
    ok(abs(y) < 0.5 and abs(p) < 0.5 and abs(r) < 0.5 and math.sqrt(dot(d, d)) < 0.02,
       f"turning, nodding, tilting and leaning moved the view by {y:+.2f}, {p:+.2f}, {r:+.2f} degrees and {math.sqrt(dot(d, d)) * 100:.1f} cm")
    settle()

def first_person(cam):
    print(f"\n--- {cam}: the first-person view")
    settle()
    base = H.probe()
    ok(base.get("firstPerson") is True, f"{cam} counts as a first-person view")
    ok(abs(base["cur"]["yaw"]) < 0.2, "the centre is the centre before any packet")
    for name, pose, expect in (("turning right 30", {"yaw": 30}, (30, 0, 0)), ("turning left 30", {"yaw": -30}, (-30, 0, 0)),
                               ("looking up 20", {"pitch": 20}, (0, 20, 0)), ("looking down 15", {"pitch": -15}, (0, -15, 0)),
                               ("tilting right 15", {"roll": 15}, (0, 0, 15))):
        now = H.hold(0.8, **pose)
        y, p, r = turn_between(base, now)
        ok(abs(y - expect[0]) < 2.0 and abs(p - expect[1]) < 2.0 and abs(r - expect[2]) < 2.0,
           f"{name}: the view moved yaw {y:+.1f}, pitch {p:+.1f}, roll {r:+.1f} (wanted {expect[0]:+d}, {expect[1]:+d}, {expect[2]:+d})")
    now = H.hold(0.8, x=10)
    right0 = cross(base["fwd"], base["up"]); d = [a - b for a, b in zip(now["pos"], base["pos"])]
    ok(abs(dot(d, right0) - 0.10) < 0.02, f"leaning right 10 cm: the camera moved {dot(d, right0) * 100:+.1f} cm to the right")
    now = H.hold(0.8, z=10)
    d = [a - b for a, b in zip(now["pos"], base["pos"])]
    ok(abs(dot(d, base["fwd"]) - 0.10) < 0.02, f"leaning 10 cm toward the screen: the camera moved {dot(d, base['fwd']) * 100:+.1f} cm forward")
    now = H.hold(0.8, yaw=30, pitch=10)
    y, p, r = turn_between(base, now)
    ok(abs(r) < 1.0, f"turning and looking up together keeps the horizon level (roll {r:+.2f})")
    settle()
    after = H.probe()
    y, p, r = turn_between(base, after)
    ok(abs(y) < 0.7 and abs(p) < 0.7 and abs(r) < 0.7, f"with no packets the view eases back to the centre ({y:+.2f}, {p:+.2f}, {r:+.2f})")
    ok(H.ask({"type": "head", "on": False}) is None, "the off switch is accepted")
    big = H.hold(0.6, yaw=500, pitch=500)
    ok(abs(big["target"]["yaw"]) <= 180 and abs(big["target"]["pitch"]) <= 90, "absurd values are clamped")
    H.ask({"type": "head", "on": True}); settle()

if not H.probe():
    print("no answer: is BeamNG running with the BeamDeck mod?"); sys.exit(1)
for cam in ("orbit", "chase"):
    leave_alone(cam)

for a in sys.argv[1:]:
    if a.startswith("--cam="):                                         # run the first-person checks on a named camera, no waiting
        H.ask({"type": "cam", "name": a[6:]}); time.sleep(1.5)
        r = H.probe()
        ok(r and r.get("cam") == a[6:], f"the game is on the {a[6:]} camera ({r and r.get('cam')})")
        if r and r.get("cam") == a[6:]:
            first_person(r["cam"])

if "--first-person" in sys.argv:
    print("\nWaiting for the first-person view: press 2 or C in BeamNG (up to 10 minutes)...")
    end, shown = time.time() + 600, 0
    while time.time() < end:
        r = H.probe()
        if r and r.get("firstPerson"):
            first_person(r["cam"]); break
        if time.time() - shown > 15:
            print(f"   the game is on the '{r and r.get('cam')}' camera"); shown = time.time()
        time.sleep(0.7)
    else:
        ok(False, "the game was switched to a first-person view")

print("\nall head tracking game tests passed" if not bad else f"\n{len(bad)} FAILED")
sys.exit(1 if bad else 0)
