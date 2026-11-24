-- BeamDeck head tracking: the head pose (from a webcam, tracked in the BeamDeck console page) arrives here over UDP, and the
-- camera filter core/cameraModes/beamdeckhead.lua turns the camera with it. It listens on 127.0.0.1:47152 and does nothing
-- until a packet arrives. The pose arrives about 30 times a second and the frames run faster, so it is smoothed here.
--
--   {"type":"head","yaw":deg,"pitch":deg,"roll":deg,"x":cm,"y":cm,"z":cm,"on":true}
--       yaw: + is the head turned right, pitch: + is looking up, roll: + is tilted right (clockwise seen from behind),
--       x: + right, y: + up, z: + toward the screen (all from the centre position the page recentred on)
--   {"type":"probe"}  answers the sender with what the camera is doing (the tests and the settings page use it)
--   {"type":"cam","name":"driver"}  switches the camera by name (tests)
local M = {}
local socket = require("socket")

local PORT = 47152
local LIMITS = {yaw = 180, pitch = 90, roll = 90, x = 60, y = 60, z = 60}
local udp
local target = {yaw = 0, pitch = 0, roll = 0, x = 0, y = 0, z = 0}
local cur = {yaw = 0, pitch = 0, roll = 0, x = 0, y = 0, z = 0}
local clock, lastPacket, packets = 0, -1e9, 0
local enabled = true
local TAU_LIVE = 0.03        -- seconds: hides the 30 Hz steps without a noticeable lag
local TAU_LOST = 0.35        -- seconds: how fast the view returns to the centre when the packets stop
local STALE_S = 0.6

-- head tracking only turns the first-person views: the driver's seat (and the other seats) and the fixed onboard cameras. In the chase,
-- orbit, external and free views the head should not move the camera, so those are left alone.
local function firstPerson(name)
  if type(name) ~= "string" then return false end
  return name == "driver" or name == "passenger" or name:find("^onboard") ~= nil or name:find("^occupant") ~= nil
end

local function clampTo(v, lim)
  v = tonumber(v) or 0
  if v ~= v then return 0 end
  return math.max(-lim, math.min(lim, v))
end

local function player()
  return getPlayerVehicle and getPlayerVehicle(0) or nil
end

local function probeReply()
  local cam, q, fwd, up, pos
  pcall(function() cam = core_camera.getActiveCamName() end)
  pcall(function()
    q = core_camera.getQuat()
    local f, u = q * vec3(0, 1, 0), q * vec3(0, 0, 1)
    fwd, up = {f.x, f.y, f.z}, {u.x, u.y, u.z}
    local p = core_camera.getPosition()
    pos = {p.x, p.y, p.z}
  end)
  local v = player()
  local vf, vu
  if v then
    local f, u = v:getDirectionVector(), v:getDirectionVectorUp()
    vf, vu = {f.x, f.y, f.z}, {u.x, u.y, u.z}
  end
  return jsonEncode({type = "probe", on = enabled, packets = packets, age = clock - lastPacket, cur = cur, target = target, cam = cam, firstPerson = firstPerson(cam),
                     fwd = fwd, up = up, pos = pos, carFwd = vf, carUp = vu, level = getCurrentLevelIdentifier and getCurrentLevelIdentifier() or nil})
end

local function handle(msg, ip, port)
  local t = msg.type
  if t == "head" then
    for k, lim in pairs(LIMITS) do target[k] = clampTo(msg[k], lim) end
    if msg.on ~= nil then enabled = msg.on and true or false end
    lastPacket = clock
    packets = packets + 1
  elseif t == "probe" then
    if udp and ip and port then udp:sendto(probeReply(), ip, port) end
  elseif t == "cam" and type(msg.name) == "string" then
    local ok, err = pcall(function()
      local id = be:getPlayerVehicleID(0)
      if not (id and id >= 0 and core_camera.setVehicleCameraByNameWithId(id, msg.name)) then core_camera.setByName(0, msg.name) end
    end)
    if not ok then log("W", "beamdeckHead", "camera " .. msg.name .. ": " .. tostring(err)) end
  end
end

function M.onExtensionLoaded()
  udp = socket.udp()
  udp:settimeout(0)
  local ok, err = udp:setsockname("127.0.0.1", PORT)
  if not ok then
    log("W", "beamdeckHead", "could not listen on " .. PORT .. ": " .. tostring(err))
    udp:close(); udp = nil
  else
    log("I", "beamdeckHead", "head tracking listens on 127.0.0.1:" .. PORT)
  end
end

function M.onUpdate(dtReal)
  if not udp then return end
  dtReal = math.min(dtReal or 0.016, 0.1)
  clock = clock + dtReal
  for _ = 1, 200 do                                         -- everything that arrived since the last frame
    local data, ip, port = udp:receivefrom()
    if not data then break end
    local ok, msg = pcall(jsonDecode, data)
    if ok and type(msg) == "table" then handle(msg, ip, port) end
  end
  local stale = clock - lastPacket > STALE_S
  local a = 1 - math.exp(-dtReal / (stale and TAU_LOST or TAU_LIVE))
  for k in pairs(cur) do
    local goal = stale and 0 or target[k]
    cur[k] = cur[k] + (goal - cur[k]) * a
  end
end

-- what the camera filter reads: the smoothed pose, and whether there is anything to apply
M.isFirstPerson = firstPerson
function M.get() return cur end
function M.active()
  if not enabled or not udp then return false end
  if clock - lastPacket <= STALE_S then return true end
  for _, v in pairs(cur) do if math.abs(v) > 0.01 then return true end end       -- still easing back to the centre
  return false
end

function M.onExtensionUnloaded()
  if udp then udp:close(); udp = nil end
end

return M
