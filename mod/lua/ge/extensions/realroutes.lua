-- Real Routes: drive real bus routes on BeamNG.Earth. Thin game side; the helper (realroutes_helper.py) runs the shift.
--   sends   -> 127.0.0.1:47151  {type="pose"}: position, speed, doors, g-forces at 10 Hz; {type="hello"}
--   listens <- 127.0.0.1:47150  commands: goto, line, atStop, departed, stopRequest, gps, gpsClear, markers,
--                               doors, kneel, hazards, notify
-- Needs BeamNG.Earth (its modding API gives real coordinates). The bus displays work on the stock city bus.
local M = {}

local socket = require("socket")
local udp
local GAME_PORT, HELPER_PORT = 47150, 47151
local SEND_EVERY = 0.1
local BUS_MODEL = "citybus"
-- a livery mod can add its own config; the stock city bus otherwise
local BUS_CONFIGS = {"vehicles/citybus/oc_transpo.pc", "vehicles/citybus/city.pc"}
local function busConfig()
  for _, c in ipairs(BUS_CONFIGS) do
    if FS:fileExists("/" .. c) then return c end
  end
  return BUS_CONFIGS[#BUS_CONFIGS]
end

local sendTimer, helloTimer = 0, 0
local veh = {d = 0, k = 0, gx = 0, gy = 0, bus = false}   -- last report from the player's vehicle
local markers = {}          -- {lat, lon, name, code, next, pos (game frame, cleared on rebase)}
local lineInfo = nil        -- the last sign/stop list, applied again to a new bus
local lastStopReq = nil
local pendingGoto = nil     -- {lat, lon, heading, name, bus, stage, at}
local lineAgainAt = nil
local sentRouteVersion = nil

local function openSocket()
  if udp then return end
  udp = socket.udp()
  udp:settimeout(0)
  local ok, err = udp:setsockname("127.0.0.1", GAME_PORT)
  if not ok then log("E", "realroutes", "Can't listen on UDP " .. GAME_PORT .. ": " .. tostring(err)) end
end

local function send(t)
  if udp then udp:sendto(jsonEncode(t), "127.0.0.1", HELPER_PORT) end
end

-- BeamNG.Earth's map tile endpoint (127.0.0.1, a new port each launch): the tablet's map uses the same tiles
local function mapPort()
  local core = rawget(_G, "beamngEarth_core")
  if not (core and core._state) then return nil end
  local ok, st = pcall(core._state)
  return ok and st and st.mapPort or nil
end

-- the GPS route as BeamNG.Earth plans it, sent once per new plan (thinned to at most ~400 points)
local function sendRoute(a)
  if not (a.has and a.has("gpsRoute")) then return end
  local g = a.gps and a.gps()
  local v = g and g.routeVersion
  if v == sentRouteVersion then return end
  sentRouteVersion = v
  local r = v and a.gpsRoute()
  local pts = {}
  if r and r.points then
    local step = math.max(1, math.floor(#r.points / 400))
    for i = 1, #r.points, step do pts[#pts + 1] = {r.points[i].lat or r.points[i][1], r.points[i].lon or r.points[i][2]} end
    local last = r.points[#r.points]
    if last then pts[#pts + 1] = {last.lat or last[1], last.lon or last[2]} end
  end
  -- turn-by-turn (BeamNG.Earth 1.7+: OSRM maneuvers with their distance from the route's start)
  local steps = {}
  if r and type(r.steps) == "table" then
    for _, st in ipairs(r.steps) do
      local loc = st.location or {}
      steps[#steps + 1] = {type = st.type, modifier = st.modifier, name = st.name, ref = st.ref, exit = st.exit,
                           offset = st.offset, distance = st.distance, lat = loc.lat or loc[1], lon = loc.lon or loc[2]}
    end
  end
  send({type = "route", version = v, points = pts, dist = r and r.dist, dur = r and r.dur, steps = steps})
end

local function player() return getPlayerVehicle(0) end

-- ---- regular maps (no BeamNG.Earth): the game's own metres stand in for latitude / longitude ----
-- lat = y / 110574, lon = x / 111320 (the helper measures distances with the same factors at latitude 0), so
-- the helper's rides, passengers and ratings work unchanged. Places = the level's big-map points of interest,
-- routes = BeamNG's own ground-marker navigation, the road map for the tablet / Android Auto = the level's AI roads.
local K_LAT, K_LON = 110574, 111320
local flat = {gpsVersion = 0}
local flatPois, flatPoisLevel = nil, nil
local function toFlat(p) return p.y / K_LAT, p.x / K_LON end
flat.version = function() return 1 end
flat.has = function(name) return name == "gpsRoute" or name == "places" end
flat.ready = function() return player() ~= nil end
flat.inWorld = function() return true end
flat.rebasing = function() return false end
flat.earthVersion = function() return nil end
flat.speedLimit = function() return nil end
flat.teleport = function() return false, "not on BeamNG.Earth" end
flat.toGame = function(lat, lon, h) return lon * K_LON, lat * K_LAT, h or 0 end
flat.pose = function()
  local v = player()
  if not v then return nil end
  local p, d, vel = vec3(v:getPosition()), vec3(v:getDirectionVector()), vec3(v:getVelocity())
  local lat, lon = toFlat(p)
  return {lat = lat, lon = lon, x = p.x, y = p.y, z = p.z, speed = vel:length(),
          heading = math.deg(math.atan2(d.x, d.y)) % 360}
end
flat.place = function()
  local lvl = getCurrentLevelIdentifier and getCurrentLevelIdentifier()
  local info = lvl and core_levels and core_levels.getLevelByName(lvl)
  local title = info and info.title or lvl
  if type(title) == "string" and title:find("^levels%.") and translateLanguage then title = translateLanguage(title, title, true) end
  return {place = title}
end
flat.gpsSet = function(lat, lon, name)
  flat.target = vec3(lon * K_LON, lat * K_LAT, 0)
  local x, y = flat.target.x, flat.target.y
  local g = be:getSurfaceHeightBelow(vec3(x, y, 2000))
  if g and g > -1e9 then flat.target.z = g end
  flat.targetName = name
  flat.gpsVersion = flat.gpsVersion + 1
  if core_groundMarkers then core_groundMarkers.setPath(flat.target) end
  return true
end
flat.gpsSetRoute = function(points)
  local p = points and points[1]
  if not p then return false, "no points" end
  return flat.gpsSet(p.lat, p.lon, p.name)
end
flat.gpsClear = function()
  flat.target, flat.targetName = nil, nil
  flat.gpsVersion = flat.gpsVersion + 1
  if core_groundMarkers then core_groundMarkers.setPath(nil) end
end
flat.gps = function()
  -- a destination set here, or one the player set on BeamNG's big map: both drive the ground-marker route
  local gm = core_groundMarkers
  local path = gm and gm.routePlanner and gm.routePlanner.path
  if not (flat.target or (gm and gm.endWP and path and path[1])) then return nil end
  local left = gm and gm.getPathLength() or nil
  if not left or left == math.huge then left = nil end
  local last = path and path[#path] and path[#path].pos
  local endKey = last and string.format("%d,%d", math.floor(last.x), math.floor(last.y)) or "-"
  -- a new version every 250 m and for every new destination, so the tablet / car get the trimmed route
  return {name = flat.targetName, remaining = left, eta = left and left / 11 or nil,
          routeVersion = string.format("%d:%s:%d", flat.gpsVersion, endKey, left and math.floor(left / 250) or -1)}
end
flat.gpsRoute = function()
  local path = core_groundMarkers and core_groundMarkers.routePlanner and core_groundMarkers.routePlanner.path
  if not path or not path[1] then return nil end
  local pts, steps, along = {}, {}, 0
  local prevHd, turnAcc, turnAt = nil, 0, nil
  for i, n in ipairs(path) do
    local lat, lon = toFlat(n.pos)
    pts[#pts + 1] = {lat, lon}
    if i > 1 then
      local a, b = path[i - 1].pos, n.pos
      local seg = a:distance(b)
      local hd = math.deg(math.atan2(b.x - a.x, b.y - a.y))
      if prevHd and seg > 0.5 then
        local d = (hd - prevHd + 540) % 360 - 180            -- + right, - left
        turnAcc = turnAcc + d
        turnAt = turnAt or along
        if math.abs(turnAcc) > 35 and seg > 4 then          -- a real turn (not a gentle bend)
          local m = math.abs(turnAcc) > 120 and "sharp " or math.abs(turnAcc) < 55 and "slight " or ""
          local plat, plon = toFlat(a)
          steps[#steps + 1] = {type = "turn", modifier = m .. (turnAcc > 0 and "right" or "left"), name = "",
                               offset = turnAt, location = {lat = plat, lon = plon}}
          turnAcc, turnAt = 0, nil
        elseif seg > 25 then
          turnAcc, turnAt = 0, nil                          -- a long straight forgets small wiggles
        end
      end
      if seg > 0.5 then prevHd = hd end
      along = along + seg
    end
  end
  local last = path[#path].pos
  local llat, llon = toFlat(last)
  steps[#steps + 1] = {type = "arrive", modifier = "", name = flat.targetName or "", offset = along, location = {lat = llat, lon = llon}}
  return {points = pts, dist = along, dur = along / 11, steps = steps}
end
-- places: the level's big-map points of interest (garages, gas stations, dealerships, missions...)
flat.places = function(lat, lon, opts, cb)
  local lvl = getCurrentLevelIdentifier and getCurrentLevelIdentifier()
  if flatPoisLevel ~= lvl then
    flatPois, flatPoisLevel = {}, lvl
    local ok, list = pcall(function() return gameplay_rawPois and gameplay_rawPois.getRawPoiListByLevel(lvl) end)
    for _, e in ipairs(ok and list or {}) do
      local bm = e.markerInfo and e.markerInfo.bigmapMarker
      if bm and bm.pos and bm.name then
        local name = bm.name
        if type(name) == "string" and translateLanguage then name = translateLanguage(name, name, true) end
        local plat, plon = toFlat(vec3(bm.pos))
        flatPois[#flatPois + 1] = {id = tostring(e.id), name = name, category = e.data and e.data.type or "place",
                                   lat = plat, lon = plon, open = "open", confidence = 1}
      end
    end
  end
  local r = (opts and opts.radius or 2000)
  local out = {}
  for _, p in ipairs(flatPois) do
    local d = math.sqrt(((p.lat - lat) * K_LAT) ^ 2 + ((p.lon - lon) * K_LON) ^ 2)
    if d <= r then out[#out + 1] = p end
  end
  cb({places = out, complete = true})
  return true
end
-- the level's roads (AI graph) for the tablet's / car's map: written once per level, path sent in hello
local roadsFor, roadsFile = nil, nil
local function exportRoads()
  local lvl = getCurrentLevelIdentifier and getCurrentLevelIdentifier()
  if not lvl or roadsFor == lvl then return roadsFile end
  local m = map and map.getMap and map.getMap()
  if not (m and m.nodes) then return nil end
  local segs, seen = {}, {}
  for id, n in pairs(m.nodes) do
    for other, link in pairs(n.links or {}) do
      local key = id < other and (id .. "|" .. other) or (other .. "|" .. id)
      local o = m.nodes[other]
      if o and not seen[key] then
        seen[key] = true
        local la, lo = toFlat(n.pos)
        local lb, lob = toFlat(o.pos)
        segs[#segs + 1] = {la, lo, lb, lob, math.floor((link.drivability or 1) * 10 + 0.5) / 10}
      end
    end
  end
  local vpath = "/beamdeck_media/realroutes/roads.json"
  jsonWriteFile(vpath, {level = lvl, segs = segs}, false)
  roadsFor = lvl
  roadsFile = FS:getFileRealPath(vpath)
  return roadsFile
end

local function api()
  local a = rawget(_G, "beamngEarth_api")
  if a and a.version and a.version() == 1 and (not a.inWorld or a.inWorld()) then return a end
  if getCurrentLevelIdentifier and getCurrentLevelIdentifier() then return flat end
  return nil
end

local function vehLua(cmd)
  local v = player()
  if v then v:queueLuaCommand(cmd) end
end

local function busEvent(name, data)
  vehLua(string.format("controller.onGameplayEvent(%q, %s)", name, serialize(data)))
end

local function message(text, secs)
  local a = api()
  if a and a.notify then a.notify(text, secs or 5)
  else guihooks.trigger("Message", {ttl = secs or 5, msg = text, icon = "directions_bus"}) end
end

-- ---- vehicle state: asked from the vehicle's own Lua, answered through onVeh ----
local QUERY = [[
local c = controller.getController("bus")
obj:queueGameEngineLua("if realroutes then realroutes.onVeh(" .. serialize({
  d = electrics.values.dooropen or 0, k = electrics.values.kneel or 0,
  gx = sensors.gx2 or sensors.gx or 0, gy = sensors.gy2 or sensors.gy or 0, bus = c ~= nil,
  l = electrics.values.lights_state or electrics.values.lowbeam or 0}) .. ") end")
]]

M.onVeh = function(t) veh = t end

-- ---- placing the bus at the first stop ----
local function placeBus(g)
  local a, v = api(), player()
  if not (a and v) then return end
  local x, y, z = a.toGame(g.lat, g.lon, 0)
  if not x then return end
  local p = vec3(v:getPosition())
  local ground = be:getSurfaceHeightBelow(vec3(x, y, p.z + 60))
  if ground and ground > -1e9 then z = ground + 0.3 else z = p.z + 0.3 end
  local h = math.rad(g.heading or 0)
  local dir = vec3(math.sin(h), math.cos(h), 0)
  -- same convention as spawn.lua: quatFromDir(forward, up)
  local rot = quatFromDir(dir, vec3(0, 0, 1))
  spawn.safeTeleport(v, vec3(x, y, z), rot)
end

local function doGoto(c)
  pendingGoto = {lat = c.lat, lon = c.lon, heading = c.heading, name = c.name, stage = "teleport", at = os.clockhp()}
  local v = player()
  if c.bus and (not v or v:getJBeamFilename() ~= BUS_MODEL) then
    core_vehicles.replaceVehicle(BUS_MODEL, {config = busConfig()})
    pendingGoto.at = os.clockhp() + 1.5   -- let the new bus settle first
  end
end

-- ---- passengers in the world: a 3D model (if they have one) or a cut-out card, turning to face the car ----
-- Pictures / models are made from the player's own files (realroutes' make_avatars.py / Blender export) and copied
-- by BeamDeck into <userfolder>/beamdeck_media/realroutes/. Several people can stand at once (groups).
local MEDIA = "/beamdeck_media/realroutes/"
local MODELS = MEDIA .. "models/"
local madeMats, cardShapes = {}, {}
local standees = {}       -- {obj, shape, lat, lon, pos}
local seated = {}         -- {obj, shape, seat}: 3D passengers in the car's seats
local camLocal = {}       -- vehicle id -> {head (vehicle-local), rhd}

local function material(name, tex, color, alpha)
  if madeMats[name] or scenetree.findObject("rrm_" .. name) then madeMats[name] = true; return end
  local mat = createObject("Material")
  mat:setField("mapTo", 0, name)
  if tex then mat:setField("diffuseMap", 0, tex)
  else
    local c = color or {1, 1, 1, 1}
    mat:setField("diffuseColor", 0, string.format("%f %f %f %f", c[1], c[2], c[3], c[4] or 1))
  end
  mat:setField("doubleSided", 0, "1")
  if alpha then mat:setField("alphaTest", 0, "1"); mat:setField("alphaRef", 0, "64") end
  mat.canSave = false
  mat:registerObject("rrm_" .. name)
  madeMats[name] = true
end

-- a 3D passenger's shape (model.dae standing, seated.dae in a seat) with its materials, or nil
local function modelShape(avatar, which)
  local dir = MODELS .. avatar .. "/"
  local shape = dir .. (which or "model") .. ".dae"
  if not FS:fileExists(shape) then return nil end
  if not madeMats["@" .. avatar] then
    for _, m in ipairs((jsonReadFile(dir .. "materials.json") or {}).materials or {}) do
      material(m.name, m.tex and (dir .. m.tex) or nil, m.color, m.alpha)
    end
    madeMats["@" .. avatar] = true
  end
  return shape
end

-- a cut-out card for someone without a model: its own small shape file and material (one per person, so a group
-- can show several pictures at once)
local CARD = [[<?xml version="1.0" encoding="utf-8"?>
<COLLADA xmlns="http://www.collada.org/2005/11/COLLADASchema" version="1.4.1"><asset><unit name="meter" meter="1"/><up_axis>Z_UP</up_axis></asset>
<library_effects><effect id="fx"><profile_COMMON><technique sid="c"><lambert><diffuse><color>1 1 1 1</color></diffuse></lambert></technique></profile_COMMON></effect></library_effects>
<library_materials><material id="%s" name="%s"><instance_effect url="#fx"/></material></library_materials>
<library_geometries><geometry id="card"><mesh>
<source id="p"><float_array id="pa" count="12">0.45 0 0 -0.45 0 0 -0.45 0 1.65 0.45 0 1.65</float_array><technique_common><accessor source="#pa" count="4" stride="3"><param name="X" type="float"/><param name="Y" type="float"/><param name="Z" type="float"/></accessor></technique_common></source>
<source id="n"><float_array id="na" count="3">0 1 0</float_array><technique_common><accessor source="#na" count="1" stride="3"><param name="X" type="float"/><param name="Y" type="float"/><param name="Z" type="float"/></accessor></technique_common></source>
<source id="t"><float_array id="ta" count="8">0 0 1 0 1 1 0 1</float_array><technique_common><accessor source="#ta" count="4" stride="2"><param name="S" type="float"/><param name="T" type="float"/></accessor></technique_common></source>
<vertices id="v"><input semantic="POSITION" source="#p"/></vertices>
<triangles material="%s" count="2"><input semantic="VERTEX" source="#v" offset="0"/><input semantic="NORMAL" source="#n" offset="1"/><input semantic="TEXCOORD" source="#t" offset="2" set="0"/><p>0 0 0 1 0 1 2 0 2 0 0 0 2 0 2 3 0 3</p></triangles>
</mesh></geometry></library_geometries>
<library_visual_scenes><visual_scene id="s"><node id="base00" name="base00" type="NODE"><node id="start01" name="start01" type="NODE"><node id="card_a2" name="card_a2" type="NODE"><instance_geometry url="#card" name="card_a2"><bind_material><technique_common><instance_material symbol="%s" target="#%s"/></technique_common></bind_material></instance_geometry></node></node></node></visual_scene></library_visual_scenes>
<scene><instance_visual_scene url="#s"/></scene></COLLADA>
]]
local function cardShape(avatar)
  if cardShapes[avatar] then return cardShapes[avatar] end
  local safe = avatar:gsub("[^%w]", "")
  local mat = "rr_card_" .. safe
  local path = MEDIA .. "cards/" .. safe .. "_v2.dae"
  writeFile(path, string.format(CARD, mat, mat, mat, mat, mat))
  material(mat, MEDIA .. avatar .. ".png", nil, true)
  cardShapes[avatar] = path
  return path
end

local function newStatic(shape, moving)
  local o = createObject("TSStatic")
  o:setField("shapeName", 0, shape)
  o:setField("collisionType", 0, "None")
  o:setField("decalType", 0, "None")
  -- roadside people: instanced (needs updateInstanceRenderData after a move). Seated people move every frame with the
  -- car: the plain render path draws them where they were put this frame (instanced data showed up frames late)
  o.useInstanceRenderData = not moving
  o.canSave = false
  o:registerObject(Sim.getUniqueName("rr_passenger"))
  scenetree.MissionGroup:addObject(o)
  return o
end
local function drop(list)
  for _, s in ipairs(list) do if s.obj then pcall(function() s.obj:delete() end) end end
  for i = #list, 1, -1 do list[i] = nil end
end

-- standing people: positions in lat / lon (game metres on regular maps), turned to the car every frame
local function setStandees(people)
  for i, p in ipairs(people) do
    local shape = modelShape(p.avatar) or cardShape(p.avatar)
    local s = standees[i]
    if s and s.shape ~= shape then pcall(function() s.obj:delete() end); s = nil end
    if not s then s = {obj = newStatic(shape), shape = shape}; standees[i] = s end
    s.lat, s.lon, s.pos = p.lat, p.lon, nil
  end
  for i = #standees, #people + 1, -1 do
    pcall(function() standees[i].obj:delete() end); standees[i] = nil
  end
end
local function placeStandees(a, vp)
  for _, s in ipairs(standees) do
    if not s.pos then
      local x, y = a.toGame(s.lat, s.lon, 0)
      local g = x and be:getSurfaceHeightBelow(vec3(x, y, (vp and vp.z or 0) + 60))
      if g and g > -1e9 then s.pos = vec3(x, y, g) end
    end
    if s.pos then
      local dir = vp and (vp - s.pos) or vec3(0, 1, 0)
      dir.z = 0
      if dir:length() < 0.1 then dir = vec3(0, 1, 0) end
      local rot = quatFromDir(dir:normalized(), vec3(0, 0, 1))   -- the front (+Y) towards the car
      s.obj:setPosRot(s.pos.x, s.pos.y, s.pos.z, rot.x, rot.y, rot.z, rot.w)
      s.obj:updateInstanceRenderData()   -- without this the drawn copy stays where it was (seen after Ctrl+R)
    end
  end
end

-- seated people in cars without BeamNG's seat system (or without an occupant part, see "people as the car's own seat
-- occupants" below): a 3D model placed at the seat every frame. Each seat slot has a camera "occupant.<seat>" at its
-- eye point; the vehicle reports those once (onSeatCams). A BeamNG "dummy" occupant takes the seat (weight, seat
-- taken) and BeamNG's mannequin is hidden in the seats used here (hideOurMannequins); BeamNG's own dummy occupants
-- in other seats stay as they are. Without seats at all, the front passenger seat mirrors the driver's camera.
-- (BeamNG keeps them in v.data.cameras.occupant[seatId] now; older vehicles: camerasInternal / cameras.onboard)
local SEAT_QUERY = [[
local out = {}
local cams = v.data.cameras or {}
local function add(name, c)
  if c and c.camNodeID then
    local p = obj:getNodePosition(c.camNodeID)
    out[#out + 1] = {name, p.x, p.y, p.z, c.camNodeID}
  end
end
for seatId, c in pairs(cams.occupant or {}) do add("occupant." .. seatId, c) end
add("driver", cams.driver)
for _, c in pairs(cams.onboard or {}) do if c.name == "driver" then add("driver", c) end end
for _, c in pairs(v.data.camerasInternal or {}) do if c.name then add(c.name, c) end end
obj:queueGameEngineLua("if realroutes then realroutes.onSeatCams(" .. serialize(out) .. ") end")
]]
local seated = {}         -- seatId -> {obj, avatar, shape}
local seatedVeh = nil
local camLocal = {}       -- vehicle id -> {[camera name] = vehicle-local eye point}
local function occ() return rawget(_G, "gameplay_seatOccupants") end

local function seatList(vid)
  local o = occ()
  if o and o.usesSeatSystem and o.usesSeatSystem(vid) then return o.getSeats(vid) or {} end
  return {}
end

-- hide BeamNG's mannequin in the seats holding placed models (re-applied: BeamNG re-shows occupant meshes when it syncs)
local hideTimer = 0
local function hideOurMannequins(v)
  local vd = core_vehicle_manager and core_vehicle_manager.getVehicleData(v:getId())
  local vdata = vd and vd.vdata
  if not vdata then return end
  local paths = {}
  for _, st in ipairs(seatList(v:getId())) do
    if seated[st.seatId] and seated[st.seatId].obj and st.partPath then paths[st.partPath] = true end
  end
  local function hide(mesh)
    if mesh.mesh and paths[mesh.partPath] then
      local kind = mesh.originalMesh or mesh.mesh
      if kind == "character_torso" or kind == "character_head" then v:setMeshAlpha(0, mesh.mesh) end
    end
  end
  for _, f in pairs(vdata.flexbodies or {}) do hide(f) end
  for _, pr in pairs(vdata.props or {}) do hide(pr) end
end

-- the car's own frame from its physics (ref node) heading: v:getRotation() is the object's transform, which does not
-- follow the body as it turns (passengers swung out of the car in corners). Car space: -Y forward, +Z up.
local function carRot(v)
  local fwd, up = vec3(v:getDirectionVector()), vec3(v:getDirectionVectorUp())
  return quatFromDir(-fwd, up)
end

local camNode = {}   -- [vid][name] = the seat camera's node id: its world position each frame

M.onSeatCams = function(list)
  local v = player()
  if not v then return end
  local rot = carRot(v):inversed()
  local c, nodes, n = {}, {}, 0
  for _, e in ipairs(list or {}) do c[e[1]] = rot * vec3(e[2], e[3], e[4]); nodes[e[1]] = e[5]; n = n + 1 end
  if n > 0 then camLocal[v:getId()] = c; camNode[v:getId()] = nodes end   -- nothing found: ask again next time
end


local function seatEye(vid, seatId)
  local c = camLocal[vid]
  if not c then return nil end
  if c["occupant." .. seatId] then return c["occupant." .. seatId] end
  if seatId == "_mirror" and c.driver then return vec3(-c.driver.x, c.driver.y, c.driver.z) end
  return nil
end

-- ---- people as the car's own seat occupants ----
-- BeamNG's seat dummy is a part ("occupant_generic", slot type seat_occupant) that every seat slot uses: its mesh
-- hangs on the seat's own nodes, so the car draws and carries it. Each 3D passenger gets a copy of that part (made
-- here from the game's own file, so it follows game updates) with the person's seated mesh instead of the dummy:
-- "occupant_generic_rr_<name>" (the seat system only treats parts named occupant_generic* as seats). Putting
-- someone in a seat = choosing that part for the seat's slot (the car reloads in place, about a second).
-- BeamDeck copies the meshes to /vehicles/common/realroutes_people/<name>/ (vehicle.dae, textures, materials).
local PEOPLE = "/vehicles/common/realroutes_people/"
-- seat.json (from the exporter): the shift that puts the person's eyes on the seat's camera point
local STOCK = "occupant_generic"
local function safeName(avatar) return (tostring(avatar):gsub("[^%w]", "")) end
local function partName(avatar) return STOCK .. "_rr_" .. safeName(avatar) end

local function makePart(avatar)
  local dir = PEOPLE .. avatar .. "/"
  if not FS:fileExists(dir .. "vehicle.dae") then return nil end
  local src = readFile("/vehicles/common/passengers/occupant_generic.jbeam")
  if not src then log("W", "realroutes", "no occupant_generic.jbeam to copy"); return nil end
  local safe, name = safeName(avatar), partName(avatar)
  local off = ((jsonReadFile(dir .. "seat.json") or {}).offset) or {x = 0, y = 0, z = 0}
  local offTxt = string.format('{"x":%.3f, "y":%.3f, "z":%.3f}', off.x or 0, off.y or 0, off.z or 0)
  local out, swapped = {}, false
  for line in (src .. "\n"):gmatch("(.-)\r?\n") do
    if line:find('"occupant_generic": {', 1, true) or line:find('"occupant_generic":{', 1, true) then
      line = line:gsub('"occupant_generic"', '"' .. name .. '"', 1)
    elseif line:find('"Seat Occupant"', 1, true) then
      line = line:gsub('"Seat Occupant"', '"' .. avatar .. ' (Real Routes)"', 1)
    elseif line:find('["character_torso"', 1, true) then
      line = nil                                  -- the dummy's soft torso: a far-reaching body bends badly as one
    elseif line:find('"character_head"', 1, true) then
      -- the person as rigid props on the occupant's hip and chest nodes, in the car's own axes (the mesh is made in
      -- the occupant frame: cushion at 0, -Y forward); body and head apart so the driver's view can drop the head
      local function prop(mesh)
        -- on the seat itself (cushion front left / right, backrest), not the occupant's springy body nodes: the
        -- person stays put on the seat instead of swaying with every bump; the occupant's nodes if a seat lacks them
        return '        ["nop", "' .. mesh .. '", ' ..
          '"$= $occupant_attachnodeFL and $occupant_attachnodeFR and $occupant_attachnodeTL and $occupant_attachnodeFL or (\'ocp\'..$seatID..\'1\')", ' ..
          '"$= $occupant_attachnodeFL and $occupant_attachnodeFR and $occupant_attachnodeTL and $occupant_attachnodeFR or (\'ocp\'..$seatID..\'2l\')", ' ..
          '"$= $occupant_attachnodeFL and $occupant_attachnodeFR and $occupant_attachnodeTL and $occupant_attachnodeTL or (\'ocp\'..$seatID..\'3\')", ' ..
          '{"x":0, "y":0, "z":0}, {"x":0, "y":0, "z":0}, {"x":0, "y":0, "z":0}, 0, 1, 0, 1, ' ..
          '{"baseRotationGlobal":{"x":0, "y":0, "z":0}, "baseTranslationGlobal":' .. offTxt .. '}],'
      end
      line = prop("rr_" .. safe .. "_body") .. "\n" .. prop("rr_" .. safe .. "_head")
      swapped = true
    end
    if line then out[#out + 1] = line end
  end
  if not swapped then log("W", "realroutes", "occupant part changed shape: not made for " .. avatar); return nil end
  local text = table.concat(out, "\n")
  local file = dir .. "occupant_rr.jbeam"
  if readFile(file) ~= text then writeFile(file, text); M._partsRewritten = true end
  return name
end

-- the parts-tree node of a seat's occupant slot
local function occupantNode(vid, seatId)
  local vd = core_vehicle_manager.getVehicleData(vid)
  local tree = vd and vd.config and vd.config.partsTree
  if not tree then return nil end
  local want
  for _, st in ipairs(seatList(vid)) do if st.seatId == seatId then want = st.partPath end end
  local found
  local function walk(n)
    if found or not n then return end
    for _, c in pairs(n.children or {}) do
      local chosen = tostring(c.chosenPartName or "")
      if chosen:find(STOCK, 1, true) == 1 and ((want and c.partPath == want) or
          (not want and tostring(c.id or ""):match("_" .. seatId .. "$"))) then
        found = c; return
      end
      walk(c)
    end
  end
  walk(tree)
  if not found and want then                       -- partPath spelled differently: the slot named after the seat
    local function walk2(n)
      if found or not n then return end
      for _, c in pairs(n.children or {}) do
        if tostring(c.chosenPartName or ""):find(STOCK, 1, true) == 1 and tostring(c.id or ""):match("_" .. seatId .. "$") then
          found = c; return
        end
        walk2(c)
      end
    end
    walk2(tree)
  end
  return found, vd
end

-- choose occupant parts (seatId -> part name) and reload the car once
local function applyParts(vid, changes)
  if not next(changes) and not M._partsRewritten then return end
  local vd, changed = nil, false
  for sid, part in pairs(changes) do
    local node, d = occupantNode(vid, sid)
    vd = vd or d
    if node and node.chosenPartName ~= part then node.chosenPartName = part; changed = true
    elseif not node then log("W", "realroutes", "no occupant slot found for seat " .. tostring(sid)) end
  end
  if M._partsRewritten then changed = true; M._partsRewritten = nil end   -- a part file changed: reload anyway
  if changed and vd then
    core_vehicle_partmgmt.setConfig({partsTree = deepcopy(vd.config.partsTree)}, true, vid)
  end
end

-- who sits where, from the car's parts (survives reloading this extension)
local function syncFromParts(vid)
  for _, st in ipairs(seatList(vid)) do
    local node = occupantNode(vid, st.seatId)
    local chosen = node and tostring(node.chosenPartName or "") or ""
    local safe = chosen:match("^" .. STOCK .. "_rr_(.+)$")
    if safe and not (seated[st.seatId] and seated[st.seatId].part) then
      local avatar = safe
      for _, f in ipairs(FS:findFiles(PEOPLE, "vehicle.dae", -1, true, false) or {}) do
        local d = f:match("realroutes_people/([^/]+)/vehicle%.dae$")
        if d and safeName(d) == safe then avatar = d end
      end
      seated[st.seatId] = {avatar = avatar, part = chosen, driver = st.isDriver or nil}
      seatedVeh = vid
    elseif not safe and seated[st.seatId] and seated[st.seatId].part then
      seated[st.seatId] = nil
    end
  end
end

local function putSeat(seatId, avatar, batch)
  local v = player()
  if not v then return end
  local vid = v:getId()
  local changes = batch or {}
  if seatedVeh and seatedVeh ~= vid then         -- another car now: let the old one go (its parts stay as they are)
    for sid, st in pairs(seated) do
      if st.obj then pcall(function() st.obj:delete() end) end
      if st.obj2 then pcall(function() st.obj2:delete() end) end
      if (st.obj or st.dummy) and occ() and not st.driver then pcall(occ().clearDummyOccupant, seatedVeh, sid) end
      seated[sid] = nil
    end
  end
  seatedVeh = vid
  syncFromParts(vid)
  if not camLocal[vid] then v:queueLuaCommand(SEAT_QUERY) end
  local old = seated[seatId]
  if old then
    if old.obj then pcall(function() old.obj:delete() end) end
    if old.obj2 then pcall(function() old.obj2:delete() end) end
    seated[seatId] = nil
    if old.part then changes[seatId] = STOCK end
    if (old.obj or old.dummy) and occ() and seatId ~= "_mirror" and not old.driver then pcall(occ().clearDummyOccupant, vid, seatId) end
  end
  local isDriver = false
  for _, st in ipairs(seatList(vid)) do if st.seatId == seatId and st.isDriver then isDriver = true end end
  local part = avatar and seatId ~= "_mirror" and makePart(avatar)
  if part then
    seated[seatId] = {avatar = avatar, part = part, driver = isDriver or nil}
    changes[seatId] = part
  else
    local shape = avatar and modelShape(avatar, "seated")
    if shape then                                  -- no car part for this person (or no seat system): placed each frame
      local st = {obj = newStatic(shape, true), avatar = avatar, shape = shape, driver = isDriver or nil}
      if isDriver then
        local body = modelShape(avatar, "seated_driver")
        if body then st.obj2 = newStatic(body, true) end
      elseif occ() and seatId ~= "_mirror" then
        pcall(occ().setDummyOccupant, vid, seatId, 58)
      end
      seated[seatId] = st
    elseif avatar and occ() and seatId ~= "_mirror" and not isDriver then
      -- someone without a 3D model (a built-in person, or a picture only): BeamNG's own dummy takes the seat
      pcall(occ().setDummyOccupant, vid, seatId, 58)
      seated[seatId] = {avatar = avatar, dummy = true}
    end
  end
  hideTimer = 0
  if not batch then
    applyParts(vid, changes)
    if occ() then pcall(occ().refreshOccupantMeshes, vid) end
  end
end

-- a ride's people into the free passenger seats (front first); one car reload for all of them
local function setSeated(people)
  local v = player()
  if not v then return end
  local batch = {}
  syncFromParts(v:getId())
  for sid, st in pairs(seated) do if not st.driver then putSeat(sid, nil, batch) end end
  local free = {}
  for _, st in ipairs(seatList(v:getId())) do
    if not st.isDriver and (not st.occupied or batch[st.seatId]) then free[#free + 1] = st.seatId end
  end
  table.sort(free, function(x, y)
    local fx, fy = tostring(x):find("F") and 0 or 1, tostring(y):find("F") and 0 or 1
    if fx ~= fy then return fx < fy end
    return tostring(x) < tostring(y)
  end)
  if #free == 0 then free = {"_mirror"} end
  for i, avatar in ipairs(people or {}) do
    if free[i] then putSeat(free[i], avatar, batch) end
  end
  applyParts(v:getId(), batch)
  if occ() then pcall(occ().refreshOccupantMeshes, v:getId()) end
end

local function placeSeated()
  if not next(seated) then return end
  local v = player()
  if not v or v:getId() ~= seatedVeh then return end
  local pos, rot = vec3(v:getPosition()), carRot(v)
  hideTimer = hideTimer - 1
  if hideTimer <= 0 then hideTimer = 20; hideOurMannequins(v) end
  local face = rot * quatFromDir(vec3(0, -1, 0), vec3(0, 0, 1))   -- model front (+Y) to the car's front (-Y)
  if not camLocal[seatedVeh] then
    if (M._askAt or 0) < os.clockhp() then M._askAt = os.clockhp() + 0.5; v:queueLuaCommand(SEAT_QUERY) end
  end
  for sid, st in pairs(seated) do
    if st.part and st.driver then
      -- the driver as a car part: no head in the driver's own first-person view (the camera is at the eyes)
      -- the driver's own seat view ("driver", or the newer "occupant" seat cameras)
      local cam = tostring(core_camera and core_camera.getActiveCamName(0) or "")
      local fp = cam == "driver" or cam:find("occupant", 1, true) ~= nil or cam == "onboard"
      if st.fp ~= fp then
        st.fp = fp
        local vd = core_vehicle_manager.getVehicleData(seatedVeh)
        local want = "rr_" .. safeName(st.avatar) .. "_head"
        for _, list in ipairs({vd and vd.vdata and vd.vdata.flexbodies or {}, vd and vd.vdata and vd.vdata.props or {}}) do
          for _, f in pairs(list) do
            if (f.originalMesh or f.mesh) == want or tostring(f.mesh or ""):find(want, 1, true) == 1 then
              v:setMeshAlpha(fp and 0 or 1, f.mesh)
            end
          end
        end
      end
    end
    local eye = st.obj and seatEye(seatedVeh, sid)
    if eye then
      local nodes = camNode[seatedVeh] or {}
      local nid = nodes["occupant." .. sid]
      local eyeW = nid and (pos + vec3(v:getNodePosition(nid))) or (pos + rot * eye)   -- the seat node itself: moves with the body
      local w = eyeW + rot * vec3(0, 0.06, -0.64)          -- hips below and a little behind the eyes (on the cushion)
      local fp = st.obj2 and core_camera and core_camera.getActiveCamName(0) == "driver"
      for _, o in ipairs({st.obj, st.obj2}) do
        o:setPosRot(w.x, w.y, w.z, face.x, face.y, face.z, face.w)
        if o.useInstanceRenderData then o:updateInstanceRenderData() end
      end
      if st.obj2 and st.fp ~= fp then
        st.fp = fp; st.obj:setHidden(fp and true or false); st.obj2:setHidden(not fp)
      end
    end
  end
end

-- the seats for the tablet (manual seating outside rides)
local function seatsReport()
  local v = player()
  if not v then send({type = "seats", seats = {}}); return end
  syncFromParts(v:getId())
  local out = {}
  for _, st in ipairs(seatList(v:getId())) do
    out[#out + 1] = {id = st.seatId, name = st.displayName, driver = st.isDriver or nil,
                     taken = (st.occupied and not st.isDriver and not seated[st.seatId]) or nil,
                     who = seated[st.seatId] and seated[st.seatId].avatar or nil}
  end
  if #out == 0 then out[1] = {id = "_mirror", name = "Front passenger", who = seated._mirror and seated._mirror.avatar or nil} end
  send({type = "seats", model = v:getJBeamFilename(), seats = out})
end

-- ---- commands from the helper ----
local handlers = {}
handlers.goto = doGoto
handlers.line = function(c)
  lineInfo = {routeID = c.routeID, direction = c.direction, routeColor = c.routeColor, tasklist = c.tasklist,
              variance = "a"}
  busEvent("bus_setLineInfo", lineInfo)
end
handlers.atStop = function(c) busEvent("bus_onAtStop", {triggerName = c.trigger}) end
handlers.departed = function(c) busEvent("bus_onDepartedStop", {triggerName = c.trigger}) end
handlers.stopRequest = function(c)
  if c.on ~= lastStopReq then
    lastStopReq = c.on
    busEvent("bus_onSetStopRequest", c.on and true or false)
  end
end
handlers.gps = function(c)
  local a = api()
  if a and a.gpsSetRoute and c.points and #c.points > 0 then
    local ok, why = a.gpsSetRoute(c.points)
    if not ok then log("W", "realroutes", "GPS route refused: " .. tostring(why)) end
  end
end
handlers.gpsClear = function() local a = api(); if a and a.gpsClear then a.gpsClear() end end
handlers.gpsTo = function(c)
  local a = api()
  if a and a.gpsSet then a.gpsSet(c.lat, c.lon, c.name) end
end
-- Rides: real places round a point, answered back to the helper (twice when the app fetched missing map cells)
handlers.places = function(c)
  local a = api()
  if not (a and a.has and a.has("places")) then
    send({type = "places", id = c.id, places = {}, complete = true, error = "BeamNG.Earth's places API isn't available"})
    return
  end
  local opts = {radius = c.radius, categories = c.categories, limit = c.limit, minConfidence = 0.6}
  if c.cachedOnly then opts.cachedOnly = true end
  local ok, why = a.places(c.lat, c.lon, opts, function(r)
    local out = {}
    for _, p in ipairs(r.places or {}) do
      out[#out + 1] = {id = p.id, name = p.name, category = p.category, lat = p.lat, lon = p.lon,
                       open = p.open, confidence = p.confidence}
    end
    send({type = "places", id = c.id, places = out, complete = r.complete, pending = r.pending, error = r.error})
  end)
  if not ok then send({type = "places", id = c.id, places = {}, complete = true, error = tostring(why)}) end
end
handlers.markers = function(c) markers = c.stops or {} end
handlers.doors = function() vehLua('local c = controller.getController("bus") if c then c.toggleDoors() end') end
handlers.kneel = function() vehLua('local c = controller.getController("bus") if c then c.toggleKneel() end') end
handlers.hazards = function() vehLua("electrics.toggle_warn_signal()") end
handlers.notify = function(c) message(c.text, c.secs) end
handlers.standee = function(c)
  if not c.show then setStandees({}); return end
  setStandees(c.people or {{avatar = c.avatar, lat = c.lat, lon = c.lon}})
end
handlers.seated = function(c) setSeated(c.people or {}) end
handlers.seatPut = function(c) putSeat(c.seat, c.avatar); seatsReport() end
handlers.seats = function() seatsReport() end
-- what is in the world right now (for troubleshooting): each standing / seated passenger and its material
handlers.debugPeople = function()
  local v = player()
  local vp = v and vec3(v:getPosition())
  local out = {}
  for i, st in ipairs(standees) do
    local ok, p = pcall(function() return vec3(st.obj:getPosition()) end)
    local safe = st.shape:match("cards/(.-)%.dae")
    out[#out + 1] = {i = i, shape = st.shape, placed = st.pos ~= nil, lat = st.lat, lon = st.lon,
                     at = ok and {p.x, p.y, p.z} or nil, dist = (ok and vp) and (p - vp):length() or nil,
                     hidden = (pcall(function() return st.obj:isHidden() end)) and st.obj:isHidden() or nil,
                     mat = safe and (scenetree.findObject("rrm_rr_card_" .. safe) ~= nil) or nil,
                     shapeFile = FS:fileExists(st.shape)}
  end
  local seats = {}
  for sid, st in pairs(seated) do
    local ok, p = pcall(function() return vec3(st.obj:getPosition()) end)
    local eye = seatedVeh and seatEye(seatedVeh, sid)
    seats[#seats + 1] = {seat = sid, avatar = st.avatar, at = ok and {p.x, p.y, p.z} or nil,
                         dist = (ok and vp) and (p - vp):length() or nil, eye = eye and {eye.x, eye.y, eye.z} or nil}
  end
  local cams = {}
  for name in pairs(v and camLocal[v:getId()] or {}) do cams[#cams + 1] = name end
  send({type = "debug", standees = out, seated = seats, car = vp and {vp.x, vp.y, vp.z} or nil,
        seatedVeh = seatedVeh, vehId = v and v:getId() or nil, cams = cams})
end

local function receive()
  if not udp then return end
  for _ = 1, 50 do
    local data = udp:receive()
    if not data then return end
    local ok, c = pcall(jsonDecode, data)
    if ok and type(c) == "table" and handlers[c.cmd] then
      local ok2, err = pcall(handlers[c.cmd], c)
      if not ok2 then log("E", "realroutes", c.cmd .. ": " .. tostring(err)) end
    end
  end
end

-- ---- stop markers (the next stop and the two after it) ----
local colNext, colLater = ColorF(0.95, 0.15, 0.2, 0.55), ColorF(1, 1, 1, 0.25)
local colPickup, colDrop = ColorF(0.2, 0.85, 0.45, 0.6), ColorF(0.95, 0.3, 0.75, 0.6)
local textCol, textBg = ColorF(1, 1, 1, 1), ColorI(0, 0, 0, 170)
local function drawMarkers(a)
  if #markers == 0 or (a.rebasing and a.rebasing()) then return end
  local v = player()
  local vp = v and vec3(v:getPosition())
  for _, m in ipairs(markers) do
    if not m.pos then
      local x, y = a.toGame(m.lat, m.lon, 0)
      if x and vp then
        local g = be:getSurfaceHeightBelow(vec3(x, y, vp.z + 60))
        if g and g > -1e9 then m.pos = vec3(x, y, g) end
      end
    end
    if m.pos and (not vp or (m.pos - vp):length() < 1500) then
      local top = m.pos + vec3(0, 0, m.next and 5 or 3)
      local col = m.kind == "pickup" and colPickup or m.kind == "dropoff" and colDrop or (m.next and colNext or colLater)
      debugDrawer:drawCylinder(m.pos, top, m.next and 0.5 or 0.3, col)
      local label = m.kind and m.name or ((m.next and "Next stop: " or "") .. m.name .. ((m.code or "") ~= "" and ("  #" .. m.code) or ""))
      debugDrawer:drawTextAdvanced(top + vec3(0, 0, 0.6), String(label), textCol, true, false, textBg)
    end
  end
end

-- ---- every frame ----
local function onUpdate(dtReal)
  receive()
  local a = api()
  if pendingGoto and os.clockhp() >= pendingGoto.at then
    local g = pendingGoto
    if g.stage == "teleport" and a then
      local ok, why = a.teleport(g.lat, g.lon, g.name)
      if ok == false then log("W", "realroutes", "teleport refused: " .. tostring(why)); pendingGoto = nil
      else g.stage = "wait"; g.at = os.clockhp() + 60 end   -- onBeamNGEarthTeleported moves it on
    elseif g.stage == "wait" then
      pendingGoto = nil                                     -- gave up waiting
    end
  end
  if lineAgainAt and os.clockhp() >= lineAgainAt then
    lineAgainAt = nil
    if lineInfo then busEvent("bus_setLineInfo", lineInfo) end
  end
  if a then
    drawMarkers(a)
    if #standees > 0 and not (a.rebasing and a.rebasing()) then
      local v = player()
      placeStandees(a, v and vec3(v:getPosition()) or nil)
    end
  end

  sendTimer = sendTimer + dtReal
  if sendTimer < SEND_EVERY then return end
  sendTimer = 0
  vehLua(QUERY)
  helloTimer = helloTimer - SEND_EVERY
  if helloTimer <= 0 then
    helloTimer = 5
    local isFlat = a == flat
    send({type = "hello", earth = a and a.earthVersion and a.earthVersion() or nil, mapPort = mapPort(),
          flat = isFlat or nil, level = isFlat and getCurrentLevelIdentifier() or nil,
          roads = isFlat and exportRoads() or nil})
  end
  if not (a and a.ready and a.ready()) then return end
  local p = a.pose()
  if not p then return end
  local lim = a.speedLimit and a.speedLimit()
  local g = a.gps and a.gps()
  sendRoute(a)
  send({type = "pose", lat = p.lat, lon = p.lon, heading = p.heading, speed = p.speed or 0,
        gpsLeft = g and g.remaining or nil, gpsEta = g and g.eta or nil, gpsOff = g and g.off or nil,
        model = (function() local v = player(); return v and v:getJBeamFilename() or nil end)(),
        place = a.place and a.place() or nil, gpsName = g and g.name or nil,
        doors = (veh.d or 0) > 0.5, kneel = (veh.k or 0) > 0.5, gx = veh.gx, gy = veh.gy, bus = veh.bus,
        ready = true, limit = lim and lim.ms or nil, earth = a.earthVersion and a.earthVersion() or nil,
        flat = (a == flat) or nil, lights = (veh.l or 0) > 0})
end

-- ---- BeamNG.Earth hooks ----
M.onBeamNGEarthTeleported = function()
  if pendingGoto and pendingGoto.stage == "wait" then
    local g = pendingGoto
    pendingGoto = nil
    placeBus(g)
    lineAgainAt = os.clockhp() + 1
  end
  for _, m in ipairs(markers) do m.pos = nil end
end
M.onBeamNGEarthRebased = function()
  for _, m in ipairs(markers) do m.pos = nil end
  for _, st in ipairs(standees) do st.pos = nil end
end
M.onBeamNGEarthWorldReady = function() for _, m in ipairs(markers) do m.pos = nil end; helloTimer = 0 end

-- a new bus (or a reset) needs its sign again
M.onVehicleSpawned = function() lineAgainAt = os.clockhp() + 1 end
M.onVehicleSwitched = function() lineAgainAt = os.clockhp() + 1 end
M.onVehicleResetted = function() lineAgainAt = os.clockhp() + 1 end

M.onExtensionLoaded = function()
  openSocket(); helloTimer = 0
  -- people left in the world by an earlier load of this extension (a reload forgets them): remove
  for _, name in ipairs(scenetree.findClassObjects("TSStatic") or {}) do
    if tostring(name):find("^rr_passenger") then
      local o = scenetree.findObject(name)
      if o then pcall(function() o:delete() end) end
    end
  end
end
M.onExtensionUnloaded = function()
  drop(standees)
  for _, st in pairs(seated) do                   -- placed models go; car parts stay (picked up again on load)
    if st.obj then pcall(function() st.obj:delete() end) end
    if st.obj2 then pcall(function() st.obj2:delete() end) end
  end
  if udp then udp:close(); udp = nil end
end
M.onUpdate = onUpdate
-- after the frame's physics: the car is where it will be drawn, so passengers don't trail behind it
M.onPreRender = function() if api() then placeSeated() end end

return M
