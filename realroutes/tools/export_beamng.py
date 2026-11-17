# A posed passenger (.blend from pose.py) -> BeamNG: model.dae (+ textures) and materials.json for realroutes.lua.
# Faces +Y like the cut-out card (quatFromDir points +Y at the car). Materials are renamed rr_<name>_<n>.
import bpy, sys, os, math, json, shutil, re
args = sys.argv[sys.argv.index("--") + 1:]
blend, name, out = args[0], args[1], args[2]
bpy.ops.wm.open_mainfile(filepath=blend)
for o in list(bpy.data.objects):
    if o.type in ("CAMERA", "ARMATURE", "LIGHT"):
        bpy.data.objects.remove(o, do_unlink=True)
meshes = [o for o in bpy.data.objects if o.type == "MESH"]
# bake every object's transform into its mesh (no parents left), then turn round to face +Y
for o in meshes:
    for m in list(o.modifiers):
        bpy.context.view_layer.objects.active = o
        try: bpy.ops.object.modifier_apply(modifier=m.name)
        except Exception: o.modifiers.remove(m)
    mw = o.matrix_world.copy()
    o.parent = None
    o.matrix_world = mw
for o in [o for o in bpy.data.objects if o.type == "EMPTY"]:
    bpy.data.objects.remove(o, do_unlink=True)
bpy.ops.object.select_all(action="DESELECT")
for o in meshes:
    o.select_set(True)
bpy.context.view_layer.objects.active = meshes[0]
bpy.ops.object.transform_apply(location=True, rotation=True, scale=True)
for o in meshes:
    o.data.transform(__import__("mathutils").Matrix.Rotation(math.pi, 4, "Z"))
os.makedirs(out, exist_ok=True)
mats = []
safe = re.sub(r"[^A-Za-z0-9]", "", name)
for i, m in enumerate([m for m in bpy.data.materials if m.users]):
    m.name = f"rr_{safe}_{i}"
    tex, alpha = None, False
    if m.use_nodes:
        for n in m.node_tree.nodes:
            if n.type == "BSDF_PRINCIPLED":
                link = n.inputs["Base Color"].links
                if link and link[0].from_node.type == "TEX_IMAGE" and link[0].from_node.image:
                    img = link[0].from_node.image
                    fn = f"{safe}_{i}.png"
                    img.filepath_raw = os.path.join(out, fn); img.file_format = "PNG"
                    try: img.save()
                    except Exception:
                        src = bpy.path.abspath(img.filepath)
                        if os.path.exists(src): shutil.copy(src, os.path.join(out, fn))
                    tex = fn
                    alpha = bool(n.inputs["Alpha"].links) or getattr(m, "blend_method", "OPAQUE") != "OPAQUE"
    col = [1, 1, 1, 1]
    if m.use_nodes:
        for n in m.node_tree.nodes:
            if n.type == "BSDF_PRINCIPLED":
                col = [round(c, 4) for c in n.inputs["Base Color"].default_value]
    mats.append({"name": m.name, "tex": tex, "alpha": alpha, "color": col})
def write_dae(path, objs, plain=False):
    """A static COLLADA file (Z up, metres): one geometry per mesh, triangles grouped by material; per-corner
    normals and UVs. Blender 5 has no COLLADA exporter, and BeamNG loads .dae."""
    f = open(path, "w")
    w = f.write
    used = sorted({m.name for o in objs for m in o.data.materials if m})
    w('<?xml version="1.0" encoding="utf-8"?>\n<COLLADA xmlns="http://www.collada.org/2005/11/COLLADASchema" version="1.4.1">\n')
    w('<asset><unit name="meter" meter="1"/><up_axis>Z_UP</up_axis></asset>\n<library_effects>\n')
    for mn in used:
        w(f'<effect id="{mn}-fx"><profile_COMMON><technique sid="c"><lambert><diffuse><color>1 1 1 1</color></diffuse></lambert></technique></profile_COMMON></effect>\n')
    w('</library_effects>\n<library_materials>\n')
    for mn in used:
        w(f'<material id="{mn}" name="{mn}"><instance_effect url="#{mn}-fx"/></material>\n')
    w('</library_materials>\n<library_geometries>\n')
    nodes = []
    for gi, o in enumerate(objs):
        me = o.data
        me.calc_loop_triangles()
        if not me.loop_triangles:
            continue
        uvl = me.uv_layers.active.data if me.uv_layers.active else None
        gid = f"g{gi}"
        pos = " ".join(f"{v.co.x:.5f} {v.co.y:.5f} {v.co.z:.5f}" for v in me.vertices)
        nrm = " ".join(f"{n.vector.x:.4f} {n.vector.y:.4f} {n.vector.z:.4f}" for n in me.corner_normals)
        uv = " ".join(f"{d.uv.x:.5f} {d.uv.y:.5f}" for d in uvl) if uvl else "0 0"
        nl = len(me.loops)
        w(f'<geometry id="{gid}"><mesh>\n')
        w(f'<source id="{gid}-p"><float_array id="{gid}-pa" count="{len(me.vertices) * 3}">{pos}</float_array><technique_common><accessor source="#{gid}-pa" count="{len(me.vertices)}" stride="3"><param name="X" type="float"/><param name="Y" type="float"/><param name="Z" type="float"/></accessor></technique_common></source>\n')
        w(f'<source id="{gid}-n"><float_array id="{gid}-na" count="{nl * 3}">{nrm}</float_array><technique_common><accessor source="#{gid}-na" count="{nl}" stride="3"><param name="X" type="float"/><param name="Y" type="float"/><param name="Z" type="float"/></accessor></technique_common></source>\n')
        w(f'<source id="{gid}-t"><float_array id="{gid}-ta" count="{(nl if uvl else 1) * 2}">{uv}</float_array><technique_common><accessor source="#{gid}-ta" count="{nl if uvl else 1}" stride="2"><param name="S" type="float"/><param name="T" type="float"/></accessor></technique_common></source>\n')
        w(f'<vertices id="{gid}-v"><input semantic="POSITION" source="#{gid}-p"/></vertices>\n')
        by_mat = {}
        for t in me.loop_triangles:
            by_mat.setdefault(t.material_index, []).append(t)
        binds = []
        for mi, tris in by_mat.items():
            mat = me.materials[mi] if mi < len(me.materials) and me.materials[mi] else None
            if not mat:
                continue
            idx = []
            for t in tris:
                for v, l in zip(t.vertices, t.loops):
                    idx.append(f"{v} {l} {l if uvl else 0}")
            w(f'<triangles material="{mat.name}" count="{len(tris)}"><input semantic="VERTEX" source="#{gid}-v" offset="0"/><input semantic="NORMAL" source="#{gid}-n" offset="1"/><input semantic="TEXCOORD" source="#{gid}-t" offset="2" set="0"/><p>{" ".join(idx)}</p></triangles>\n')
            binds.append(mat.name)
        w('</mesh></geometry>\n')
        nodes.append((gid, sorted(set(binds)), o.name))
    # BeamNG (Torque) detail convention: base00 > start01 > <mesh>_a<size>; without it nothing is drawn
    tag = os.path.splitext(os.path.basename(path))[0]
    w('</library_geometries>\n<library_visual_scenes><visual_scene id="scene">\n')
    if plain:   # a vehicle mesh file: one plain node per mesh, named as the jbeam refers to it
        for gid, binds, oname in nodes:
            bm = "".join(f'<instance_material symbol="{b}" target="#{b}"/>' for b in binds)
            w(f'<node id="{oname}" name="{oname}" type="NODE"><matrix sid="transform">1 0 0 0 0 1 0 0 0 0 1 0 0 0 0 1</matrix><instance_geometry url="#{gid}" name="{oname}"><bind_material><technique_common>{bm}</technique_common></bind_material></instance_geometry></node>\n')
        w('</visual_scene>\n</library_visual_scenes>\n<scene><instance_visual_scene url="#scene"/></scene>\n</COLLADA>\n')
        f.close()
        return
    w('<node id="base00" name="base00" type="NODE"><node id="start01" name="start01" type="NODE">\n')
    for gid, binds, _ in nodes:
        bm = "".join(f'<instance_material symbol="{b}" target="#{b}"/>' for b in binds)
        nm = f"{safe}_{tag}_{gid}_a2"
        w(f'<node id="{nm}" name="{nm}" type="NODE"><instance_geometry url="#{gid}" name="{nm}"><bind_material><technique_common>{bm}</technique_common></bind_material></instance_geometry></node>\n')
    w('</node></node>\n</visual_scene></library_visual_scenes>\n<scene><instance_visual_scene url="#scene"/></scene>\n</COLLADA>\n')
    f.close()

write_dae(os.path.join(out, "model.dae"), meshes)

# seated: legs bent to sit (thighs forward at the hip, shins down at the knee), origin at the hips. The model faces
# +Y here, so "forward" is +Y; rotations are about the X axis through the hip / knee, both legs at once.
import bmesh
from mathutils import Matrix, Vector
H = 1.68
HIP, KNEE = 0.52 * H, 0.285 * H
smooth = lambda w: w * w * (3 - 2 * w)
def sit(p, ycen):
    if p.z >= HIP:
        return p
    hip = Vector((p.x, ycen, HIP))
    w1 = smooth(min(1.0, (HIP - p.z) / (0.05 * H)))
    r1 = Matrix.Rotation(math.radians(90) * w1, 3, "X")
    q = hip + r1 @ (p - hip)
    if p.z < KNEE:
        knee = hip + Matrix.Rotation(math.radians(90), 3, "X") @ (Vector((p.x, ycen, KNEE)) - hip)
        w2 = smooth(min(1.0, (KNEE - p.z) / (0.04 * H)))
        q = knee + Matrix.Rotation(math.radians(-90) * w2, 3, "X") @ (q - knee)
    return q
if len(args) > 3:
    # a seated pose made with bones (rig.py): same source, so the materials come in the same order
    bpy.ops.wm.open_mainfile(filepath=args[3])
    for o in list(bpy.data.objects):
        if o.type in ("CAMERA", "ARMATURE", "LIGHT"):
            bpy.data.objects.remove(o, do_unlink=True)
    meshes = [o for o in bpy.data.objects if o.type == "MESH"]
    for o in meshes:
        for m in list(o.modifiers):
            bpy.context.view_layer.objects.active = o
            try: bpy.ops.object.modifier_apply(modifier=m.name)
            except Exception: o.modifiers.remove(m)
        mw = o.matrix_world.copy(); o.parent = None; o.matrix_world = mw
    for o in [o for o in bpy.data.objects if o.type == "EMPTY"]:
        bpy.data.objects.remove(o, do_unlink=True)
    bpy.ops.object.select_all(action="DESELECT")
    for o in meshes: o.select_set(True)
    bpy.context.view_layer.objects.active = meshes[0]
    bpy.ops.object.transform_apply(location=True, rotation=True, scale=True)
    for i, m in enumerate([m for m in bpy.data.materials if m.users]):
        m.name = f"rr_{safe}_{i}"
    for o in meshes:
        o.data.transform(Matrix.Rotation(math.pi, 4, "Z"))
        for v in o.data.vertices:
            v.co.z -= HIP
else:
    ys = [v.co.y for o in meshes for v in o.data.vertices if 0.45 * H < v.co.z < 0.6 * H]
    ycen = sum(ys) / max(1, len(ys))
    for o in meshes:
        for v in o.data.vertices:
            v.co = sit(v.co.copy(), ycen)
            v.co.z -= HIP
write_dae(os.path.join(out, "seated.dae"), meshes)
# the car's own occupant part (vehicle.dae): the seated pose in BeamNG's occupant frame (origin on the seat cushion,
# -Y forward, Z up), one body mesh and one head mesh, so the car carries the person like its stock dummy and the
# head can be hidden from the driver's own camera. Materials rrv_<name>_<n> (vehicle materials file).
CUSHION = 0.03                                   # hip joint height above the cushion surface
vobjs = []
for o in meshes:
    c = o.copy(); c.data = o.data.copy(); bpy.context.scene.collection.objects.link(c); vobjs.append(c)
bpy.ops.object.select_all(action="DESELECT")
for c in vobjs: c.select_set(True)
bpy.context.view_layer.objects.active = vobjs[0]
bpy.ops.object.join()
body = bpy.context.view_layer.objects.active
body.data.transform(Matrix.Rotation(math.pi, 4, "Z"))           # back to facing -Y (the car's forward)
body.data.transform(Matrix.Translation((0, 0, CUSHION)))
head = body.copy(); head.data = body.data.copy(); bpy.context.scene.collection.objects.link(head)
NECKV = 0.80 * H - HIP + CUSHION
for ob, keep_head in ((body, False), (head, True)):
    bm = bmesh.new(); bm.from_mesh(ob.data)
    gone = [fc for fc in bm.faces if (max(v.co.z for v in fc.verts) > NECKV) != keep_head]
    bmesh.ops.delete(bm, geom=gone, context="FACES")
    bmesh.ops.delete(bm, geom=[v for v in bm.verts if not v.link_faces], context="VERTS")
    bm.to_mesh(ob.data); bm.free()
body.name = f"rr_{safe}_body"; head.name = f"rr_{safe}_head"
vmats = {}
for ob in (body, head):
    for i, m in enumerate(ob.data.materials):
        if m and not m.name.startswith("rrv_"):
            if m.name not in vmats:
                n = m.copy(); n.name = m.name.replace("rr_", "rrv_", 1); vmats[m.name] = n
            ob.data.materials[i] = vmats[m.name]
write_dae(os.path.join(out, "vehicle.dae"), [body, head], plain=True)
for ob in (body, head):
    bpy.data.objects.remove(ob, do_unlink=True)

# the driver's own body for first person: the same seated pose without the head and hair above the neck (the camera
# sits at the eyes, inside the head)
NECK = 0.80 * H - HIP
for o in meshes:
    bm = bmesh.new(); bm.from_mesh(o.data)
    bmesh.ops.delete(bm, geom=[v for v in bm.verts if v.co.z > NECK], context="VERTS")
    bm.to_mesh(o.data); bm.free()
write_dae(os.path.join(out, "seated_driver.dae"), [o for o in meshes if len(o.data.polygons)])
# a side view of the seated pose, to check it
import bpy as _b
sc = _b.context.scene
cam = _b.data.objects.new("cam", _b.data.cameras.new("cam")); sc.collection.objects.link(cam); sc.camera = cam
cam.data.type = "ORTHO"; cam.data.ortho_scale = 1.6
cam.location = (8, 0.3, 0.1); cam.rotation_euler = (math.radians(90), 0, math.radians(90))
sc.render.engine = "BLENDER_WORKBENCH"; sc.display.shading.color_type = "TEXTURE"; sc.display.shading.light = "STUDIO"
sc.render.resolution_x, sc.render.resolution_y = 380, 380
sc.render.filepath = os.path.join(out, "seated_side.png"); _b.ops.render.render(write_still=True)
json.dump({"name": name, "materials": mats}, open(os.path.join(out, "materials.json"), "w"), indent=1)
faces = sum(len(o.data.polygons) for o in meshes)
print("EXPORTED", name, len(mats), "materials", faces, "faces")
