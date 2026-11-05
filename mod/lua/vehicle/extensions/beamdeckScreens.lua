-- BeamDeck: swaps this car's dashboard / GPS screens to the BeamDeck video page,
-- and shakes the doors / body with the music's bass.
local M = {}

-- bass shake tuning (accelerations in m/s^2 at full bass; window slider scales the input 0..2)
-- doors buzz at 12 Hz (slow enough to see); the body bounces at 3 Hz, near the suspension's own frequency,
-- so the springs amplify it, with a little side-to-side rock
local DOOR_HZ, DOOR_ACCEL = 12, 160
local BODY_HZ, BODY_ACCEL, ROCK_ACCEL = 3, 0.8, 0.3
local MAX_DOOR_NODES, MAX_BODY_NODES = 64, 24
local shake = nil          -- {doors = {{cid, w, side}...}, body = {{cid, w, side}...}}
local bassTarget, bassLevel, shakeT, hooked, physSteps = 0, 0, 0, false, 0

-- state used by enable() before the sections that manage it (declared here so they stay local)
local gameMap, gameMapShown, mapTimer, mapResend
local lightsOn, lightsShown, lightsTimer

local SCREEN_URI = "local://local/ui/beamdeck/car.html"
local NAVI_DEFAULT = "local://local/vehicles/common/navi_screen.html"
local screens = nil
local enabled = false

local function report()
  local tags = {}
  for _, s in ipairs(screens or {}) do tags[#tags + 1] = s.tag end
  obj:queueGameEngineLua(string.format("if beamdeck then beamdeck.onScreens(%d, %d, %q) end",
    obj:getID(), #tags, jsonEncode(tags)))
end

local function collect()
  screens = {}
  local seen = {}
  for _, c in pairs(v.data.controller or {}) do
    local name = c.name or c.fileName
    local d = tableMergeRecursive(deepcopy(c), deepcopy(v.data[name] or {}))
    for k, sub in pairs(d) do
      if type(k) == "string" and k:sub(1, 14) == "configuration_" and type(sub) == "table" then
        tableMergeRecursive(d, sub)
      end
    end
    local s
    if d.materialName and d.htmlPath then
      s = {tag = d.materialName, uri = d.htmlPath, w = d.displayWidth or 512, h = d.displayHeight or 256}
    elseif type(c.fileName) == "string" and c.fileName:find("beamNavigator") then
      s = {tag = d.screenMaterialName or "@screen_gps", uri = d.htmlFilePath or NAVI_DEFAULT,
           w = d.textureWidth or 256, h = d.textureHeight or 128}
    end
    if s and not seen[s.tag] then
      seen[s.tag] = true
      s.controller = name
      s.data = d
      screens[#screens + 1] = s
    end
  end
end

local reinitTimer = nil

local function navigate(tag, uri)
  -- createWebView on an existing screen is ignored, so steer the screen's page instead
  obj:queueWebViewJS(tag, string.format("window.location.replace(%q)", uri))
end

local function enable()
  collect()
  for _, s in ipairs(screens) do
    navigate(s.tag, SCREEN_URI)
  end
  enabled = true
  reinitTimer = nil
  gameMapShown = nil
  lightsShown = nil          -- a fresh page needs the dim state again
  report()
end

local function disable()
  if not screens then collect() end
  for _, s in ipairs(screens) do
    navigate(s.tag, s.uri)
  end
  enabled = false
  reinitTimer = 1.0  -- give the original pages a moment to load, then let their controllers set them up
  report()
end

local function reinitControllers()
  for _, s in ipairs(screens or {}) do
    local ctrl = controller.getController(s.controller)
    if ctrl then
      if ctrl.initSecondStage and ctrl.initSecondStage ~= nop then
        pcall(ctrl.initSecondStage, s.data)
      elseif ctrl.init and ctrl.init ~= nop then
        pcall(ctrl.init, s.data)
      end
    end
  end
end

-- ---------------------------------------------------------------- game map over Android Auto's map pane
gameMap, gameMapShown, mapTimer, mapResend = "", nil, 0, 0   -- gameMap: "left", "right" or "" (declared at the top)

local function pushAll(js)
  for _, s in ipairs(screens or {}) do obj:queueWebViewJS(s.tag, js) end
end

local function showGameMap(pane)
  local first = pane ~= "" and pane ~= gameMapShown      -- new or moved map: it needs the map data
  gameMapShown = pane
  pushAll(string.format("window.btGameMap && btGameMap(%q)", pane))
  if first then                                   -- the map itself comes from the game, like its own GPS screens
    for _, s in ipairs(screens or {}) do
      obj:queueGameEngineLua(string.format("extensions.ui_uinavi.requestVehicleDashboardMap(%q, nil, %d)", s.tag, obj:getID()))
    end
  end
end

local function mapUpdate(dt)
  if not enabled then return end
  if gameMap ~= gameMapShown then showGameMap(gameMap) end
  if gameMap == "" then return end
  mapResend = mapResend - dt                   -- re-send now and then (the page may have reloaded)
  if mapResend <= 0 then mapResend = 5; gameMapShown = nil; showGameMap(gameMap) end
  mapTimer = mapTimer + dt
  if mapTimer < 1 / 15 then return end
  mapTimer = 0
  local pos = obj:getPosition()                -- same values the game's beamNavigator sends
  local speed = (electrics.values.airspeed or 0) * 3.6
  pushAll(string.format("map.updateData(%s)", jsonEncode({
    x = pos.x, y = pos.y, rotation = math.deg(obj:getDirection()) + 180, speed = speed,
    zoom = math.min(150 + speed * 1.5, 250), ignitionLevel = electrics.values.ignitionLevel or 2})))
end

-- ---------------------------------------------------------------- headlights on → dim the screen
lightsOn, lightsShown, lightsTimer = false, nil, 0          -- (declared at the top)
-- ---------------------------------------------------------------- how open the car is (for the outside sound)
-- Openings by side: L / R / F / B / T, each 0..1. Doors, trunks and tailgates are open when their latch
-- (advancedCouplerControl, "<name>_notAttached") isn't attached, which also covers torn-off doors; glass counts
-- when its break group has broken (beamstate.brokenBreakGroups), a cracked windscreen by its deform damage.
local function sideOf(name)
  local n = name:lower()
  if n:find("windshield") or n:find("windscreen") or n:find("front") then return "F" end
  if n:find("roof") or n:find("sunroof") then return "T" end
  if n:find("trunk") or n:find("tailgate") or n:find("hatch") or n:find("liftgate") or n:find("rear_?glass")
     or n:find("backglass") or n:find("backlight") or n:find("boot") then return "B" end
  if n:find("_fl") or n:find("_rl") or n:find("_l_") or n:find("_l$") or n:find("left") or n:find("fl_") or n:find("rl_") then return "L" end
  if n:find("_fr") or n:find("_rr") or n:find("_r_") or n:find("_r$") or n:find("right") or n:find("fr_") or n:find("rr_") then return "R" end
  return nil
end

local function isGlass(name)
  local n = name:lower()
  if n:find("backlight") then return true end   -- "backlight" = the rear window
  if n:find("light") or n:find("lamp") or n:find("mirror") or n:find("gauge") or n:find("screen_") then return false end
  return n:find("glass") or n:find("window") or n:find("windshield") or n:find("windscreen")
end

local openNames = {}
local function openness()
  local o = {L = 0, R = 0, F = 0, B = 0, T = 0}
  openNames = {}
  local function add(side, v, name)
    if #openNames < 10 then openNames[#openNames + 1] = name end
    if side then o[side] = math.min(1, o[side] + v)
    else for k in pairs(o) do o[k] = math.min(1, o[k] + v * 0.4) end end
  end
  for k, val in pairs(electrics.values) do         -- open doors, trunk lids, tailgates
    if type(k) == "string" and val == 1 and k:sub(-12) == "_notAttached" then
      local n = k:lower()
      if n:find("door") or n:find("trunk") or n:find("tailgate") or n:find("hatch") or n:find("liftgate") or n:find("boot") then
        add(sideOf(k), n:find("door") and 0.35 or 0.3, k)
      end
    end
  end
  for g in pairs(beamstate.brokenBreakGroups or {}) do            -- broken windows
    if type(g) == "string" and isGlass(g) then
      local side = sideOf(g)
      add(side, 0.28, g)      -- a broken pane: ~84 % of an open door
    end
  end
  -- shattered glass: windows swap to their broken look through a deform group (doorglass_L_break,
  -- backlight_break…), so any damage there means the pane is gone; a windscreen only cracks, so it counts less
  for g, d in pairs(beamstate.deformGroupDamage or {}) do
    if type(g) == "string" and isGlass(g) and type(d) == "table" and (d.damage or 0) > 0 then
      local n, side = g:lower(), sideOf(g)
      local cracks = n:find("windshield") or n:find("windscreen")
      add(side, cracks and math.min(0.25, d.damage * 0.25) or 0.28, g)
    end
  end
  return o
end

local function lightsUpdate(dt)
  lightsTimer = lightsTimer + dt
  if lightsTimer < 0.5 then return end
  lightsTimer = 0
  lightsOn = (electrics.values.lights_state or 0) > 0
  if enabled and lightsOn ~= lightsShown then
    lightsShown = lightsOn
    pushAll(lightsOn and "window.btDim && btDim(true)" or "window.btDim && btDim(false)")
  end
  obj:queueGameEngineLua(string.format("if beamdeck then beamdeck.onLights(%d, %s, %q) end", obj:getID(), tostring(lightsOn),
    jsonEncode({level = math.floor(bassLevel * 100) / 100, target = bassTarget, steps = physSteps,
                doors = shake and #shake.doors or -1, body = shake and #shake.body or -1, open = openness(),
                openings = openNames})))
end

local function sync(file, pos, paused, gm)
  if not enabled then return end
  gameMap = type(gm) == "string" and gm or ""
  local js = string.format("window.btSync && btSync(%s)", jsonEncode({file = file, pos = pos, paused = paused}))
  for _, s in ipairs(screens) do
    obj:queueWebViewJS(s.tag, js)
  end
end

-- ---------------------------------------------------------------- bass shake
local right, up = vec3(0, 0, 0), vec3(0, 0, 1)
local doorVec, bodyVec = vec3(), vec3()

local function groupHas(g, word)
  if type(g) == "string" then return g:lower():find(word, 1, true) ~= nil end
  if type(g) == "table" then
    for _, x in pairs(g) do
      if type(x) == "string" and x:lower():find(word, 1, true) then return true end
    end
  end
  return false
end

local function sample(list, maxN)
  if #list <= maxN then return list end
  local out, step = {}, #list / maxN
  for i = 0, maxN - 1 do out[#out + 1] = list[math.floor(i * step) + 1] end
  return out
end

local function setupShake()
  -- at runtime nodes keep only their first group's number (firstGroup); v.data.groups maps name -> number
  local groupName = {}
  for name, idx in pairs(v.data.groups or {}) do groupName[idx] = name end
  local doors, body, totalW = {}, {}, 0
  for _, n in pairs(v.data.nodes or {}) do
    if type(n) == "table" and n.cid then
      local w = type(n.nodeWeight) == "number" and n.nodeWeight or 1
      totalW = totalW + w
      local g = n.group or groupName[n.firstGroup]
      -- every stock car names its panels <car>_door_FL / _door_R / _trunk / _tailgate (checked across the
      -- lineup); skip parts that only mention them (door flares and nets, trunk antennas, frames)
      local skip = groupHas(g, "flare") or groupHas(g, "net") or groupHas(g, "antenna") or groupHas(g, "frame")
      if not skip and groupHas(g, "door") then
        local side = (n.pos and n.pos.x or 0) >= 0 and 1 or -1
        doors[#doors + 1] = {n.cid, w, side}        -- doors flex in and out
      elseif not skip and (groupHas(g, "trunk") or groupHas(g, "tailgate") or groupHas(g, "hatch") or groupHas(g, "liftgate")) then
        doors[#doors + 1] = {n.cid, w, 0}           -- lids buzz up and down
      elseif not (groupHas(g, "wheel") or groupHas(g, "tire") or groupHas(g, "hub") or groupHas(g, "spring")) then
        body[#body + 1] = {n.cid, w, (n.pos and n.pos.x or 0) >= 0 and 1 or -1}
      end
    end
  end
  table.sort(body, function(a, b) return a[1] < b[1] end)
  local doorSel = sample(doors, MAX_DOOR_NODES)
  -- door nodes picked by sampling stand in for the whole door: scale their weight up
  local doorScale = #doors / math.max(1, #doorSel)
  for _, d in ipairs(doorSel) do d[2] = d[2] * doorScale end
  local bodySel = sample(body, MAX_BODY_NODES)
  local per = totalW / math.max(1, #bodySel)
  for _, b in ipairs(bodySel) do b[2] = per end
  shake = {doors = doorSel, body = bodySel}
end

local function setBass(x)
  bassTarget = math.max(0, math.min(2, x or 0))
  if bassTarget > 0 and not hooked then
    if not shake then setupShake() end
    hooked = true
    enablePhysicsStepHook()          -- extensions' onPhysicsStep only runs once this is switched on
    extensions.hookUpdate("onPhysicsStep")
  end
end

local function updateGFX(dt)
  mapUpdate(dt)
  lightsUpdate(dt)
  if reinitTimer then
    reinitTimer = reinitTimer - dt
    if reinitTimer <= 0 then reinitTimer = nil; reinitControllers() end
  end
  if not hooked then return end
  local f = obj:getDirectionVector()
  up:set(obj:getDirectionVectorUp())
  right:set(f:cross(up))
  right:normalize()
end

local function onPhysicsStep(dt)
  if not shake then return end
  physSteps = physSteps + 1
  bassLevel = bassLevel + (bassTarget - bassLevel) * math.min(1, dt * 25)
  if bassLevel < 0.005 then return end
  shakeT = shakeT + dt
  -- only real bass hits count: nothing below 0.3, growing fast above it; the body only moves on heavy hits
  local e = math.max(0, bassLevel - 0.3) / 0.7
  e = math.min(1.5, e * e)
  local eb = math.min(1, math.max(0, e - 0.5))
  local sd = math.sin(shakeT * DOOR_HZ * 2 * math.pi) * DOOR_ACCEL * e
  local sb = math.sin(shakeT * BODY_HZ * 2 * math.pi) * BODY_ACCEL * eb
  for i = 1, #shake.doors do
    local d = shake.doors[i]
    if d[3] ~= 0 then                 -- a door: in and out
      doorVec:set(right.x, right.y, right.z)
      doorVec:setScaled(sd * d[2] * d[3])
    else                              -- a trunk lid or tailgate: up and down, a bit gentler
      doorVec:set(up.x, up.y, up.z)
      doorVec:setScaled(sd * d[2] * 0.6)
    end
    obj:applyForceVector(d[1], doorVec)
  end
  local sr = math.sin(shakeT * BODY_HZ * 2 * math.pi + 1.3) * ROCK_ACCEL * eb
  for i = 1, #shake.body do
    local b = shake.body[i]
    bodyVec:set(up.x * sb + right.x * sr * b[3], up.y * sb + right.y * sr * b[3], up.z * sb + right.z * sr * b[3])
    bodyVec:setScaled(b[2])
    obj:applyForceVector(b[1], bodyVec)
  end
end

local function onExtensionLoaded()
  collect()
  report()
end

M.enable = enable
M.disable = disable
M.sync = sync
M.setBass = setBass
M.updateGFX = updateGFX
M.onPhysicsStep = onPhysicsStep
M.isEnabled = function() return enabled end
M.onExtensionLoaded = onExtensionLoaded

return M
