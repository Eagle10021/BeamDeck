# Real Routes passengers: normalise a downloaded model (1.68 m, feet at z=0, centred, facing -Y as imported)
# and bring the arms down from the A-pose. Skeleton: rotate the upper-arm bones and apply. No skeleton: rotate the
# vertices along each arm round the shoulder, blended near the shoulder. Saves <name>.blend and front/side renders.
import bpy, sys, math, os, mathutils
from mathutils import Vector, Matrix
args = sys.argv[sys.argv.index("--") + 1:]
src, name, outdir = args[0], args[1], args[2]
SLEEVE = float(args[3]) if len(args) > 3 else 0.075   # arm zone radius (share of height): wide sleeves need more
FLOOR = float(args[4]) if len(args) > 4 else 0.40      # nothing below this share of the height is moved
H = 1.68
TARGET = math.radians(-82)          # arm angle below horizontal when done (-90 = straight down)

bpy.ops.wm.read_factory_settings(use_empty=True)
bpy.ops.import_scene.gltf(filepath=src)
sc = bpy.context.scene
meshes = [o for o in sc.objects if o.type == "MESH"]
arms = [o for o in sc.objects if o.type == "ARMATURE"]

def bbox(objs):
    bpy.context.view_layer.update()
    mn = Vector((1e9,) * 3); mx = -mn
    for o in objs:
        for c in o.bound_box:
            w = o.matrix_world @ Vector(c)
            mn = Vector(map(min, mn, w)); mx = Vector(map(max, mx, w))
    return mn, mx

# normalise: one root empty scaled and moved. Size from the vertices' 0.2 / 99.8 percentiles, not the bounding
# boxes: a stray part far from the body would otherwise shrink the whole model.
def robust(objs):
    bpy.context.view_layer.update()
    xs, ys, zs = [], [], []
    dg = bpy.context.evaluated_depsgraph_get()
    for o in objs:                       # as drawn (a skinned mesh's own vertices are in the skeleton's space)
        ev = o.evaluated_get(dg); me = ev.to_mesh(); mw = ev.matrix_world
        for v in me.vertices:
            w = mw @ v.co; xs.append(w.x); ys.append(w.y); zs.append(w.z)
        ev.to_mesh_clear()
    q = lambda a, f: sorted(a)[int(f * (len(a) - 1))]
    return Vector((q(xs, .002), q(ys, .002), q(zs, .002))), Vector((q(xs, .998), q(ys, .998), q(zs, .998)))
mn, mx = robust(meshes)
s = H / (mx.z - mn.z)
root = bpy.data.objects.new("root", None); sc.collection.objects.link(root)
for o in [o for o in sc.objects if o.parent is None and o is not root]:
    o.parent = root
root.scale = (s, s, s)
root.location = (-(mn.x + mx.x) / 2 * s, -(mn.y + mx.y) / 2 * s, -mn.z * s)
bpy.context.view_layer.update()

posed = "none"
if arms:
    arm = arms[0]
    bpy.context.view_layer.objects.active = arm
    bpy.ops.object.mode_set(mode="POSE")
    done = 0
    for pb in arm.pose.bones:
        nm = pb.name.lower()
        side = 1 if ("left arm" in nm or nm.startswith("arm_l") or "左腕" in pb.name) else -1 if ("right arm" in nm or nm.startswith("arm_r") or "右腕" in pb.name) else 0
        if not side or "twist" in nm:
            continue
        head = arm.matrix_world @ pb.head
        tail = arm.matrix_world @ pb.tail
        cur = math.atan2(tail.z - head.z, (tail.x - head.x) * side)
        rot = Matrix.Rotation((TARGET - cur) * side * -1, 4, "Y")     # about the forward axis, at the shoulder
        mw = arm.matrix_world @ pb.matrix
        t = Matrix.Translation(head)
        new = t @ rot @ t.inverted() @ mw
        pb.matrix = arm.matrix_world.inverted() @ new
        bpy.context.view_layer.update()
        done += 1
    bpy.ops.object.mode_set(mode="OBJECT")
    # bake the pose into the meshes
    for o in meshes:
        for m in list(o.modifiers):
            if m.type == "ARMATURE":
                bpy.context.view_layer.objects.active = o
                try: bpy.ops.object.modifier_apply(modifier=m.name)
                except Exception as e: print("apply failed", o.name, e)
    posed = f"skeleton ({done} bones)"
else:
    # vertex bend: find each arm's tip (the outermost point between hip and shoulder height) and the shoulder
    deps = bpy.context.evaluated_depsgraph_get()
    pts = []
    for o in meshes:
        mw = o.matrix_world
        pts += [mw @ v.co for v in o.data.vertices]
    moved = 0
    for side in (1, -1):
        cand = [p for p in pts if 0.45 * H < p.z < 0.82 * H and p.x * side > 0]
        hand = max(cand, key=lambda p: p.x * side)
        sh = Vector((0.105 * H * side, 0, 0.815 * H))
        sh.y = sum(p.y for p in cand if abs(p.x - sh.x) < 0.03) / max(1, sum(1 for p in cand if abs(p.x - sh.x) < 0.03))
        axis = hand - sh
        L = axis.length
        cur = math.atan2(axis.z, axis.x * side)
        ang = (TARGET - cur)
        r_arm = SLEEVE * H
        def zone(p):
            """(in the arm's zone, t along the arm) for a world point; the hip floor is checked separately."""
            if p.x * side < sh.x * side - 0.01:
                return False, 0.0
            t = (p - sh).dot(axis) / (L * L)
            if t < -0.02 or t > 1.15:
                return False, t
            d = (p - (sh + axis * max(0.0, min(1.0, t)))).length
            return d <= r_arm * (1.0 + 0.6 * max(0.0, t)), t
        smooth = lambda w: w * w * (3 - 2 * w)
        for o in meshes:
            mw = o.matrix_world; inv = mw.inverted()
            me = o.data
            # loose pieces (ribbons, tassels, sleeve panels): union-find over the edges
            parent = list(range(len(me.vertices)))
            def find(i):
                while parent[i] != i:
                    parent[i] = parent[parent[i]]; i = parent[i]
                return i
            for e in me.edges:
                ra, rb = find(e.vertices[0]), find(e.vertices[1])
                if ra != rb: parent[ra] = rb
            world = [mw @ v.co for v in me.vertices]
            info = [zone(p) for p in world]
            groups = {}
            for i in range(len(world)):
                groups.setdefault(find(i), []).append(i)
            total = len(world)
            for g in groups.values():
                inz = [i for i in g if info[i][0]]
                small = len(g) < 0.3 * total
                if small and len(inz) >= 0.5 * len(g):
                    # a piece hanging off the arm: swings down whole, by its average place along the arm
                    tm = sum(info[i][1] for i in inz) / len(inz)
                    w = smooth(max(0.0, min(1.0, (tm - 0.02) / 0.22)))
                    rot = Matrix.Rotation(ang * w * side * -1, 3, "Y")
                    for i in g:
                        me.vertices[i].co = inv @ (sh + rot @ (world[i] - sh))
                        moved += 1
                else:
                    for i in inz:
                        if world[i].z < FLOOR * H:          # nothing below the hips (hair, coat tails)
                            continue
                        w = smooth(max(0.0, min(1.0, (info[i][1] - 0.02) / 0.22)))
                        rot = Matrix.Rotation(ang * w * side * -1, 3, "Y")
                        me.vertices[i].co = inv @ (sh + rot @ (world[i] - sh))
                        moved += 1
        print(name, "side", side, "arm angle", round(math.degrees(cur)), "->", round(math.degrees(TARGET)))
    posed = f"vertex bend ({moved} verts)"
print("POSED", name, posed)

os.makedirs(outdir, exist_ok=True)
bpy.ops.wm.save_as_mainfile(filepath=os.path.join(outdir, f"{name}.blend"))
cam = bpy.data.objects.new("cam", bpy.data.cameras.new("cam")); sc.collection.objects.link(cam); sc.camera = cam
cam.data.type = "ORTHO"; cam.data.ortho_scale = 1.9
sc.render.engine = "BLENDER_WORKBENCH"; sc.display.shading.color_type = "TEXTURE"; sc.display.shading.light = "STUDIO"
sc.render.resolution_x, sc.render.resolution_y = 360, 560
for view, loc, rot in (("front", (0, -10, 0.86), (math.radians(90), 0, 0)), ("side", (10, 0, 0.86), (math.radians(90), 0, math.radians(90)))):
    cam.location = loc; cam.rotation_euler = rot
    sc.render.filepath = os.path.join(outdir, f"{name}_{view}.png")
    bpy.ops.render.render(write_still=True)
