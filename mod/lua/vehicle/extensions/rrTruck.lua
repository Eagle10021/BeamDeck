-- rrTruck: vehicle-side helper for the Real Routes freight panel.
-- Holds the engine at a chosen RPM for PTO and hydraulic work (a dump bed, a crane, a mixer's drum), like the game's own
-- "raise idle" switch but with any speed in between. Runs after the controllers' updateGFX (main.lua: controller, then
-- extensions, then the powertrain reads the engine's idle override), so this value is the one the engine sees.
local M = {}

local RPM_TO_AV = 0.104719755          -- rpm -> rad/s
local MIN_RPM, MAX_RPM = 700, 2400
local DRIVE_OFF_MS = 3                 -- wheel speed above which the hold lets go: this is for work while parked

local targetRPM = nil                  -- nil: off
local engine

local function getEngine()
  if not engine then engine = powertrain.getDevice("mainEngine") end
  return engine
end

-- also switches the game's own hydraulics controller, so a pump that waits for raised revs gets connected
local function hydraulics(on)
  local c = controller.getController("hydraulicsControl")
  if c and c.setIdleRaise then c.setIdleRaise(on) end
end

local function setPtoRPM(rpm)
  rpm = tonumber(rpm)
  if rpm and rpm > 0 then
    targetRPM = math.max(MIN_RPM, math.min(MAX_RPM, rpm))
    hydraulics(true)
  else
    targetRPM = nil
    local e = getEngine()
    if e then e.idleAVOverwrite, e.maxIdleThrottleOverwrite = 0, 0 end
    hydraulics(false)
  end
end

local function getRPM() return targetRPM end

local function updateGFX(dt)
  if not targetRPM then return end
  local e = getEngine()
  if not e then return end
  if (electrics.values.wheelspeed or 0) > DRIVE_OFF_MS then return end
  e.idleAVOverwrite = targetRPM * RPM_TO_AV
  e.maxIdleThrottleOverwrite = 1
end

local function onReset() targetRPM = nil end

M.setPtoRPM = setPtoRPM
M.getRPM = getRPM
M.updateGFX = updateGFX
M.onReset = onReset
return M
