-- BeamDeck head tracking: a camera filter that turns the view with the player's head. Like the game's TrackIR filter it runs on
-- top of whichever camera is active; the head pose comes from the beamdeckHead extension (a webcam, tracked in the BeamDeck page).
local C = {}
C.__index = C

function C:init()
  self.isGlobal = true
  self.isFilter = true
  self.hidden = true
  self.runningOrder = 0.5
end

-- v turned about the unit axis k by ang (radians, right-hand rule)
local function turned(v, k, ang)
  local c, s = math.cos(ang), math.sin(ang)
  return v * c + k:cross(v) * s + k * (k:dot(v) * (1 - c))
end

local WORLD_UP = vec3(0, 0, 1)

function C:update(data)
  if data.openxrSessionRunning then return true end              -- a VR headset does its own head tracking
  local H = extensions.beamdeckHead
  if not (H and H.active()) then return true end
  local name
  pcall(function() name = core_camera.getActiveCamName() end)
  if not H.isFirstPerson(name) then return true end                 -- first person only: the other views are left alone
  local p = H.get()
  local rot = data.res.rot
  -- the head's movement, in the camera's own axes (x right, y forward, z up): x right, z toward the screen, y up, in centimetres
  data.res.pos = data.res.pos + (rot * vec3(p.x, p.z, p.y)) * 0.01
  -- the head's turn, done with vectors (a quaternion product here turned out to compose differently from what the maths suggests, so the
  -- turn is worked out as what a person sees and the camera rotation is rebuilt from it; tools/test_head_game.py checks it in the game):
  --   yaw: the heading, about the world's vertical (right is clockwise from above), so the horizon stays level
  --   pitch: the height of the view, about the view's horizontal right-hand axis (up is +)
  --   roll: about the view direction (tilting the head right turns the view clockwise)
  local f, u = rot * vec3(0, 1, 0), rot * vec3(0, 0, 1)
  f, u = turned(f, WORLD_UP, -math.rad(p.yaw)), turned(u, WORLD_UP, -math.rad(p.yaw))
  local right = f:cross(WORLD_UP)
  if right:length() < 1e-4 then right = rot * vec3(1, 0, 0) end                -- looking straight up or down: keep the camera's own right
  right = right:normalized()
  f, u = turned(f, right, math.rad(p.pitch)), turned(u, right, math.rad(p.pitch))
  f = f:normalized()
  u = turned(u, f, math.rad(p.roll)):normalized()
  data.res.rot = quatFromDir(f, u)
  return true
end

-- DO NOT CHANGE CLASS IMPLEMENTATION BELOW

return function(...)
  local o = ... or {}
  setmetatable(o, C)
  o:init()
  return o
end
