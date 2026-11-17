# Real Routes passengers: pose a downloaded model with bones, standing (arms down) and seated (legs bent, hands on
# the thighs). Models with a skeleton (MMD names such as "Left arm" / "Left knee") use it; models without one get a
# simple skeleton built from the body's proportions and automatic weights (any vertex left unweighted goes to its
# nearest bone, so loose pieces such as big sleeves follow the arm instead of staying behind).
# Saves <out>/<name>_standing.blend and <name>_seated.blend (meshes posed and applied, facing -Y, 1.68 m) and renders.
#   blender -b --python rig.py -- <scene.gltf> <name> <outdir>
import bpy, sys, math, os
from mathutils import Vector, Matrix
args = sys.argv[sys.argv.index("--") + 1:]
src, name, outdir = args[0], args[1], args[2]
H = 1.68
os.makedirs(outdir, exist_ok=True)

def load():
    bpy.ops.wm.read_factory_settings(use_empty=True)
    bpy.ops.import_scene.gltf(filepath=src)
    sc = bpy.context.scene
    meshes = [o for o in sc.objects if o.type == "MESH"]
    dg = bpy.context.evaluated_depsgraph_get()
    xs, ys, zs = [], [], []
    for o in meshes:                                      # size from the drawn vertices' percentiles
        ev = o.evaluated_get(dg); me = ev.to_mesh()
        for v in me.vertices:
            w = ev.matrix_world @ v.co; xs.append(w.x); ys.append(w.y); zs.append(w.z)
        ev.to_mesh_clear()
    q = lambda a, f: sorted(a)[int(f * (len(a) - 1))]
    mn = Vector((q(xs, .002), q(ys, .002), q(zs, .002))); mx = Vector((q(xs, .998), q(ys, .998), q(zs, .998)))
    s = H / (mx.z - mn.z)
    root = bpy.data.objects.new("root", None); sc.collection.objects.link(root)
    for o in [o for o in sc.objects if o.parent is None and o is not root]:
        o.parent = root
    root.scale = (s, s, s)
    root.location = (-(mn.x + mx.x) / 2 * s, -(mn.y + mx.y) / 2 * s, -mn.z * s)
    bpy.context.view_layer.update()
    return sc, meshes

def find(arm, *words, exclude=("twist", "ik", "d", "ex", "parent")):
    """A bone whose lower-case name contains all words (MMD English names), skipping helpers."""
    for b in arm.pose.bones:
        n = b.name.lower()
        base = n.split("_")[0].strip()
        if all(w in n for w in words) and not any(base.endswith(" " + e) or base.startswith(e + " ") for e in exclude) \
                and "twist" not in n and " ik" not in n:
            return b
    return None

def build_rig(meshes):
    """A skeleton from proportions, then automatic weights (+ nearest bone for anything unweighted)."""
    # bake the meshes' transforms so the rig and the meshes share one space
    bpy.ops.object.select_all(action="DESELECT")
    for o in meshes:
        mw = o.matrix_world.copy(); o.parent = None; o.matrix_world = mw; o.select_set(True)
    bpy.context.view_layer.objects.active = meshes[0]
    bpy.ops.object.transform_apply(location=True, rotation=True, scale=True)
    pts = [v.co.copy() for o in meshes for v in o.data.vertices]
    chest = [p for p in pts if 0.68 * H < p.z < 0.80 * H and abs(p.x) < 0.08 * H]
    yc = sum(p.y for p in chest) / max(1, len(chest))
    arm_data = bpy.data.armatures.new("rig"); arm = bpy.data.objects.new("rig", arm_data)
    bpy.context.scene.collection.objects.link(arm)
    bpy.context.view_layer.objects.active = arm
    bpy.ops.object.mode_set(mode="EDIT")
    eb = arm_data.edit_bones
    def bone(n, a, b, parent=None, connect=False):
        e = eb.new(n); e.head = Vector(a); e.tail = Vector(b)
        if parent: e.parent = eb[parent]; e.use_connect = connect
        return e
    Z = lambda f: f * H
    bone("hips", (0, yc, Z(.52)), (0, yc, Z(.62)))
    bone("spine", (0, yc, Z(.62)), (0, yc, Z(.74)), "hips", True)
    bone("chest", (0, yc, Z(.74)), (0, yc, Z(.82)), "spine", True)
    bone("neck", (0, yc, Z(.82)), (0, yc, Z(.88)), "chest", True)
    bone("head", (0, yc, Z(.88)), (0, yc, Z(1.0)), "neck", True)
    for side, tag in ((1, "L"), (-1, "R")):
        sh = Vector((0.105 * H * side, yc, 0.815 * H))
        # the fingertips: the outermost point a real arm's length from the shoulder (ribbons or skirt panels
        # hanging further out are too far away to be the hand)
        band = [p for p in pts if 0.45 * H < p.z < 0.82 * H and p.x * side > 0 and 0.28 * H < (p - sh).length < 0.42 * H]
        if not band:
            band = [p for p in pts if 0.45 * H < p.z < 0.82 * H and p.x * side > 0]
        tip = max(band, key=lambda p: p.x * side)
        el = sh + (tip - sh) * 0.45; wr = sh + (tip - sh) * 0.80
        bone(f"shoulder.{tag}", (0.035 * H * side, yc, 0.81 * H), sh, "chest")
        bone(f"upperarm.{tag}", sh, el, f"shoulder.{tag}", True)
        bone(f"forearm.{tag}", el, wr, f"upperarm.{tag}", True)
        bone(f"hand.{tag}", wr, tip, f"forearm.{tag}", True)
        hx = 0.058 * H * side
        bone(f"thigh.{tag}", (hx, yc, Z(.50)), (hx, yc - 0.01, Z(.285)), "hips")
        bone(f"shin.{tag}", (hx, yc - 0.01, Z(.285)), (hx * 1.05, yc, Z(.05)), f"thigh.{tag}", True)
        bone(f"foot.{tag}", (hx * 1.05, yc, Z(.05)), (hx * 1.05, yc - 0.12, Z(.01)), f"shin.{tag}", True)
    bpy.ops.object.mode_set(mode="OBJECT")
    bpy.ops.object.select_all(action="DESELECT")
    for o in meshes: o.select_set(True)
    arm.select_set(True); bpy.context.view_layer.objects.active = arm
    bpy.ops.object.parent_set(type="ARMATURE_NAME")          # empty groups; the weights are set below
    # weights by distance to the bones: each point follows its nearest bone, blended with the second nearest near a
    # joint (Blender's heat weighting fails on these multi-piece meshes). Hair goes to the head, panels hanging from
    # the waist to the hips, small loose bits to the bone nearest the point they hang from.
    segs = [(b.name, arm.matrix_world @ b.head_local, arm.matrix_world @ b.tail_local) for b in arm_data.bones
            if not b.name.startswith("shoulder")]
    def dists(p):
        out = []
        for n, a, b in segs:
            ab = b - a; t = max(0.0, min(1.0, (p - a).dot(ab) / max(1e-9, ab.length_squared)))
            out.append(((p - (a + ab * t)).length, n))
        out.sort()
        return out
    def nearest(p):
        return dists(p)[0][1]
    fixed = rigid = 0
    for o in meshes:
        me = o.data
        for n, _, _ in segs:
            if n not in o.vertex_groups: o.vertex_groups.new(name=n)
        parent = list(range(len(me.vertices)))
        def root(i):
            while parent[i] != i:
                parent[i] = parent[parent[i]]; i = parent[i]
            return i
        for e in me.edges:
            ra, rb = root(e.vertices[0]), root(e.vertices[1])
            if ra != rb: parent[ra] = rb
        groups = {}
        for v in me.vertices:
            groups.setdefault(root(v.index), []).append(v.index)
        world = [o.matrix_world @ v.co for v in me.vertices]
        for g in groups.values():
            zs = [world[i].z for i in g]
            xs = [world[i].x for i in g]; ys = [world[i].y for i in g]
            top_z, low_z = max(zs), min(zs)
            diag = math.sqrt((max(xs) - min(xs)) ** 2 + (max(ys) - min(ys)) ** 2 + (top_z - low_z) ** 2)
            # how much of the piece lies along a leg / an arm (stockings, gloves: these must bend with the limb)
            near = [dists(world[i])[0] for i in g[::max(1, len(g) // 200)]]
            on_leg = sum(1 for d, n in near if n.split(".")[0] in ("thigh", "shin", "foot") and d < 0.075 * H) / len(near)
            on_arm = sum(1 for d, n in near if n.split(".")[0] in ("upperarm", "forearm", "hand") and d < 0.08 * H) / len(near)
            limb = on_leg > 0.6 or on_arm > 0.6
            hair = not limb and top_z > 0.86 * H and low_z > 0.05 * H and max(abs(x) for x in xs) < 0.3 * H
            skirt = not limb and on_leg < 0.3 and 0.40 * H < top_z < 0.66 * H and top_z - low_z > 0.10 * H
            small = not limb and diag < 0.2 * H
            if hair or skirt or small:
                bn = "head" if hair else "hips" if skirt else nearest(world[max(g, key=lambda i: world[i].z)])
                o.vertex_groups[bn].add(g, 1.0, "REPLACE"); rigid += len(g)
                continue
            for i in g:
                d = dists(world[i])
                (d1, n1), (d2, n2) = d[0], d[1]
                w1 = 1 / (d1 ** 4 + 1e-6); w2 = 1 / (d2 ** 4 + 1e-6) if d2 < 2.0 * d1 + 0.02 else 0.0
                o.vertex_groups[n1].add([i], w1 / (w1 + w2), "REPLACE")
                if w2:
                    o.vertex_groups[n2].add([i], w2 / (w1 + w2), "REPLACE")
                fixed += 1
        if not any(m.type == "ARMATURE" for m in o.modifiers):
            m = o.modifiers.new("rig", "ARMATURE"); m.object = arm
    print("rig built;", fixed, "vertices given to their nearest bone;", rigid, "in loose pieces moved as one")
    return arm, {"upperarm": "upperarm.%s", "forearm": "forearm.%s", "thigh": "thigh.%s", "shin": "shin.%s"}

def own_rig(arm):
    names = {}
    for side, word in (("L", "left"), ("R", "right")):
        names[("upperarm", side)] = find(arm, word, "arm")
        names[("forearm", side)] = find(arm, word, "elbow")
        names[("thigh", side)] = find(arm, word, "leg")
        names[("shin", side)] = find(arm, word, "knee")
    if all(names.values()):
        return {k: v.name for k, v in names.items()}
    print("own skeleton lacks some bones:", [k for k, v in names.items() if not v])
    return None

def rotate(arm, bone_name, axis, angle):
    """Turn a pose bone round its head, about a world axis (children follow)."""
    bpy.context.view_layer.update()
    pb = arm.pose.bones[bone_name]
    head = arm.matrix_world @ pb.head
    mw = arm.matrix_world @ pb.matrix
    R = Matrix.Translation(head) @ Matrix.Rotation(angle, 4, axis) @ Matrix.Translation(-head)
    pb.matrix = arm.matrix_world.inverted() @ (R @ mw)
    bpy.context.view_layer.update()

def arm_down(arm, bone_name, side, target=-82):
    bpy.context.view_layer.update()
    pb = arm.pose.bones[bone_name]
    h = arm.matrix_world @ pb.head; t = arm.matrix_world @ pb.tail
    cur = math.atan2(t.z - h.z, (t.x - h.x) * side)
    rotate(arm, bone_name, "Y", -(math.radians(target) - cur) * side)

def pose_and_save(kind):
    sc, meshes = load()
    arms = [o for o in sc.objects if o.type == "ARMATURE"]
    names = own_rig(arms[0]) if arms else None
    if names:
        arm = arms[0]
        B = lambda part, side: names[(part, side)]
    else:
        for a in arms:  # an unusable skeleton: drop it (and its modifiers) and build one
            for o in meshes:
                for m in list(o.modifiers):
                    if m.type == "ARMATURE":
                        bpy.context.view_layer.objects.active = o
                        bpy.ops.object.modifier_apply(modifier=m.name)
            bpy.data.objects.remove(a, do_unlink=True)
        arm, pat = build_rig(meshes)
        B = lambda part, side: pat[part] % side
    bpy.context.view_layer.objects.active = arm
    bpy.ops.object.mode_set(mode="POSE")
    for side, tag in ((1, "L"), (-1, "R")):
        arm_down(arm, B("upperarm", tag), side)
        if kind == "seated":
            # facing -Y: "forward" is -Y; about +X a negative angle swings a downward bone forward
            # a car seat: knees a little above the hips (thighs 15° up), shins angled forward, hands on the lap
            rotate(arm, B("thigh", tag), "X", math.radians(-112))     # knees 22 deg above the hips (footwell, not floor)
            rotate(arm, B("shin", tag), "X", math.radians(80))
            rotate(arm, B("upperarm", tag), "X", math.radians(-15))
            rotate(arm, B("forearm", tag), "X", math.radians(-35))
        else:
            rotate(arm, B("forearm", tag), "X", math.radians(-8))       # a relaxed elbow
    bpy.ops.object.mode_set(mode="OBJECT")
    for o in meshes:                                      # bake the pose into the meshes
        for m in list(o.modifiers):
            if m.type == "ARMATURE":
                bpy.context.view_layer.objects.active = o
                try: bpy.ops.object.modifier_apply(modifier=m.name)
                except Exception as e: print("apply failed", o.name, e)
    bpy.data.objects.remove(arm, do_unlink=True)
    bpy.ops.wm.save_as_mainfile(filepath=os.path.join(outdir, f"{name}_{kind}.blend"))
    cam = bpy.data.objects.new("cam", bpy.data.cameras.new("cam")); sc.collection.objects.link(cam); sc.camera = cam
    cam.data.type = "ORTHO"; cam.data.ortho_scale = 1.9
    sc.render.engine = "BLENDER_WORKBENCH"; sc.display.shading.color_type = "TEXTURE"; sc.display.shading.light = "STUDIO"
    sc.render.resolution_x, sc.render.resolution_y = 380, 520
    for view, loc, rot in (("front", (0, -10, 0.86), (math.radians(90), 0, 0)),
                           ("side", (10, 0, 0.86), (math.radians(90), 0, math.radians(90)))):
        cam.location = loc; cam.rotation_euler = rot
        sc.render.filepath = os.path.join(outdir, f"{name}_{kind}_{view}.png")
        bpy.ops.render.render(write_still=True)
    print("POSED", name, kind, "own skeleton" if names else "built skeleton")

pose_and_save("standing")
pose_and_save("seated")
