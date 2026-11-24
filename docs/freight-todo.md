# Freight: where things stand (2026-11-21, late)

Written so the next session can pick up without the chat. Nothing here has been tried in the real game with a friend yet.

## Built and passing its tests

- **Freight multiplayer** (ETS2-style): shared job board, convoys with a 4-letter code (+5% pay per extra driver, up to +20%), drivers online with a **Route** button (GPS to a friend), **On the road** (take the same job as a friend), radio chat, delivery announcements. No leaderboard (dropped on purpose).
  - Hub: `relay/freight-hub.py`. **Already running on Phrolova** as container `beamdeck-freight-hub` (`~/beamdeck-relay`, port 47141, Tailscale only). Check: `http://100.64.167.103:47141/api/ping`. The old compose is saved as `compose.yaml.bak-before-hub`. Remove it with `ssh phrolova 'cd ~/beamdeck-relay && docker compose rm -sf freight-hub'`.
  - Client: `realroutes/freight_mp.py`, wired in `realroutes_helper.py` and `freight.py`. Prefs: `mpOn`, `mpHub`, `mpName`. The hub address falls back to the music relay's host on port 47141.
- **Time:** every job gets spare time (60 min + 0.2 min/km, up to 120; express contracts keep half). The job clock stops while the game is paused or silent. The mod sends `paused` in the pose message.
- **Rules** split into speeding fines, cargo damage and late delivery (each can be switched off).
- **Redesign:** Overpass font, job tickets with a tear-off stub and 416/401 shields, amber pay, text tabs with a quiet pill (no orange underlines), the convoy code as a licence plate, the speedometer and convoy in one pill, a 12-hour clock (setting: 12 / 24 / follow device).
- **Last edits (made but never screenshotted after the final size change):** the Speed tile was removed from the gauge grid (Engine now spans the row), the speed limit is a "MAX 100" sign under the speedometer on the map, and a traditional compass card (dial that turns, pointer on top, heading in degrees) is at the top right of the map. The compass was just enlarged to 112 px because the heading text collided with the dial letters. It was checked on 2026-11-22 and looks right.

Tests (all passed when last run alone): `python3 relay/test_hub.py`, `python3 realroutes/tools/test_mp.py`, `python3 realroutes/tools/test_freight.py`.

## Added 2026-11-22 (afternoon)

- **Truck pictures in the Garage** (`realroutes/previews.py`, route `/api/vehicle/preview?model=&config=`). Order: the user folder (a saved config's own thumbnail, `vehicles/<model>/<name>.jpg`), mods' folders, the game's `content/vehicles/<model>.zip`, then the model's `default.jpg`. The helper finds the game folders from the mod's hello (`gamePath`, `userPath`, so **Ctrl+L is needed once**), or from `BEAMNG_GAME_DIR` / `BEAMNG_USER_DIR`, or standard Steam locations (including libraryfolders.vdf). Test: `python3 realroutes/tools/test_previews.py`. Layout fixes in the same change: Freight page columns can shrink (`minmax(0,1fr)`), so nothing overflows the screen.
- **Supply chains, slice 1** (`D.CHAINS` in `freight_data.py`, `Freight._chain_offer`): a delivery of logs / lumber / steel / pipes / gravel / milk / container / general freight leaves the next job at the drop-off (+25%, +10% more per further link, lasts 2 h), posted to the shared hub (or your own board when multiplayer is off) and announced on the radio. The board has a **Supply chains** filter and tag. Slice 2 ideas: a map of active chains on the Multiplayer tab, town needs ("Brockville is out of fuel") that create demand, and an AI dispatcher voice on the radio using the local model.

## Do first tomorrow

1. **Done 2026-11-22: the helper and tablet page are installed and BeamDeck restarted.** The game's mod copy is installed too, but BeamNG still has to reload it. Redo the copy after any further change.
   - `cp -r realroutes/. ~/.local/bin/realroutes/` and `cp beamdeck-server.py ~/.local/bin/`
   - `cp -r mod/. ~/.local/share/BeamNG/BeamNG.drive/current/mods/unpacked/beamdeck/` (the same folder as `~/GamesAndFiles/BeamNG Mods and User Stuff/current/...`)
   - `systemctl --user restart beamdeck`, then press **Ctrl+L in BeamNG** (needed for the pause signal, the BeamMP nickname and the load fixes).
2. Look at the map in the cockpit: compass size, the limit sign, the pill. Then run `python3 realroutes/tools/ui_test.py <outdir>`.
3. **Try multiplayer for real:** switch it on in Freight → Multiplayer, have a friend connect over Tailscale, open a convoy, join it, and check the bonus on delivery.

## Known problems / unfinished

- **`tools/ui_test.py` (60 checks, about 5 minutes) passed cleanly on 2026-11-22.** Last night's flaky failures had two causes, both fixed: fixed sleeps in the multiplayer steps (now waits on conditions), and a **test-isolation bug**: the in-process helper read this machine's real BeamDeck config, so it could talk to the REAL Phrolova hub before the test hub address was set. `fake_game.start()` now blanks the relay hint and the tests set the hub address before switching multiplayer on. Earlier test runs may have left test jobs on the real hub; they expire by themselves within about 30 minutes.
- Shared jobs are now a mix that includes jobs made for the poster's own truck (without the "Made for your truck" tag), so a board always has work for several kinds of truck. The hub-side top-up also retries within about 6 seconds and prints a traceback if making jobs fails.
- **Not verified in the real game** (only in the fake game): the spawn / hitch / built-in load part reload, the pause signal (`simTimeAuthority.getPause()` with a `bullettime` fallback), the BeamMP nickname (`MPConfig.getNickname()`), `gpsTo` for Route, truck controls, the PTO hold.
- Chat only shows system lines like "joined" for players who hit Send or deliver; nothing is spoken aloud.
- The Android Auto app (0.6.0) has not been rebuilt or installed, and does not know about convoys. The Soundboard Android app's new mix-level slider also needs a build (the offline Gradle cache could not find the Android plugin).
- Other BeamDeck copies are not synced: `Shorekeeper-Arch/tools/beamdeck` and `~/GamesAndFiles/BeamDeck` do not have the `realroutes` folder.
- Rides mode seat/passenger parts now send only the changed slots (the full-tree send froze the game); also unverified in game.

## Deferred: the Windows .exe and auto-update (do it on Windows)

- Plan: a PyInstaller launcher that checks `http://<hub>:47141/update/manifest.json`, downloads the payload zip from `/update/files/` (SHA-256 checked), installs the mod into BeamNG's user folder, downloads ffmpeg and yt-dlp if missing, makes the Start-menu shortcut, and starts the helper and window. The hub already serves `/update/` from `~/beamdeck-relay/hub-data/update/` (`manifest.json` plus `files/`). Publishing would be a `publish-windows.sh` that builds the payload and `scp`s it there.
- Piper speech stays optional (it needs a system Python); without it Real Routes speaks on the phone or tablet.
- Building it on Linux through Wine was blocked by the permission classifier (a detached download-and-run of the Windows Python installer). It needs no sudo. On Windows it is a normal `pip install pyinstaller`.

## Commit and push (the user pushes)

```sh
cd ~/Documents/"Personal Project"/BeamDeck && git add -A && git commit -m "Freight multiplayer (hub, convoys, shared jobs), spare time and pause, redesign, map compass and limit sign" && git push
```
