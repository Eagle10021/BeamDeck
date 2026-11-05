-- BeamDeck TV: creates the live page on the TV's screen panel. BeamDeck's game script finds TVs through
-- onTV and shows its car screen there (the vehicle extension's screen discovery sees materialName/htmlPath).
local M = {}
M.type = "auxiliary"

local htmlTexture = require("htmlTexture")     -- (not a global in the vehicle's Lua: stock screen controllers require it)

local function init(jbeamData)
  htmlTexture.create(jbeamData.materialName or "@beamdeck_tv", jbeamData.htmlPath or "local://local/ui/beamdeck/tv.html",
    jbeamData.displayWidth or 640, jbeamData.displayHeight or 480, 30, "automatic")
  obj:queueGameEngineLua(string.format("if beamdeck and beamdeck.onTV then beamdeck.onTV(%d) end", obj:getID()))
end

M.init = init
return M
