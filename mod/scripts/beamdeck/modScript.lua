-- BeamDeck: start the background link to the BeamDeck window when the game loads
setExtensionUnloadMode("beamdeck", "manual")
extensions.load("beamdeck")
-- Real Routes (real bus routes on BeamNG.Earth): idle unless BeamNG.Earth is running
setExtensionUnloadMode("realroutes", "manual")
extensions.load("realroutes")
-- Head tracking: the player's head, from a webcam tracked in the BeamDeck page, turns the camera
setExtensionUnloadMode("beamdeckHead", "manual")
extensions.load("beamdeckHead")
