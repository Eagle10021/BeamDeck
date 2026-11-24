-- Real Routes: drive real bus routes on BeamNG.Earth. Thin game side; the helper (realroutes_helper.py) runs the shift.
--   sends   -> 127.0.0.1:47151  {type="pose"}: position, speed, doors, g-forces at 10 Hz; {type="hello"}
--   listens <- 127.0.0.1:47150  commands: goto, line, atStop, departed, stopRequest, gps, gpsClear, markers,
--                               doors, kneel, hazards, notify
-- Needs BeamNG.Earth (its modding API gives real coordinates). The bus displays work on the stock city bus.
local M = {}

local MOD_VERSION = 8              -- the helper (freight.py MOD_REQUIRED) warns when the game runs an older copy of this file

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
flat.teleport = function() return true end   -- onUpdate places the bus directly on a regular map
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

local function tr(s)
  if type(s) ~= "string" or s == "" then return s end
  if translateLanguage then
    local ok, t = pcall(translateLanguage, s, s, true)
    if ok and t then return t end
  end
  return s
end
-- the level's freight places (regular maps), best source first: BeamNG's own delivery businesses (the career's
-- facilities: warehouses, shops, fuel stations, trailer yards, with what each ships and takes), then the big map's
-- points of interest, then spots spread over the AI road network, so a map with roads but neither of those still
-- has places to haul between. Written once per level; the path goes in hello (freightSites).
local sitesFor, sitesFile = nil, nil
local COMPASS = {"North", "North-east", "East", "South-east", "South", "South-west", "West", "North-west"}
local ROAD_ROLES = {"Depot", "Yard", "Warehouse", "Industrial park", "Distribution centre", "Works", "Terminal", "Supply yard"}
local function prettyId(s)
  s = tostring(s or ""):gsub("(%l)(%u)", "%1 %2"):gsub("[_%-]+", " ")
  return (s:gsub("^%l", string.upper))
end
local function facilitySites(lvl)
  local out, spots = {}, {}
  local dir = "/levels/" .. lvl .. "/facilities/"
  local ok, files = pcall(function() return FS:findFiles(dir, "*.sites.json", -1, true, false) end)
  for _, f in ipairs(ok and files or {}) do
    local d = jsonReadFile(f)
    for _, ps in ipairs(d and d.parkingSpots or {}) do
      if ps.name and type(ps.pos) == "table" then spots[ps.name] = ps.pos end
    end
  end
  local ok2, ffiles = pcall(function() return FS:findFiles(dir, "*.facilities.json", -1, true, false) end)
  for _, f in ipairs(ok2 and ffiles or {}) do
    local d = jsonReadFile(f)
    local group = f:match("([^/]+)%.facilities%.json$") or ""
    for _, pr in ipairs(d and d.deliveryProviders or {}) do
      local types, gives, takes, best, any = {}, false, false, nil, nil
      for _, ap in ipairs(pr.manualAccessPoints or {}) do
        local pos = spots[ap.psName]
        if pos then
          any = any or pos
          for _, t in ipairs(ap.logisticTypesProvided or {}) do types[t] = true; gives = true end
          for _, t in ipairs(ap.logisticTypesReceived or {}) do types[t] = true; takes = true end
          if tostring(ap.psName):lower():find("trailer", 1, true) then best = pos end   -- a trailer yard suits a truck
        end
      end
      local pos = best or any
      if pos then
        local name = tr(pr.name)
        if not name or name == pr.name or name:find("^ui%.") then name = prettyId(pr.id) end
        local tl = {}
        for t in pairs(types) do tl[#tl + 1] = t end
        local lat, lon = toFlat(vec3(pos[1], pos[2], pos[3] or 0))
        out[#out + 1] = {name = name, lat = lat, lon = lon, z = pos[3], group = group, types = tl, gives = gives,
                         takes = takes, source = "facility"}
      end
    end
  end
  return out
end
local function poiSites(lvl)
  local out = {}
  local ok, list = pcall(function() return gameplay_rawPois and gameplay_rawPois.getRawPoiListByLevel(lvl) end)
  for _, e in ipairs(ok and list or {}) do
    local bm = e.markerInfo and e.markerInfo.bigmapMarker
    if bm and bm.pos and bm.name then
      local bp = vec3(bm.pos)
      local lat, lon = toFlat(bp)
      out[#out + 1] = {name = tr(bm.name), lat = lat, lon = lon, z = bp.z, group = e.data and e.data.type or "place",
                       types = {}, source = "poi"}
    end
  end
  return out
end
-- spots spread over the road network: the node furthest from every spot already picked, again and again
local function roadSites(m, have, want)
  local nodes = {}
  for _, n in pairs(m.nodes or {}) do
    local links = 0
    for _ in pairs(n.links or {}) do links = links + 1 end
    if links >= 1 and (n.radius or 0) >= 2.5 then nodes[#nodes + 1] = n.pos end
  end
  if #nodes < 2 then return {} end
  local cx, cy = 0, 0
  for _, p in ipairs(nodes) do cx, cy = cx + p.x, cy + p.y end
  cx, cy = cx / #nodes, cy / #nodes
  local picked = {}
  for _, s in ipairs(have) do picked[#picked + 1] = vec3(s.lon * K_LON, s.lat * K_LAT, 0) end
  local out, used = {}, {}
  for k = 1, want do
    local bestD, bestP = -1, nil
    for _, p in ipairs(nodes) do
      local dmin = math.huge
      for _, q in ipairs(picked) do
        local d = (p.x - q.x) ^ 2 + (p.y - q.y) ^ 2
        if d < dmin then dmin = d end
      end
      if #picked == 0 then dmin = -((p.x - cx) ^ 2 + (p.y - cy) ^ 2) end   -- the first one: the middle of the map
      if dmin > bestD then bestD, bestP = dmin, p end
    end
    if not bestP or (#picked > 0 and bestD < 300 * 300) then break end    -- closer than 300 m to another: enough
    picked[#picked + 1] = bestP
    local ang = math.deg(math.atan2(bestP.x - cx, bestP.y - cy)) % 360
    local near = math.sqrt((bestP.x - cx) ^ 2 + (bestP.y - cy) ^ 2) < 400
    local base = (near and "Central" or COMPASS[math.floor((ang + 22.5) / 45) % 8 + 1]) .. " " .. ROAD_ROLES[(k - 1) % #ROAD_ROLES + 1]
    used[base] = (used[base] or 0) + 1
    local lat, lon = toFlat(bestP)
    out[#out + 1] = {name = used[base] > 1 and (base .. " " .. used[base]) or base, lat = lat, lon = lon, z = bestP.z,
                     group = "road", types = {}, source = "road"}
  end
  return out
end
-- the map's named spawn points (the spots its makers named: lookouts, summits, canyon floors...), for the challenge
-- routes (climbs and descents between them) and as places on maps with few others
local function spawnSites(lvl)
  local out = {}
  local info = core_levels and core_levels.getLevelByName and core_levels.getLevelByName(lvl)
  for _, sp in ipairs(info and info.spawnPoints or {}) do
    local o = sp.objectname and scenetree.findObject(sp.objectname)
    if o then
      local p = vec3(o:getPosition())
      local name = tr(sp.translationId)
      if not name or name == sp.translationId or name:find("^levels%.") then name = prettyId(sp.objectname:gsub("^spawn_", "")) end
      local lat, lon = toFlat(p)
      out[#out + 1] = {name = name, lat = lat, lon = lon, z = p.z, group = "spawn", types = {}, source = "spawn"}
    end
  end
  return out
end
local function exportFreightSites()
  local lvl = getCurrentLevelIdentifier and getCurrentLevelIdentifier()
  if not lvl then return nil end
  if sitesFor == lvl then return sitesFile end
  local m = map and map.getMap and map.getMap()
  if not (m and m.nodes and next(m.nodes)) then return nil end         -- the road graph isn't loaded yet: next hello
  local sites = facilitySites(lvl)
  if #sites < 8 then
    for _, s in ipairs(poiSites(lvl)) do sites[#sites + 1] = s end
  end
  for _, sp in ipairs(spawnSites(lvl)) do                 -- unless another place is already there
    local dup = false
    for _, s in ipairs(sites) do
      if ((s.lat - sp.lat) * K_LAT) ^ 2 + ((s.lon - sp.lon) * K_LON) ^ 2 < 150 * 150 then dup = true; break end
    end
    if not dup then sites[#sites + 1] = sp end
  end
  if #sites < 10 then
    for _, s in ipairs(roadSites(m, sites, 12 - #sites)) do sites[#sites + 1] = s end
  end
  local vpath = "/beamdeck_media/realroutes/freight_sites.json"
  jsonWriteFile(vpath, {level = lvl, title = flat.place().place, sites = sites}, false)
  sitesFor = lvl
  sitesFile = FS:getFileRealPath(vpath)
  log("I", "realroutes", string.format("freight sites for %s: %d", lvl, #sites))
  return sitesFile
end

-- the level's own bus lines (levels/<map>/buslines/*.buslines.json + the "busstops" triggers), the way the game's
-- busRouteManager reads them, with the road path between the stops: written once per level, path sent in hello
local busFor, busFile = nil, nil
local function exportBuslines()
  local lvl = getCurrentLevelIdentifier and getCurrentLevelIdentifier()
  if not lvl then return nil end
  if busFor == lvl then return busFile end
  local m = map and map.getMap and map.getMap()
  if not (m and m.nodes and next(m.nodes)) then return nil end   -- the road graph isn't loaded yet
  busFor, busFile = lvl, nil
  local files = FS:findFiles("/levels/" .. lvl .. "/buslines/", "*.buslines.json", -1, true, false) or {}
  if #files == 0 then return nil end
  local stops = {}
  local grp = scenetree.findObject("busstops")
  if grp and grp.getCount then
    for i = 0, grp:getCount() - 1 do
      local t = grp:getObject(i)
      if t and t:getClassName() == "BeamNGTrigger" then
        t = Sim.upcast(t)
        if t.type == "busstop" then
          local id = t:getName()
          stops[id] = {pos = vec3(t:getPosition()), name = tr(t.stopName) or id}
        end
      end
    end
  end
  local routes = {}
  for _, f in ipairs(files) do
    local data = jsonReadFile(f)
    if data and data.version == 1 then
      for _, r in ipairs(data.routes or {}) do
        local list, positions = {}, {}
        for _, id in ipairs(r.tasklist or {}) do
          local st = stops[id]
          if st then
            for _, wp in ipairs((r.navhelp or {})[id] or {}) do      -- the route's own hints on the way there
              local n = m.nodes[wp]
              if n then positions[#positions + 1] = vec3(n.pos) end
            end
            positions[#positions + 1] = st.pos
            local la, lo = toFlat(st.pos)
            list[#list + 1] = {id = id, name = st.name, lat = la, lon = lo}
          end
        end
        if #list >= 2 then
          local shape = {}
          local ok, err = pcall(function()
            local rp = require('/lua/ge/extensions/gameplay/route/route')()
            rp:setupPathMulti(positions)
            for _, p in ipairs(rp.path or {}) do
              local la, lo = toFlat(p.pos)
              shape[#shape + 1] = {la, lo}
            end
          end)
          if not ok then log("W", "realroutes", "bus line path: " .. tostring(err)) end
          if #shape < 2 then shape = {}; for _, st in ipairs(list) do shape[#shape + 1] = {st.lat, st.lon} end end
          routes[#routes + 1] = {routeID = tostring(r.routeID or "?"), variance = tostring(r.variance or ""),
                                 name = tr(r.name), direction = tr(r.direction), color = r.routeColor,
                                 stops = list, shape = shape}
        end
      end
    end
  end
  if #routes == 0 then return nil end
  local vpath = "/beamdeck_media/realroutes/buslines.json"
  jsonWriteFile(vpath, {level = lvl, routes = routes}, false)
  busFile = FS:getFileRealPath(vpath)
  log("I", "realroutes", string.format("%d bus lines on %s", #routes, lvl))
  return busFile
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
rrTick = (rrTick or 0) + 1
local ev = electrics.values
local function has(n) return controller.getController(n) ~= nil end
local function mode(n)
  local k = controller.getController(n)
  if k and k.getCurrentDriveModeKey then
    local ok, r = pcall(k.getCurrentDriveModeKey)
    if ok then return r end
  end
end
-- The retarder and engine brake name their readings and levels in the truck's own jbeam (a Percheron's retarder has an
-- Auto mode and a custom reading name), so what is shown comes from that configuration, not from assumed names.
local function jd(name)
  local d = v and v.data and v.data[name]
  return type(d) == "table" and d or nil
end
local rc = jd("retarderControl")
local rname = rc and rc.controlledRetarder or "retarder"
local retIdx = ev[(rc and rc.electricsNameLevelIndex) or (rname .. "_levelIndex")]
local retA = ev[(rc and rc.electricsNameActual) or (rname .. "_actual")]
local retAuto = rc and rc.hasAutoMode or false
local retL
if retIdx then
  local levels = rc and rc.retarderManualLevels or {0.2, 0.4, 0.6, 0.8, 1.0}
  local offIdx = retAuto and 2 or 1                       -- the list is: [auto,] off, then the manual levels
  if retAuto and retIdx == 1 then retL = "Auto"
  elseif retIdx == offIdx then retL = "Off"
  else
    local lv = levels[retIdx - offIdx]
    retL = lv and (math.floor(lv * 100 + 0.5) .. "%") or ("L" .. retIdx)
  end
end
local cb = jd("compressionBrake")
local ename = cb and cb.controlledEngine or "mainEngine"
local cbIdx = ev[(cb and cb.electricsNameLevelIndex) or (ename .. "_compressionBrake_levelIndex")]
local cbOn = ev[(cb and cb.electricsNameIsEnabled) or (ename .. "_compressionBrake_isEnabled")]
local cbLevels = cb and cb.compressionBrakeLevels or {0.33, 0.66, 1.0}
local cbL = cbIdx and cbLevels[cbIdx] and (math.floor(cbLevels[cbIdx] * 100 + 0.5) .. "%") or nil
if rrTick % 20 == 1 then   -- the weight of the whole vehicle: every two seconds is plenty
  local ok, st = pcall(function() return obj:calcBeamStats() end)
  if ok and st then rrMass = st.total_weight end
end
obj:queueGameEngineLua("if realroutes then realroutes.onVeh(" .. serialize({
  d = ev.dooropen or 0, k = ev.kneel or 0,
  gx = sensors.gx2 or sensors.gx or 0, gy = sensors.gy2 or sensors.gy or 0, bus = c ~= nil,
  l = ev.lights_state or ev.lowbeam or 0,
  rpm = ev.rpm, gear = ev.gear, fuel = ev.fuel, fuelL = ev.fuelVolume, fuelCap = ev.fuelCapacity,
  wT = ev.watertemp, oT = ev.oiltemp, odo = ev.odometer, trip = ev.trip, pb = ev.parkingbrake,
  load = ev.engineLoad, eng = ev.engineRunning, boost = ev.turboBoost, chk = ev.checkengine, lowf = ev.lowfuel,
  dmg = beamstate.damage, mass = rrMass,
  ret = retIdx, retL = retL, retA = retA, retAuto = retAuto, cb = cbIdx, cbL = cbL, cbOn = cbOn,
  cc = ev.cruiseControlActive, ccT = ev.cruiseControlTarget, mix = ev.mixer_on,
  ptoRpm = rrTruck and rrTruck.getRPM() or nil, raised = ev.raisedIdle,
  has = {ret = has("retarderControl"), cb = has("compressionBrake"), la = has("liftAxleControl"),
         dr = has("rearLockControl"), df = has("frontLockControl"), di = has("interaxleLockControl"),
         tc = has("transfercaseControl"), fw = has("fifthwheel"), mix = ev.mixer_on ~= nil,
         pto = ev.hydraulicPTOControl ~= nil},
  dm = {r = mode("rearLockControl"), f = mode("frontLockControl"), i = mode("interaxleLockControl"),
        t = mode("transfercaseControl")}}) .. ") end")
]]

M.onVeh = function(t) veh = t end

-- =====================================================================================================
-- Freight: the truck garage, the truck and trailer for a job, and the truck control panel's commands.
-- =====================================================================================================

-- how many vehicles are hooked to the player's (trailers; the game keeps this table on every couple / uncouple)
local function trailerCount()
  local v = player()
  local cv = rawget(_G, "core_vehicles")
  local adj = v and cv and cv.vehsCouplings and cv.vehsCouplings[v:getID()]
  if not adj then return 0 end
  local n = 0
  for _ in pairs(adj) do n = n + 1 end
  return n
end

-- every truck the game has (stock, mods and the player's own saved configs) with the specs the game shows and the key
-- parts each config is built from; one message a model, one model a frame (reading every config's file at once would stall)
local TRUCK_TYPES = {Truck = true, ["Heavy Machinery"] = true}
local PART_KW = {"fifthwheel", "trailerhitch", "pintle", "towhitch", "ballhitch", "gooseneck", "flatbed", "cargobox", "upfit_box",
                 "tanker", "dump", "mixer", "logging", "schoolbus", "citybus", "ambulance", "police", "firetruck"}
local vehQueue, vehIdx = nil, 0

local function specsOf(c)
  local y = c.Years
  return {power = c.Power, torque = c.Torque, weight = c.Weight, top = c["Top Speed"], drive = c.Drivetrain,
          trans = c.Transmission, body = c["Body Style"], fuel = c["Fuel Type"],
          years = type(y) == "table" and y.min and (tostring(y.min) .. "-" .. tostring(y.max or "")) or nil}
end

-- the parts of a config that say what the truck is (a fifth wheel, a flatbed, a tanker, a bus body...); custom configs
-- have names that say nothing, but their parts do
local function partsOf(model, key)
  local ok, d = pcall(jsonReadFile, "/vehicles/" .. model .. "/" .. key .. ".pc")
  if not ok or type(d) ~= "table" or type(d.parts) ~= "table" then return nil end
  local out, seen = {}, {}
  for _, v in pairs(d.parts) do
    if type(v) == "string" and v ~= "" and not seen[v] then
      local lv = v:lower()
      for _, kw in ipairs(PART_KW) do
        if lv:find(kw, 1, true) then seen[v] = true; out[#out + 1] = v; break end
      end
    end
  end
  table.sort(out)
  while #out > 14 do table.remove(out) end
  return out
end

local function listVehicles()
  local ok, list = pcall(function() return core_vehicles.getModelList(true).models end)
  local trucks = {}
  if ok and type(list) == "table" then
    for _, m in ipairs(list) do
      if TRUCK_TYPES[m.Type] then trucks[#trucks + 1] = m end
    end
  end
  if #trucks == 0 then send({type = "vehicles", first = true, last = true}); return end
  vehQueue, vehIdx = trucks, 0
end

local function pumpVehicles()
  if not vehQueue then return end
  vehIdx = vehIdx + 1
  local m = vehQueue[vehIdx]
  if m then
    local okm, full = pcall(core_vehicles.getModel, m.key)
    local configs = {}
    if okm and type(full) == "table" and type(full.configs) == "table" then
      for key, c in pairs(full.configs) do
        configs[#configs + 1] = {key = key, name = c.Configuration or c.Name or key, specs = specsOf(c),
                                 source = type(c.Source) == "string" and c.Source or nil, parts = partsOf(m.key, key)}
      end
      table.sort(configs, function(a, b) return a.key < b.key end)
      while #configs > 140 do table.remove(configs) end     -- one datagram per model
    end
    send({type = "vehicles", first = vehIdx == 1, last = vehIdx == #vehQueue,
          model = {model = m.key, brand = m.Brand, name = m.Name, type = m.Type, configs = configs}})
  end
  if vehIdx >= #vehQueue then vehQueue = nil end
end

-- ---- the rig: the job's truck, teleported to the pickup, with a trailer that fits its hitch ----
local placeBus                 -- (the bus placement below: a regular map's truck goes down the same way)
local rig = nil                -- {id, stage, truck, dest, trailers, tIdx, truckId, trailerId, ...}
local rigTrailers = {}         -- ids of the trailers this extension spawned (removed with the job)
local rigTeleported = false
local PREFER = {tractor = {"fifthwheel_v2", "fifthwheel", "gooseneck_hitch"}, drawbar = {"pintle"},
                light = {"tow_hitch", "gooseneck_hitch", "pintle"}}

local function rigSay(text)
  if rig then send({type = "freight", id = rig.id, event = "stage", text = text}) end
end

local BOX_SUFFIXES = {
  "FA", "FB", "FC", "FD", "A", "B", "C", "D", "E", "F", "G", "H",
  "RA", "RB", "RC", "RD", "RE", "RF",
  "FAB", "FAT", "FBB", "FBT", "FCB", "FCT", "FDB", "FDT",
  "AB", "AT", "BB", "BT", "CB", "CT", "DB", "DT", "EB", "ET", "FB", "FT", "GB", "GT", "HB", "HT",
  "RAB", "RAT", "RBB", "RBT", "RCB", "RCT", "RDB", "RDT",
  "trunk_A", "trunk_B", "trunk_C", "trunk_D", "trunk_E", "trunk_F", "trunk",
  "seat_FL", "seat_FR", "seat_RA", "seat_RB", "seat_RC", "seat_RD", "seat_RE", "seat_RF", "seat_RG", "seat_RH", "seat_RL", "seat_RR", "seat_R"
}

local function clearCargoVars()
  local vars = {}
  for _, s in ipairs(BOX_SUFFIXES) do
    vars["$box_" .. s .. "_weight"] = 0
    vars["$box" .. s .. "_weight"] = 0
    vars["$box_" .. s .. "_loadVolume"] = 0
    vars["$box" .. s .. "_loadVolume"] = 0
  end
  vars["$loadVolume"] = 0
  return vars
end

local function rigClearLoad()
  if rig and rig.loadSlot and rig.truckId then
    local vd = core_vehicle_manager.getVehicleData(rig.truckId)
    if vd and vd.config then
      vd.config.partsTree = nil
    end
    core_vehicle_partmgmt.setConfig({parts = {[rig.loadSlot] = ""}, vars = clearCargoVars()}, true, rig.truckId)
    rig.loadSlot = nil
  end
end
local function rigClearTrailers()
  rigClearLoad()
  for id in pairs(rigTrailers) do
    local o = getObjectByID(id)
    if o then pcall(function() o:delete() end) end
    rigTrailers[id] = nil
  end
end
local function rigFail(why)
  if rig then send({type = "freight", id = rig.id, event = "fail", why = why}) end
  rigClearTrailers()
  rig = nil
end
local function couplerCache(id)
  local cv = rawget(_G, "core_vehicles")
  return cv and cv.vehsCouplerCache and cv.vehsCouplerCache[id] or {}
end

-- A coupler pair that fits: an active coupler (couplerTag) on one vehicle and a receiver (tag) of the same name on the
-- other: the truck's fifth wheel and a semi trailer's kingpin, the truck's pintle (eye hitch) and a drawbar's eye.
local function findPair(truckId, trailerId, prefer)
  local best
  local function try(aId, bId, aIsTruck)
    for acid, an in pairs(couplerCache(aId)) do
      if an.couplerTag and not an.couplerWeld and not an.couplerLock then
        for bcid, bn in pairs(couplerCache(bId)) do
          if bn.tag == an.couplerTag and not bn.couplerWeld then
            local score = 0
            for i, t in ipairs(prefer) do if t == an.couplerTag then score = 10 - i end end
            if not best or score > best.score then
              best = {tag = an.couplerTag, truckNode = aIsTruck and acid or bcid,
                      trailerNode = aIsTruck and bcid or acid, score = score}
            end
          end
        end
      end
    end
  end
  try(truckId, trailerId, true)
  try(trailerId, truckId, false)
  return best
end

local function rigCoupled()
  local cv = rawget(_G, "core_vehicles")
  local adj = cv and rig and rig.truckId and cv.vehsCouplings and cv.vehsCouplings[rig.truckId]
  return adj ~= nil and rig.trailerId ~= nil and adj[rig.trailerId] == true
end

-- put the trailer's hitch on the truck's and switch the couplers to latch
local function rigCouple()
  local cv = core_vehicles
  local t, tr = rig.truckId, rig.trailerId
  local pair = findPair(t, tr, rig.prefer)
  if not pair then return false end
  rig.pair = pair
  local offT = cv.vehsCouplerOffset[t] and cv.vehsCouplerOffset[t][pair.truckNode]
  local offR = cv.vehsCouplerOffset[tr] and cv.vehsCouplerOffset[tr][pair.trailerNode]
  if offT and offR then
    local ok, err = pcall(spawn.placeTrailer, t, offT, tr, offR, pair.tag)
    if not ok then log("W", "realroutes", "placeTrailer: " .. tostring(err)) end
  end
  for _, id in ipairs({t, tr}) do
    local o = getObjectByID(id)
    if o then o:queueLuaCommand("beamstate.activateAutoCoupling()") end
  end
  return true
end

local function rigDone(coupled)
  rig.stage = "done"
  local c = rig.cand
  send({type = "freight", id = rig.id, event = rig.defer and "loaded" or "ready", coupled = coupled and true or false,
        trailer = c and c.config or nil, trailerName = c and (c.model .. " " .. c.config) or nil,
        hitch = rig.pair and rig.pair.tag or nil, loadName = rig.loadLabel, loadKg = rig.loadKg, noLoad = rig.noLoad or nil})
end

-- after the trailer: the truck's own load part (pipes, planks, boxes...), or done
local function rigTrailerDone(coupled)
  rig.coupled = coupled
  if rig.loadKind and rig.loadKind ~= "none" then rig.stage = "loadpart" else rigDone(coupled) end
end

-- ---- the loads a truck ships with: slots in its parts tree (a flatbed's pipes, planks, marble block or cargo boxes, a box
-- body's pallets, a tipper's rocks, a tanker's liquid). The part whose weight is nearest the job's is switched on
-- when the truck is loaded at the pickup, and the truck reloads in place (about a second) like it does for a seat.
local LOAD_PATTERNS = {                         -- kind -> part-name pieces, the first being preferred
  pipes = {"load_pipes"}, wood = {"load_woodplanks", "woodplanks"},
  marble = {"load_marble_mount", "marble_block"}, boxes = {"cargo_load_box"},
  pallets = {"load_pallet", "cargo_load_box"}, rocks = {"load_rocks"},
}
-- a part's weight in kg from its display name ("10000kg Steel Pipes", "15T Rocks"), or an estimate from the box or
-- pallet count in its name ("cargo_load_box_20XL", "load_pallet_x10"); the second value says it is an estimate
local function kgOf(label, partName)
  local kg = tostring(label):match("(%d[%d%.]*)%s*kg")
  if kg then return tonumber(kg), false end
  local t = tostring(label):match("(%d[%d%.]*)%s*T%f[%W]")
  if t then return tonumber(t) * 1000, false end
  local pn = tostring(partName or ""):lower()
  local boxes = pn:match("cargo_load_box_(%d+)xl")
  if boxes then return tonumber(boxes) * 600, true end
  local pallets = pn:match("load_pallet_x(%d+)")
  if pallets then return tonumber(pallets) * 800, true end
  return nil, false
end
local function loadTree(vid)
  local vd = core_vehicle_manager.getVehicleData(vid)
  return vd, vd and vd.config and vd.config.partsTree
end
local function availableParts(vd)
  local ok, io = pcall(require, "jbeam/io")
  if not (ok and io and io.getAvailableParts and vd.ioCtx) then return {} end
  local ok2, parts = pcall(io.getAvailableParts, vd.ioCtx)
  return ok2 and parts or {}
end
-- the best slot + part for a pattern list: the earlier the pattern the better, then the closest weight
local function chooseLoad(vd, tree, patterns, wantKg, skip)
  local parts = availableParts(vd)
  local best
  local function walk(n)
    for _, c in pairs(n.children or {}) do
      if type(c.suitablePartNames) == "table" then
        for _, pn in ipairs(c.suitablePartNames) do
          local lp = pn:lower()
          for rank, pat in ipairs(patterns) do
            if lp:find(pat, 1, true) and not (skip and lp:find(skip, 1, true)) and not lp:find("downpipe", 1, true)
               and not lp:find("tanker_pipes", 1, true) then
              local info = parts[pn] and parts[pn].information
              local label = info and type(info.name) == "string" and info.name or pn
              local kg, approx = kgOf(label, pn)
              local score = rank * 1e6 + (kg and math.abs(kg - wantKg) or 5e5)
              if not best or score < best.score then
                best = {node = c, part = pn, label = label, kg = (not approx) and kg or nil, score = score}
              end
              break
            end
          end
        end
      end
      walk(c)
    end
  end
  walk(tree)
  return best
end
local function setLoadPart(vid, vd, tree, best)
  local slot = best.node.id
  if rig and slot then rig.loadSlot = slot end
  best.node.chosenPartName = best.part

  -- keep vd.config clean so serialize(config) is tiny and does NOT dump the huge partsTree
  if vd and vd.config then
    vd.config.partsTree = nil
  end

  local cfg = {parts = {[slot] = best.part}}

  -- compute variables for boxes or tanker loads so they have physical mass
  local kind = rig and rig.loadKind or ""
  local wantKg = rig and rig.wantKg or 8000
  local vars = {}
  if kind == "boxes" or best.part:lower():find("box", 1, true) then
    local count = tonumber(best.part:lower():match("cargo_load_box_(%d+)xl") or best.part:lower():match("(%d+)xl")) or 10
    if count < 1 then count = 10 end
    local kgPerBox = math.min(800, math.max(50, math.floor(wantKg / count)))
    for _, s in ipairs(BOX_SUFFIXES) do
      vars["$box_" .. s .. "_weight"] = kgPerBox
      vars["$box" .. s .. "_weight"] = kgPerBox
      vars["$box_" .. s .. "_loadVolume"] = math.min(800, kgPerBox)
      vars["$box" .. s .. "_loadVolume"] = math.min(800, kgPerBox)
    end
    if rig then
      rig.loadKg = kgPerBox * count
      rig.loadLabel = tostring(math.floor(rig.loadKg)) .. "kg Cargo Boxes"
    end
  elseif kind:sub(1, 7) == "tanker:" or best.part:lower():find("tanker", 1, true) then
    vars["$loadVolume"] = math.min(16800, math.max(100, math.floor(wantKg)))
  end
  if next(vars) then cfg.vars = vars end

  core_vehicle_partmgmt.setConfig(cfg, true, vid)
end
-- first step: the load part itself; a marble block comes with a rail mount, and the block follows once the mount is in
local function applyLoadPart()
  local vd, tree = loadTree(rig.truckId)
  if not tree then rig.noLoad = true; return false end
  local kind = rig.loadKind
  local patterns = LOAD_PATTERNS[kind]
  local skip
  if kind:sub(1, 7) == "tanker:" then
    patterns = {"tanker_load_" .. kind:sub(8), "tanker_load_dynamic"}
  end
  if not patterns then rig.noLoad = true; return false end
  local best = chooseLoad(vd, tree, patterns, rig.wantKg or 10000, "chain")
  if not best then rig.noLoad = true; return false end
  rig.loadLabel, rig.loadKg = best.label, best.kg
  if kind == "marble" and best.part:lower():find("mount", 1, true) then rig.second = "marble" end
  setLoadPart(rig.truckId, vd, tree, best)
  return true
end
local function applySecondPart()
  local vd, tree = loadTree(rig.truckId)
  if not tree then return false end
  local block = chooseLoad(vd, tree, {"marble_block"}, rig.wantKg or 10000, "chain")
  if block and not block.part:lower():find("mount", 1, true) then
    rig.loadLabel, rig.loadKg = block.label, block.kg or rig.loadKg
    block.node.chosenPartName = block.part
    local chain = chooseLoad(vd, tree, {"marble_block_chain"}, 0)         -- its chains, when the slot is there
    if chain then chain.node.chosenPartName = chain.part end
    local parts = {[block.node.id] = block.part}
    if chain and chain.node.id then parts[chain.node.id] = chain.part end
    if vd and vd.config then vd.config.partsTree = nil end
    core_vehicle_partmgmt.setConfig({parts = parts}, true, rig.truckId)
    return true
  end
  return false
end

local function rigUpdate()
  if not rig or rig.stage == "done" or rig.stage == "waiting" then return end
  local now = os.clockhp()
  if now - rig.t0 > 110 then rigFail("Setting up the truck took too long."); return end
  local a = api()
  if rig.stage == "wait_truck" then
    local v = player()
    if v and now >= rig.next and v:getJBeamFilename() == rig.truck.model then
      rig.truckId = v:getID()
      rig.stage = "teleport"
    end
  elseif rig.stage == "teleport" then
    if a == flat then                         -- a regular map: put the truck there straight away
      placeBus({lat = rig.dest.lat, lon = rig.dest.lon, heading = rig.heading})
      rigSay("Moving to the pickup…")
      rig.stage, rig.next = "settle", now + 1.5
      return
    end
    if not (a and a.teleport) then rigFail("Freight needs BeamNG.Earth's world or a regular map loaded."); return end
    rigTeleported = false
    local ok, why = a.teleport(rig.dest.lat, rig.dest.lon, rig.dest.name)
    if ok == false then rigFail("Could not move to the pickup: " .. tostring(why)); return end
    rigSay("Moving to the pickup (the world has to load)…")
    rig.stage, rig.next = "wait_teleport", now
  elseif rig.stage == "wait_teleport" then
    if rigTeleported and a and a.ready and a.ready() then rig.stage, rig.next = "settle", now + 1.5
    elseif now - rig.next > 75 then rig.stage, rig.next = "settle", now end      -- gave up waiting: carry on
  elseif rig.stage == "settle" then
    if now >= rig.next then
      if rig.defer then
        rig.stage = "waiting"                           -- the truck is in place; the load comes at the pickup (freightLoad)
        send({type = "freight", id = rig.id, event = "ready"})
      elseif #rig.trailers > 0 then rig.stage = "trailer"; rig.tIdx = 1 else rigDone(nil) end
    end
  elseif rig.stage == "loadpart" then
    rigSay("Loading…")
    if applyLoadPart() then rig.stage, rig.next = "loadpart_wait", now + 3.5 else rigDone(rig.coupled) end
  elseif rig.stage == "loadpart_wait" and now >= rig.next then
    if rig.second then
      rig.second = nil
      if applySecondPart() then rig.next = now + 3.5 else rigDone(rig.coupled) end
    else
      rigDone(rig.coupled)
    end
  elseif rig.stage == "trailer" then
    local cand = rig.trailers[rig.tIdx]
    if not cand then rigFail("No trailer fits this truck's hitch. Pick another truck or another job."); return end
    local truck = getObjectByID(rig.truckId)
    if not truck then rigFail("The truck disappeared."); return end
    rigSay("Fitting a trailer: " .. cand.model .. " " .. cand.config)
    local pos = vec3(truck:getPosition())
    local dir = vec3(truck:getDirectionVector()); dir:normalize()
    local path = "vehicles/" .. cand.model .. "/" .. cand.config .. ".pc"
    local ok, tv = pcall(core_vehicles.spawnNewVehicle, cand.model,
      {config = path, pos = pos - dir * 14 + vec3(0, 0, 1.5), rot = quatFromDir(dir, vec3(0, 0, 1)),
       autoEnterVehicle = false, canSpawnAnotherVehicleCheck = false})
    if not ok or not tv then
      log("W", "realroutes", "trailer " .. path .. ": " .. tostring(tv))
      rig.tIdx = rig.tIdx + 1
      return
    end
    rig.trailerId, rig.cand = tv:getID(), cand
    rigTrailers[rig.trailerId] = true
    local pv = player()                         -- the truck stays the vehicle being driven
    if pv and pv:getID() ~= rig.truckId then be:enterVehicle(0, truck) end
    rig.stage, rig.next = "wait_trailer", now + 0.8
  elseif rig.stage == "wait_trailer" and now >= rig.next then
    if not getObjectByID(rig.trailerId) then rig.tIdx = rig.tIdx + 1; rig.stage = "trailer"; return end
    if not rigCouple() then                     -- this trailer has no hitch the truck takes: the next one
      local o = getObjectByID(rig.trailerId)
      if o then pcall(function() o:delete() end) end
      rigTrailers[rig.trailerId], rig.trailerId, rig.cand = nil, nil, nil
      rig.tIdx = rig.tIdx + 1
      rig.stage = "trailer"
      return
    end
    rig.stage, rig.next, rig.tries = "verify", now + 1.5, 0
  elseif rig.stage == "verify" then
    if rigCoupled() then rigTrailerDone(true)
    elseif now >= rig.next then
      rig.tries = rig.tries + 1
      if rig.tries >= 4 then rigTrailerDone(false)     -- not latched: the driver hooks it up
      else rigCouple(); rig.next = now + 2.5 end
    end
  end
end

-- the game's pause (the Esc menu or the pause key): a job's clock stops while it is on
local function gamePaused()
  local ok, r = pcall(function()
    if simTimeAuthority and simTimeAuthority.getPause then return simTimeAuthority.getPause() end
    if bullettime and bullettime.getPause then return bullettime.getPause() end
    return false
  end)
  return ok and r == true
end

local function rigStart(c)
  if rig and rig.id == c.id and rig.stage ~= "done" then return end    -- a repeated command
  rigClearTrailers()
  rig = {id = c.id, truck = c.truck, dest = c.at, heading = c.heading, trailers = c.trailers or {}, prefer = PREFER[c.needs] or {},
         stage = "wait_truck", t0 = os.clockhp(), next = os.clockhp() + 1.2, defer = c.deferLoad == true}
  rigSay("Spawning the truck…")
  core_vehicles.replaceVehicle(c.truck.model, {config = "vehicles/" .. c.truck.model .. "/" .. c.truck.config .. ".pc"})
end

-- ---- the truck control panel: a fixed list of the game's own vehicle actions (nothing from the network runs as code) ----
local TRUCK_CMDS = {
  retarder_up = "controller.getControllerSafe('retarderControl').changeRetarderSettingIndex(1)",
  retarder_down = "controller.getControllerSafe('retarderControl').changeRetarderSettingIndex(-1)",
  retarder_off = "controller.getControllerSafe('retarderControl').setRetarderMode('off')",
  retarder_auto = "controller.getControllerSafe('retarderControl').setRetarderMode('auto')",
  jake_toggle = "controller.getControllerSafe('compressionBrake').toggleCompressionBrakeState()",
  jake_up = "controller.getControllerSafe('compressionBrake').changeCompressionBrakeLevel(1)",
  jake_down = "controller.getControllerSafe('compressionBrake').changeCompressionBrakeLevel(-1)",
  park_on = "input.event('parkingbrake', 1, 1)",
  park_off = "input.event('parkingbrake', 0, 1)",
  lift_axle = "controller.getControllerSafe('liftAxleControl').toggleMode()",
  diff_rear_lock = "controller.getControllerSafe('rearLockControl').setDriveMode('locked')",
  diff_rear_unlock = "controller.getControllerSafe('rearLockControl').setDriveMode('unlocked')",
  diff_front_lock = "controller.getControllerSafe('frontLockControl').setDriveMode('locked')",
  diff_front_unlock = "controller.getControllerSafe('frontLockControl').setDriveMode('unlocked')",
  diff_inter_lock = "controller.getControllerSafe('interaxleLockControl').setDriveMode('locked')",
  diff_inter_unlock = "controller.getControllerSafe('interaxleLockControl').setDriveMode('unlocked')",
  tc_2hi = "controller.getControllerSafe('transfercaseControl').setDriveMode('2hi')",
  tc_4hi = "controller.getControllerSafe('transfercaseControl').setDriveMode('4hi')",
  tc_4lo = "controller.getControllerSafe('transfercaseControl').setDriveMode('4lo')",
  fifth_release = "controller.getControllerSafe('fifthwheel').detachFifthwheel() beamstate.detachCouplers('fifthwheel_v2')",
  pintle_release = "beamstate.detachCouplers('pintle')",
  trailer_air = "energyStorage.getStorageSafe('mainAirTank'):toggleConsumerPressureCoef()",
  cruise_on = "extensions.use('cruiseControl').holdCurrentSpeed()",
  cruise_off = "extensions.use('cruiseControl').setEnabled(false)",
  -- changeSpeed switches cruise on at the stored target (100 km/h at first), so with cruise off the first press
  -- engages it at the current speed instead (the game needs 30 km/h or more for that)
  cruise_up = "local cc = extensions.use('cruiseControl') if cc.getConfiguration().isEnabled then cc.changeSpeed(1.389) else cc.holdCurrentSpeed() end",
  cruise_down = "local cc = extensions.use('cruiseControl') if cc.getConfiguration().isEnabled then cc.changeSpeed(-1.389) else cc.holdCurrentSpeed() end",
  cruise_up1 = "local cc = extensions.use('cruiseControl') if cc.getConfiguration().isEnabled then cc.changeSpeed(0.2778) else cc.holdCurrentSpeed() end",
  cruise_down1 = "local cc = extensions.use('cruiseControl') if cc.getConfiguration().isEnabled then cc.changeSpeed(-0.2778) else cc.holdCurrentSpeed() end",
  bed_up = "electrics.values.hydraulicPTOControl = 1",
  bed_stop = "electrics.values.hydraulicPTOControl = 0",
  bed_down = "electrics.values.hydraulicPTOControl = -1",
  mixer_on = "electrics.values.mixer_on = 1",
  mixer_off = "electrics.values.mixer_on = 0",
  mixer_faster = "if electrics.values.mixer_speed < 0.89 then electrics.values.mixer_speed = electrics.values.mixer_speed + 0.2 end",
  mixer_slower = "if electrics.values.mixer_speed > 0.31 then electrics.values.mixer_speed = electrics.values.mixer_speed - 0.2 end",
  mixer_dir = "if (electrics.values.mixer_lock or 0) < 0.01 then electrics.values.mixer_dir = (electrics.values.mixer_dir == -1 and 1 or -1) end",
}

-- ---- placing the bus at the first stop ----
function placeBus(g)                   -- (declared local above, with the rig)
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
  local vd, changed, parts, needTree = nil, false, {}, false
  for sid, part in pairs(changes) do
    local node, d = occupantNode(vid, sid)
    vd = vd or d
    if node and node.chosenPartName ~= part then
      node.chosenPartName = part; changed = true
      if node.id then parts[node.id] = part else needTree = true end
    elseif not node then log("W", "realroutes", "no occupant slot found for seat " .. tostring(sid)) end
  end
  if M._partsRewritten then changed = true; M._partsRewritten = nil end   -- a part file changed: reload anyway
  if changed and vd then
    if needTree then
      core_vehicle_partmgmt.setConfig({partsTree = deepcopy(vd.config.partsTree)}, true, vid)
    else
      -- only the changed slots go in: the whole tree is huge to serialize (it froze the game) and resets parts the player added
      vd.config.partsTree = nil
      core_vehicle_partmgmt.setConfig({parts = parts}, true, vid)
    end
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
handlers.vehicles = function() listVehicles() end
-- ---- CareerMP (our 0.40 fork): jobs, the hourly payslip and the shared clock live in careerMPJobs / careerMPWorld;
-- Real Routes reports what each job earned and shows the jobs on the tablet. Without CareerMP these do nothing.
local function careerJobs() return rawget(_G, "careerMPJobs") end
local function sendCareer(extra)
  local J = careerJobs()
  if not (J and J.getUI) then return end
  local ok, ui = pcall(J.getUI)
  if not ok or type(ui) ~= "table" then return end
  local msg = {type = "career", active = ui.career, main = ui.main, second = ui.second, world = ui.world, jobs = ui.jobs}
  for k, v in pairs(extra or {}) do msg[k] = v end
  send(msg)
end
handlers.careerEarn = function(c)
  local J = careerJobs()
  if J and J.earn and tonumber(c.amount) then J.earn(tostring(c.role or ""), tonumber(c.amount), tostring(c.label or "")) end
end
handlers.careerJobs = function(c)
  local J = careerJobs()
  if J and J.setRoles then J.setRoles(c.main ~= "" and tostring(c.main) or nil, c.second ~= "" and tostring(c.second) or nil) end
  sendCareer()
end
M.onCareerMPJobsChanged = function() sendCareer() end
M.onCareerMPPayslip = function(slip) sendCareer({slip = slip}) end

handlers.freightSpawn = function(c) rigStart(c) end
handlers.freightClear = function() rigClearTrailers(); rig = nil end
-- the driver chose to skip the drive to the pickup
handlers.freightGoto = function(c)
  local a = api()
  if not (tonumber(c.lat) and tonumber(c.lon)) then return end
  if a == flat then placeBus({lat = c.lat, lon = c.lon, heading = c.heading})
  elseif a and a.teleport then a.teleport(c.lat, c.lon, c.name) end
end
-- at the pickup: hook the trailer on and / or put the load on the deck
handlers.freightLoad = function(c)
  local v = player()
  if not rig or rig.id ~= c.id then
    if not v then
      log("W", "realroutes", "freightLoad: no vehicle and no active rig for job " .. tostring(c.id))
      return
    end
    log("I", "realroutes", "freightLoad: adopting current player truck for job " .. tostring(c.id))
    rig = {id = c.id, truckId = v:getID(), trailers = c.trailers or {}, prefer = PREFER[c.needs] or {}, defer = true, stage = "waiting"}
  end
  if rig.stage ~= "waiting" and rig.stage ~= "settle" and rig.stage ~= "loadpart" and rig.stage ~= "trailer" then
    log("W", "realroutes", "freightLoad: stage was " .. tostring(rig.stage) .. ", proceeding with load")
    rig.stage = "waiting"
  end
  if v then rig.truckId = v:getID() end
  rig.loadKind = type(c.kind) == "string" and c.kind or "none"
  rig.wantKg = tonumber(c.weightKg or c.weight) or 10000
  rig.trailers = c.trailers or rig.trailers or {}
  rig.prefer = PREFER[c.needs] or rig.prefer or {}
  rig.t0 = os.clockhp()
  rigSay("Loading…")
  if #rig.trailers > 0 then rig.stage, rig.tIdx = "trailer", 1
  elseif rig.loadKind ~= "none" then rig.stage = "loadpart"
  else rigDone(nil) end
end
handlers.freightRecouple = function()
  if rig and rig.truckId and rig.trailerId then rigCouple(); rig.stage, rig.next, rig.tries = "verify", os.clockhp() + 1.5, 0 end
end
handlers.pto = function(c)
  local rpm = tonumber(c.rpm)
  local arg = (rpm and rpm > 0) and string.format("%d", math.floor(math.max(700, math.min(2400, rpm)))) or "nil"
  vehLua('if not rrTruck then extensions.load("rrTruck") end rrTruck.setPtoRPM(' .. arg .. ')')
end
handlers.truck = function(c)
  local cmd = TRUCK_CMDS[c.name]
  if cmd then vehLua(cmd) end
end
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
  local ok, err = pcall(rigUpdate)                     -- a mistake in the rig must not leave a job waiting for ever
  if not ok then
    log("E", "realroutes", "rig: " .. tostring(err))
    if rig then rigFail("The game could not finish setting up the load (" .. tostring(err):sub(1, 120) .. "). See beamng.log.") end
  end
  pumpVehicles()
  local a = api()
  if pendingGoto and os.clockhp() >= pendingGoto.at then
    local g = pendingGoto
    if g.stage == "teleport" and a == flat then          -- a regular map: put the bus at the stop straight away
      pendingGoto = nil
      placeBus(g)
      lineAgainAt = os.clockhp() + 1
      for _, m in ipairs(markers) do m.pos = nil end
    elseif g.stage == "teleport" and a then
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
    send({type = "hello", rr = MOD_VERSION, gamePath = pcall(function() return FS:getGamePath() end) and FS:getGamePath() or nil,
          userPath = pcall(function() return FS:getUserPath() end) and FS:getUserPath() or nil, player = (MPConfig and MPConfig.getNickname and MPConfig.getNickname()) or nil, earth = a and a.earthVersion and a.earthVersion() or nil, mapPort = mapPort(),
          flat = isFlat or nil, level = isFlat and getCurrentLevelIdentifier() or nil,
          roads = isFlat and exportRoads() or nil, buslines = isFlat and exportBuslines() or nil,
          freightSites = isFlat and exportFreightSites() or nil})
    sendCareer()                                         -- the jobs and the clock for the tablet (CareerMP only)
  end
  if not (a and a.ready and a.ready()) then return end
  local p = a.pose()
  if not p then return end
  local lim = a.speedLimit and a.speedLimit()
  local g = a.gps and a.gps()
  sendRoute(a)
  send({type = "pose", lat = p.lat, lon = p.lon, heading = p.heading, speed = p.speed or 0, paused = gamePaused() or nil,
        gpsLeft = g and g.remaining or nil, gpsEta = g and g.eta or nil, gpsOff = g and g.off or nil,
        model = (function() local v = player(); return v and v:getJBeamFilename() or nil end)(),
        place = a.place and a.place() or nil, gpsName = g and g.name or nil,
        doors = (veh.d or 0) > 0.5, kneel = (veh.k or 0) > 0.5, gx = veh.gx, gy = veh.gy, bus = veh.bus,
        ready = true, limit = lim and lim.ms or nil, earth = a.earthVersion and a.earthVersion() or nil,
        flat = (a == flat) or nil, lights = (veh.l or 0) > 0,
        -- the truck: gauges, trailer count and the state of its controls (the freight panel)
        rpm = veh.rpm, gear = veh.gear, fuel = veh.fuel, fuelL = veh.fuelL, fuelCap = veh.fuelCap, wT = veh.wT, oT = veh.oT,
        odo = veh.odo, trip = veh.trip, pb = veh.pb, load = veh.load, eng = veh.eng, boost = veh.boost, chk = veh.chk,
        lowf = veh.lowf, dmg = veh.dmg, mass = veh.mass, tr = trailerCount(),
        ret = veh.ret, retL = veh.retL, retA = veh.retA, retAuto = veh.retAuto, cb = veh.cb, cbL = veh.cbL, cbOn = veh.cbOn,
        cc = veh.cc, ccT = veh.ccT,
        mix = veh.mix, has = veh.has, dm = veh.dm, ptoRpm = veh.ptoRpm, raised = veh.raised})
end

-- ---- BeamNG.Earth hooks ----
M.onBeamNGEarthTeleported = function()
  rigTeleported = true
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
