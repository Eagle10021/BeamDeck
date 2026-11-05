-- BeamDeck game link (no in-game UI). Talks to the BeamDeck helper over UDP on localhost:
--   sends     -> 127.0.0.1:47132  camera/car info at 20 Hz (for the car-speaker audio effect)
--   listens   <- 127.0.0.1:47131  {on, file, pos, paused} car-screen state at 5 Hz
local M = {}

local socket = require("socket")
local udp = nil
local HELPER_PORT, MY_PORT = 47132, 47131

local sendTimer, keepTimer = 0, 0
local state = {on = false, file = "", pos = 0, paused = true}
local lastStateAt = -1
local enabledVehId = nil
local screenCounts = {}  -- vehId -> count reported by the vehicle
local lightsByVeh = {}   -- vehId -> headlights on (reported by the vehicle)

-- The music lives in cars only. Walking mode makes the player a "unicycle" vehicle; then the sound (and screens,
-- shake, doors) stays with the car the player got out of. In BeamMP, a spectated car doesn't belong to the local
-- player either: the local music stays in the player's last own car (and the owner's music plays from it).
local lastCarId = nil
local function isWalker(v) return v and v.jbeam == "unicycle" end
local function inMP()                   -- on a BeamMP server (BeamMP's scripts stay loaded in single player too)
  if not (MPCoreNetwork and MPCoreNetwork.isMPSession) then return false end
  local ok, on = pcall(MPCoreNetwork.isMPSession)
  return ok and on and true or false
end
local function isMine(v)                -- BeamMP: only the local player's own cars (not a spectated one) carry their music
  if not (inMP() and MPVehicleGE and MPVehicleGE.isOwn) then return true end
  local ok, own = pcall(MPVehicleGE.isOwn, v:getId())
  return not ok or own
end
-- BeamDeck TVs (the TV prop's "BeamDeck" configuration): vehId -> true, reported by their controller
local tvSet = {}
local function carVehicle()
  local v = getPlayerVehicle(0)
  if v and not isWalker(v) and v.jbeam ~= "tv" and not tvSet[v:getId()] and isMine(v) then   -- (a TV prop is never the car)
    lastCarId = v:getId()
    return v
  end
  local c = lastCarId and be:getObjectByID(lastCarId)
  if c then return c end
  lastCarId = nil
  return nil
end
local shakeByVeh = {}    -- vehId -> what the bass shake is doing (reported by the vehicle)
local bass, bassAt, bassSentAt = 0, -1, -1
local camPos, vehPos, right = vec3(), vec3(), vec3()

-- ---- clicking the car's screen (Android Auto). Each car model's screen is set up once by clicking its
-- four corners from the driver's seat. What's saved is the camera position and the four click directions,
-- all in the car's own frame (so they move with it); a click is then mapped onto the screen with a
-- perspective (homography) fit, which is exact however the screen is angled. It doesn't depend on what
-- the click "hits", so dash geometry in the way doesn't matter.
local im = ui_imgui
local CALIB_FILE = "/settings/beamdeck_screens.json"
local calibs = jsonReadFile(CALIB_FILE) or {}
-- Set-up is two passes of four corner clicks from two camera positions; each corner is triangulated
-- from its two rays, which gives the screen's real position in the car (so any later camera angle works).
local calibStep, calibRays, lastCalibReq = 0, {}, nil
local CORNER_NAMES = {"top-left", "top-right", "bottom-right", "bottom-left"}

-- The screen calibration is saved per model and config: "<model>/<config file name>". Worked out once per car
-- (cached by its ID until it respawns): after a change in the config menu, partConfig is no longer a file path but
-- the whole configuration as one long serialized string (often 100+ KB), and searching that with a pattern 20
-- times a second froze the game to ~1 fps.
local vehKeys = {}
local function vehKey(veh)
  local id = veh:getId()
  local k = vehKeys[id]
  if k then return k end
  local pc = veh.partConfig
  local cfg = ""
  if type(pc) == "string" and #pc < 512 and pc:sub(-3) == ".pc" then
    cfg = pc:match("([^/]*)%.pc$") or ""          -- (short path: cheap)
  elseif type(pc) == "string" and #pc >= 3 then
    cfg = "custom"                                 -- edited in the config menu
  end
  k = (veh.jbeam or "?") .. "/" .. cfg
  vehKeys[id] = k
  return k
end
-- a calibration for this exact config, else one for any config of the same model (a customised car keeps its
-- screen where the model's screen is)
local function calibFor(veh)
  local exact = calibs[vehKey(veh)]
  if exact then return exact end
  local model = (veh.jbeam or "?") .. "/"
  for key, v in pairs(calibs) do
    if key:sub(1, #model) == model then return v end
  end
  return nil
end

local function frame(veh)               -- origin and axes of the car's reference-node frame
  local m = veh:getRefNodeMatrix()
  return vec3(m:getColumn(3)), vec3(m:getColumn(0)), vec3(m:getColumn(1)), vec3(m:getColumn(2))
end

local function toLocal(veh, p)          -- a point, in the car's frame
  local o, x, y, z = frame(veh)
  local d = p - o
  return vec3(d:dot(x), d:dot(y), d:dot(z))
end

local function dirToLocal(veh, d)       -- a direction, in the car's frame
  local _, x, y, z = frame(veh)
  return vec3(d:dot(x), d:dot(y), d:dot(z))
end

local function v3(t) return vec3(t[1], t[2], t[3]) end
local function t3(v) return {v.x, v.y, v.z} end

-- solve the 8x8 system for the homography taking 4 points (x, y) to the unit square corners
local function homography(src)
  local dst = {{0, 0}, {1, 0}, {1, 1}, {0, 1}}
  local A = {}
  for i = 1, 4 do
    local x, y, u, v = src[i][1], src[i][2], dst[i][1], dst[i][2]
    A[#A + 1] = {x, y, 1, 0, 0, 0, -u * x, -u * y, u}
    A[#A + 1] = {0, 0, 0, x, y, 1, -v * x, -v * y, v}
  end
  for c = 1, 8 do                       -- Gauss-Jordan with partial pivoting
    local piv = c
    for r = c + 1, 8 do if math.abs(A[r][c]) > math.abs(A[piv][c]) then piv = r end end
    A[c], A[piv] = A[piv], A[c]
    if math.abs(A[c][c]) < 1e-12 then return nil end
    for r = 1, 8 do
      if r ~= c then
        local f = A[r][c] / A[c][c]
        for k = c, 9 do A[r][k] = A[r][k] - f * A[c][k] end
      end
    end
  end
  local h = {}
  for r = 1, 8 do h[r] = A[r][9] / A[r][r] end
  return h
end

-- the point halfway between two rays where they pass closest (each ray: origin o, direction d)
local function triangulate(o1, d1, o2, d2)
  local w = o1 - o2
  local a, b, c, d, e = d1:dot(d1), d1:dot(d2), d2:dot(d2), d1:dot(w), d2:dot(w)
  local den = a * c - b * b
  if math.abs(den) < 1e-9 then return nil end
  local t1, t2 = (b * e - c * d) / den, (a * e - b * d) / den
  return (o1 + d1 * t1 + o2 + d2 * t2) * 0.5
end

-- where a ray (in the car's frame) points on the screen, as (u, v) in 0..1
local function rayToScreen(c, o, d)
  local P = {v3(c.corners[1]), v3(c.corners[2]), v3(c.corners[3]), v3(c.corners[4])}
  local C = (P[1] + P[2] + P[3] + P[4]) * 0.25
  local n = (P[2] - P[1]):cross(P[4] - P[1]):normalized()
  local den = d:dot(n)
  if math.abs(den) < 1e-6 then return nil end
  local t = (C - o):dot(n) / den
  if t <= 0 then return nil end
  local q = o + d * t
  local e1 = (P[2] - P[1]):normalized()
  local e2 = n:cross(e1):normalized()
  local src = {}
  for k = 1, 4 do src[k] = {(P[k] - C):dot(e1), (P[k] - C):dot(e2)} end
  local h = homography(src)
  if not h then return nil end
  local x, y = (q - C):dot(e1), (q - C):dot(e2)
  local w = h[7] * x + h[8] * y + 1
  return (h[1] * x + h[2] * y + h[3]) / w, (h[4] * x + h[5] * y + h[6]) / w
end

local pushScreens  -- (defined further down)
local function sendHelper(t)
  if udp then udp:sendto(jsonEncode(t), "127.0.0.1", HELPER_PORT) end
end

local function handleClick(veh)
  local ray = getCameraMouseRay()
  if not ray then return end
  -- the camera itself (a mouse ray may start a little in front of it, differently for each click)
  local eye = (core_camera and core_camera.getPosition and vec3(core_camera.getPosition())) or vec3(ray.pos)
  local o, d = toLocal(veh, eye), dirToLocal(veh, vec3(ray.dir))

  if calibStep > 0 then                 -- setting up: two passes of the four corners
    calibRays[calibStep] = {o = o, d = d}
    calibStep = calibStep + 1
    if calibStep == 5 then
      ui_message("BeamDeck: now move the camera a little (W/S moves the seat, or look from another angle), " ..
                 "then click the top-left corner again", 15, "beamdeck")
    elseif calibStep > 8 then
      local corners, spread = {}, 1
      for k = 1, 4 do
        local r1, r2 = calibRays[k], calibRays[k + 4]
        spread = math.min(spread, 1 - math.abs(r1.d:dot(r2.d)))
        local p = triangulate(r1.o, r1.d, r2.o, r2.d)
        if not p then spread = 0 break end
        corners[k] = t3(p)
      end
      calibStep = 0
      if spread < 1e-5 then             -- the two passes were from (almost) the same place
        ui_message("BeamDeck: the camera didn't move enough between the two passes. Click Set up screen and try again.", 8, "beamdeck")
        return
      end
      calibs[vehKey(veh)] = {v = 3, corners = corners}
      jsonWriteFile(CALIB_FILE, calibs, true)
      ui_message("BeamDeck: screen set up. Clicks on it now tap Android Auto from any camera angle.", 5, "beamdeck")
    else
      local k = (calibStep - 1) % 4 + 1
      ui_message("BeamDeck: click the screen's " .. CORNER_NAMES[k] .. " corner" ..
                 (calibStep > 4 and " (second pass)" or ""), 10, "beamdeck")
    end
    return
  end

  local c = calibFor(veh)
  if not (c and c.v == 3 and state.aa) then return end
  local u, v = rayToScreen(c, o, d)
  if u and u >= 0 and u <= 1 and v >= 0 and v <= 1 then
    sendHelper({tap = {u, v}})
    pushScreens(string.format("window.btTap && btTap(%.4f, %.4f)", u, v))   -- show where it landed
  end
end


local function openSocket()
  if udp then return end
  udp = socket.udp()
  udp:settimeout(0)
  local ok, err = udp:setsockname("127.0.0.1", MY_PORT)
  if not ok then
    log("E", "beamdeck", "Can't listen on UDP " .. MY_PORT .. ": " .. tostring(err))
  end
end

local function vehCmd(veh, cmd)
  if veh then veh:queueLuaCommand(cmd) end
end

local ENABLE = 'extensions.load("beamdeckScreens"); if not beamdeckScreens.isEnabled() then beamdeckScreens.enable() end'
local DISABLE = 'if beamdeckScreens and beamdeckScreens.isEnabled() then beamdeckScreens.disable() end'

-- ---- BeamDeck TVs: they show the same as the car screen, and the sound comes from the nearest one when the camera
-- is closer to it than to the car. Cost limits: own TVs only, the 2 nearest within 60 m, pictures at most 15/s.
local TV_RANGE, TV_MAX, TV_FPS = 60, 2, 15
local tvOn, tvActive, tvTimer, tvFrameAt = {}, {}, 0, 0
local function tvRefresh()
  tvActive = {}
  if not core_camera or next(tvSet) == nil then return end
  local cam = vec3(core_camera.getPosition())
  local list = {}
  for id in pairs(tvSet) do
    local o = be:getObjectByID(id)
    if not o then tvSet[id] = nil
    elseif isMine(o) then
      local d = (vec3(be:getObjectOOBBCenterXYZ(id)) - cam):length()
      if d <= TV_RANGE then list[#list + 1] = {o = o, d = d} end
    end
  end
  table.sort(list, function(a, b) return a.d < b.d end)
  for i = 1, math.min(TV_MAX, #list) do tvActive[i] = list[i].o end
end

local function syncScreens(force)
  local veh = carVehicle()
  local vid = veh and veh:getId() or nil
  local wantOn = state.on and (os.clockhp() - lastStateAt) < 3  -- helper gone -> screens off

  if enabledVehId and (not wantOn or enabledVehId ~= vid) then
    vehCmd(be:getObjectByID(enabledVehId), DISABLE)
    enabledVehId = nil
  end
  if wantOn and vid and (enabledVehId ~= vid or force) then
    vehCmd(veh, ENABLE)  -- idempotent; also re-enables after a vehicle reload (Ctrl+R)
    enabledVehId = vid
  end
  local syncCmd = string.format("if beamdeckScreens then beamdeckScreens.sync(%q, %.3f, %s, %s) end",
    state.file or "", state.pos or 0, state.paused and "true" or "false",
    string.format("%q", (state.aa and type(state.gamemap) == "string") and state.gamemap or ""))
  if enabledVehId and veh then vehCmd(veh, syncCmd) end
  -- TVs nearby show the same screen; ones that went out of range (or screens off) go back to "no signal"
  local want = {}
  if wantOn then for _, tv in ipairs(tvActive) do want[tv:getId()] = tv end end
  for id in pairs(tvOn) do
    if not want[id] then vehCmd(be:getObjectByID(id), DISABLE); tvOn[id] = nil end
  end
  for id, tv in pairs(want) do
    if not tvOn[id] or force then vehCmd(tv, ENABLE); tvOn[id] = true end
    vehCmd(tv, syncCmd)
  end
end

local function receive()
  if not udp then return end
  local got, newBass = false, false
  while true do
    local data = udp:receive()
    if not data then break end
    local ok, d = pcall(jsonDecode, data)
    if ok and type(d) == "table" and d.bass ~= nil then
      bass = tonumber(d.bass) or 0
      bassAt = os.clockhp()
      newBass = true
    elseif ok and type(d) == "table" then
      local req = tonumber(d.calib) or 0
      if lastCalibReq ~= nil and req > lastCalibReq then
        calibStep, calibRays = 1, {}
        ui_message("BeamDeck: click the screen's top-left corner", 10, "beamdeck")
      end
      lastCalibReq = math.max(lastCalibReq or 0, req)
      state = d
      lastStateAt = os.clockhp()
      got = true
    end
  end
  if got then syncScreens(false) end
  if bass > 0 and os.clockhp() - bassAt > 0.5 then bass = 0; newBass = true end  -- window closed / paused
  -- forward the bass level to the player's car when it changes
  if newBass and (bass > 0 or bassSentAt >= 0) then
    local veh = carVehicle()
    if veh then
      veh:queueLuaCommand(string.format(
        'if not beamdeckScreens then extensions.load("beamdeckScreens") end beamdeckScreens.setBass(%.3f)', bass))
    end
    bassSentAt = bass > 0 and os.clockhp() or -1
  end
end

-- BeamMP: the local player's name, and where every other player's car is (the one they're in), for their shared music
local otherPos = vec3()
-- how open a car is towards the listener at camPos (0..1): openings on the listener's side count fully, the far side less
local function opennessToward(veh, carPos, camPos)
  local sh = shakeByVeh[veh:getId()]
  local op = sh and sh.open
  if not op or (carPos - camPos):length() < 0.01 then return 0 end
  local fwd = vec3(veh:getDirectionVector()):normalized()
  local up = vec3(veh:getDirectionVectorUp()):normalized()
  local rgt = fwd:cross(up):normalized()
  local toCam = (camPos - carPos):normalized()
  local x, y = toCam:dot(rgt), toCam:dot(fwd)          -- camera to the car's right (+x) / front (+y)
  local w = function(a) return 0.45 + 0.55 * math.max(0, a) end
  return math.min(1, (op.L or 0) * w(-x) + (op.R or 0) * w(x) + (op.F or 0) * w(y)
                     + (op.B or 0) * w(-y) + (op.T or 0) * 0.7)
end
local watchedAt = {}            -- vehId -> when its car script was last ensured to be running (it reports doors / windows)
local lastCarOf = {}            -- player name -> their last real car (when they walk, the music stays there)
local mpCarOf = {}              -- player name -> the car their music (and shared screen) comes from
local lastCam, lastCamAt, camVel = nil, nil, vec3()
local function mpInfo(cam, camRight)
  -- the camera's own velocity (for Doppler), measured between updates
  local now = os.clockhp()
  if lastCam and lastCamAt and now > lastCamAt then
    local v = (cam - lastCam) / (now - lastCamAt)
    camVel = v:length() < 150 and v or vec3()               -- a camera jump (teleport, switch) isn't speed
  end
  lastCam, lastCamAt = vec3(cam), now
  if not (inMP() and MPVehicleGE and MPConfig and MPVehicleGE.getPlayers) then return nil end
  local ok, res = pcall(function()
    local vehs = MPVehicleGE.getVehicles() or {}
    local cars = {}
    for _, p in pairs(MPVehicleGE.getPlayers() or {}) do
      if not p.isLocal and p.name then
        local v = p.activeVehicleID and (vehs[p.activeVehicleID] or vehs[tostring(p.activeVehicleID)])
        local function walker(x)
          local o = x and x.gameVehicleID and be:getObjectByID(x.gameVehicleID)
          -- walking, or a TV prop: never the car their music (and shared screen) comes from
          return (x and (x.jbeam == "unicycle" or x.jbeam == "tv")) or isWalker(o) or (o and o.jbeam == "tv") or (o and tvSet[o:getId()]) or false
        end
        if v and v.ownerName == p.name and not walker(v) then
          lastCarOf[p.name] = v.gameVehicleID                 -- they're in their car
        else                                                  -- walking or spectating: the car they left
          v = nil
          local last = lastCarOf[p.name]
          for _, x in pairs(vehs) do
            if x.ownerName == p.name and not x.isDeleted and not walker(x) then
              if x.gameVehicleID == last then v = x break end
              v = v or x
            end
          end
        end
        local obj = v and v.gameVehicleID and be:getObjectByID(v.gameVehicleID)
        mpCarOf[p.name] = obj and v.gameVehicleID or nil
        if obj then
          otherPos:set(be:getObjectOOBBCenterXYZ(v.gameVehicleID))
          local d = otherPos - cam
          local dist = d:length()
          -- Doppler: speed towards the camera along the line between them (positive = approaching)
          local rv = 0
          if dist > 0.5 then
            local vel = vec3(obj:getVelocity())
            rv = (vel - camVel):dot(d * (-1 / dist))
          end
          if os.clockhp() - (watchedAt[v.gameVehicleID] or -99) > 5 then      -- their car reports its doors / windows
            watchedAt[v.gameVehicleID] = os.clockhp()
            obj:queueLuaCommand('if not beamdeckScreens then extensions.load("beamdeckScreens") end')
          end
          cars[#cars + 1] = {name = p.name, dist = math.floor(dist * 10) / 10,
                             open = math.floor(opennessToward(obj, otherPos, cam) * 100) / 100,
                             pan = dist > 0.01 and math.floor(clamp(d:dot(camRight) / dist, -1, 1) * 100) / 100 or 0,
                             rv = math.floor(rv * 10) / 10}
        end
      end
    end
    -- voice chat: each player talks from where they are (their car, or on foot); never muffled, just distance
    local voice = {}
    for _, p in pairs(MPVehicleGE.getPlayers() or {}) do
      if not p.isLocal and p.name then
        local v = p.activeVehicleID and (vehs[p.activeVehicleID] or vehs[tostring(p.activeVehicleID)])
        local id = (v and v.ownerName == p.name) and v.gameVehicleID or mpCarOf[p.name]     -- (spectating: their car)
        local obj = id and be:getObjectByID(id)
        if obj then
          local pos = vec3(be:getObjectOOBBCenterXYZ(id))
          local d = pos - cam
          local dist = d:length()
          voice[#voice + 1] = {name = p.name, dist = math.floor(dist * 10) / 10,
                               pan = dist > 0.01 and math.floor(clamp(d:dot(camRight) / dist, -1, 1) * 100) / 100 or 0}
        end
      end
    end
    return {me = MPConfig.getNickname and MPConfig.getNickname() or nil, cars = cars, voice = voice}
  end)
  return ok and res or nil
end

local function sendInfo()
  if not udp then return end
  local info = {hasCar = false}
  local veh = carVehicle()
  if veh and core_camera then
    camPos:set(core_camera.getPosition())
    vehPos:set(be:getObjectOOBBCenterXYZ(veh:getId()))
    right:set(core_camera.getRight())
    local d = vehPos - camPos
    local dist = d:length()
    info.hasCar = true
    local viewed = getPlayerVehicle(0)       -- (inside only counts in the player's own car, not one walked past or spectated)
    info.inside = viewed ~= nil and viewed:getId() == veh:getId() and core_camera.isCameraInside(0, camPos) == 1
    info.dist = dist
    info.pan = dist > 0.01 and clamp(d:dot(right) / dist, -1, 1) or 0
    info.vehName = veh.jbeam or ""
    -- how open the car is towards the listener: openings on the listener's side count fully, the far side less
    info.openness = opennessToward(veh, vehPos, camPos)
    info.screenCount = screenCounts[veh:getId()]
    info.calibrated = (calibFor(veh) or {}).v == 3
    info.lights = lightsByVeh[veh:getId()] or false
    info.shake = shakeByVeh[veh:getId()]
    info.calibStep = calibStep
  end
  if core_camera then
    local cam = vec3(core_camera.getPosition())
    info.mp = mpInfo(cam, vec3(core_camera.getRight()))
    local tv = tvActive[1]
    if tv and tvOn[tv:getId()] then                  -- the nearest TV showing BeamDeck: where its sound comes from
      local d = vec3(be:getObjectOOBBCenterXYZ(tv:getId())) - cam
      local dist = d:length()
      info.tv = {dist = math.floor(dist * 10) / 10,
                 pan = dist > 0.01 and math.floor(clamp(d:dot(vec3(core_camera.getRight())) / dist, -1, 1) * 100) / 100 or 0}
    end
  end
  info.paused = simTimeAuthority and simTimeAuthority.getPause and simTimeAuthority.getPause() or false
  udp:sendto(jsonEncode(info), "127.0.0.1", HELPER_PORT)
end

-- called by the vehicle extension: how many screens, and their web view tags
local screenTags = {}
local function onLights(vehId, on, shakeJson)
  lightsByVeh[vehId] = on
  if shakeJson then shakeByVeh[vehId] = jsonDecode(shakeJson) end
end
local function onScreens(vehId, count, tagsJson)
  screenCounts[vehId] = count
  if tagsJson then screenTags[vehId] = jsonDecode(tagsJson) end
end

-- ---- Android Auto frames: read over plain TCP from the helper (the screens' web views can't load from
-- the network), then handed to the screen pages with queueJSUITexture. Each frame: 8 hex digits of length,
-- then a base64 JPEG.
local FRAME_PORT = 47133
local ftcp, fbuf, fneed, fRetry, aaShown = nil, "", nil, 0, nil

local function frameClose()
  if ftcp then ftcp:close() end
  ftcp, fbuf, fneed = nil, "", nil
end

pushScreens = function(js, isFrame)
  local veh = carVehicle()
  if veh then
    for _, tag in ipairs(screenTags[veh:getId()] or {}) do
      veh:queueJSUITexture(tag, js)
    end
  end
  if next(tvOn) == nil then return end
  if isFrame then                                    -- TVs: at most TV_FPS pictures a second
    local now = os.clockhp()
    if now - tvFrameAt < 1 / TV_FPS then return end
    tvFrameAt = now
  end
  for id in pairs(tvOn) do
    local tv = be:getObjectByID(id)
    if tv then for _, tag in ipairs(screenTags[id] or {}) do tv:queueJSUITexture(tag, js) end end
  end
end

local npShown, npTimer, coverB64, coverTimer = nil, 0, nil, 0

-- ---- other players' car screens (BeamMP "Share my screen"): each player's screen is shown on that player's car
local remoteScr = {}            -- name -> {vid, last, pane}
local ENABLE_R = 'extensions.load("beamdeckScreens"); if not beamdeckScreens.isEnabled() then beamdeckScreens.enable() end'
local DISABLE_R = 'if beamdeckScreens and beamdeckScreens.isEnabled() then beamdeckScreens.disable() end'

local function pushCar(vid, js)
  local veh = be:getObjectByID(vid)
  if not veh then return end
  for _, tag in ipairs(screenTags[vid] or {}) do veh:queueJSUITexture(tag, js) end
end

local function remoteStop(name)
  local r = remoteScr[name]
  if not r then return end
  remoteScr[name] = nil
  local veh = be:getObjectByID(r.vid)
  if veh then veh:queueLuaCommand(DISABLE_R) end
end

local function remoteScreen(body)
  local name, kind, data = body:match("^([^\t]*)\t(.)\t(.*)$")
  local vid = name and mpCarOf[name]
  if not vid then return end
  local r = remoteScr[name]
  if r and r.vid ~= vid then remoteStop(name); r = nil end      -- they changed cars
  if not r then
    r = {vid = vid, last = 0, pane = ""}
    remoteScr[name] = r
  end
  local veh = be:getObjectByID(vid)
  if not veh then return end
  if os.clockhp() - r.last > 2 then veh:queueLuaCommand(ENABLE_R) end   -- (idempotent; also after a re-sync)
  r.last = os.clockhp()
  if kind == "F" then
    pushCar(vid, "window.btAA && btAA(true)")
    pushCar(vid, "window.btFrame && btFrame('data:image/jpeg;base64," .. data .. "')")
  elseif kind == "C" then
    pushCar(vid, "window.btCover && btCover('" .. (#data > 0 and "data:image/jpeg;base64," .. data or "") .. "')")
  elseif kind == "N" then
    local ok, n = pcall(jsonDecode, data)
    if not ok or type(n) ~= "table" then return end
    if n.mode == "none" then return remoteStop(name) end
    if n.mode == "np" then
      pushCar(vid, "window.btAA && btAA(false)")
      pushCar(vid, "window.btNow && btNow(" .. jsonEncode(n.np or false) .. ")")
    end
    r.pane = (n.mode == "aa" and type(n.pane) == "string") and n.pane or ""
    -- their game map at their car's position (the car script draws it, as on the local car)
    veh:queueLuaCommand(string.format('if beamdeckScreens then beamdeckScreens.sync("", 0, true, %q) end', r.pane))
  end
end

local function remoteTick()
  local now = os.clockhp()
  for name, r in pairs(remoteScr) do
    if now - r.last > 6 or not be:getObjectByID(r.vid) then remoteStop(name) end
  end
end

local function frameUpdate(dt)
  local live = state.on and (os.clockhp() - lastStateAt) < 3
  local want = state.aa and live
  if want ~= aaShown then
    aaShown = want
    pushScreens(want and "window.btAA && btAA(true)" or "window.btAA && btAA(false)")
  end
  -- now playing (title / artist / position) for the car screen; re-sent now and then in case the page reloaded
  npTimer = npTimer - dt
  if live then
    local js = "window.btNow && btNow(" .. jsonEncode(state.np or false) .. ")"
    if js ~= npShown or npTimer <= 0 then
      npShown, npTimer = js, 1
      pushScreens(js)
    end
    coverTimer = coverTimer - dt
    if coverB64 and coverTimer <= 0 then        -- and the cover every 5 s
      coverTimer = 5
      pushScreens("window.btCover && btCover('" .. coverB64 .. "')")
    end
  end
  -- stay connected while the helper is around: other players' screens (BeamMP) come this way even when the local
  -- own car screens are off
  if (os.clockhp() - lastStateAt) >= 3 then
    if ftcp then frameClose() end
    return
  end
  if not ftcp then
    fRetry = fRetry - dt
    if fRetry > 0 then return end
    fRetry = 2
    ftcp = socket.tcp()
    ftcp:settimeout(0)
    ftcp:connect("127.0.0.1", FRAME_PORT)
    return
  end
  -- read whatever has arrived
  for _ = 1, 64 do
    local data, err, partial = ftcp:receive(262144)
    local chunk = data or partial
    if chunk and #chunk > 0 then fbuf = fbuf .. chunk end
    if err == "closed" then frameClose() return end
    if not data then break end
  end
  -- messages: a type byte (F = Android Auto frame, C = cover) then base64; only the newest frame is shown
  local latest
  while true do
    if not fneed then
      if #fbuf < 8 then break end
      fneed = tonumber(fbuf:sub(1, 8), 16)
      fbuf = fbuf:sub(9)
      if not fneed then frameClose() return end
    end
    if #fbuf < fneed then break end
    local kind, body = fbuf:sub(1, 1), fbuf:sub(2, fneed)
    fbuf = fbuf:sub(fneed + 1)
    fneed = nil
    if kind == "F" then
      latest = body
    elseif kind == "C" then
      coverB64 = #body > 0 and ("data:image/jpeg;base64," .. body) or ""
      pushScreens("window.btCover && btCover('" .. coverB64 .. "')")
    elseif kind == "R" then
      remoteScreen(body)
    elseif kind == "T" then                      -- a short message from the BeamDeck window (volume, mic muted…)
      ui_message(body, 2, "beamdeck")
    end
  end
  if latest and want then
    pushScreens("window.btFrame && btFrame('data:image/jpeg;base64," .. latest .. "')", true)
  end
end

local function onUpdate(dtReal)
  openSocket()
  receive()
  sendTimer = sendTimer + dtReal
  if sendTimer >= 0.05 then
    sendTimer = 0
    sendInfo()
  end
  if (calibStep > 0 or state.aa) and im.IsMouseClicked(0) and not im.GetIO().WantCaptureMouse then
    local veh = carVehicle()
    if veh then handleClick(veh) end
  end
  frameUpdate(dtReal)
  remoteTick()
  tvTimer = tvTimer + dtReal
  if tvTimer > 0.5 then tvTimer = 0; tvRefresh() end
  keepTimer = keepTimer + dtReal
  if keepTimer >= 2 then
    keepTimer = 0
    syncScreens(true)
  end
end

local function onExtensionUnloaded()
  if udp then udp:close() udp = nil end
  frameClose()
end

-- game controls (Options > Controls > BeamDeck): passed to the BeamDeck window through the helper
M.ptt = function(down) sendHelper({cmd = "ptt", on = down and true or false}) end
M.command = function(name) sendHelper({cmd = name}) end

M.onVehicleSpawned = function(vid)
  if vid then vehKeys[vid] = nil end                -- a config change respawns the car: its key may change
end

M.onVehicleDestroyed = function(vid) if vid then vehKeys[vid] = nil end end
M.onVehicleResetted = function(vid) if vid then vehKeys[vid] = nil end end

M.onTV = function(vid) if vid then tvSet[vid] = true end end

M.onUpdate = onUpdate
M.onScreens = onScreens
M.onLights = onLights
M.onExtensionUnloaded = onExtensionUnloaded

return M
