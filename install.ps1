# BeamDeck installer for Windows (PowerShell):   powershell -ExecutionPolicy Bypass -File install.ps1
#   Needs Python 3, ffmpeg and yt-dlp on PATH. If they're missing:
#     winget install Python.Python.3.12 Gyan.FFmpeg yt-dlp.yt-dlp
#   Optional extras (the script says which are missing):
#     - App sound through the car / Android Auto's sound: VB-CABLE (https://vb-audio.com/Cable/) and NirSoft's
#       SoundVolumeCommandLine (svcl.exe, https://www.nirsoft.net/utils/sound_volume_command_line.html) put in
#       %LOCALAPPDATA%\BeamDeck\app
#     - Now playing from apps:  py -m pip install winrt-runtime winrt-Windows.Media.Control winrt-Windows.Foundation
#     - Android Auto: adb (winget install Google.PlatformTools); the head unit is downloaded below with -AndroidAuto
param([switch]$AndroidAuto, [string]$BeamNGUserFolder = "")
$ErrorActionPreference = "Stop"
$here = Split-Path -Parent $MyInvocation.MyCommand.Path
$app  = Join-Path $env:LOCALAPPDATA "BeamDeck\app"
$data = Join-Path $env:LOCALAPPDATA "BeamDeck"

# BeamNG's user folder: the 'current' version folder, else the newest version folder
if (-not $BeamNGUserFolder) {
    $bases = @("$env:LOCALAPPDATA\BeamNG\BeamNG.drive", "$env:LOCALAPPDATA\BeamNG.drive")
    # a user folder moved in BeamNG's launcher is only recorded in BeamNG.drive.ini ("userFolder = D:\...")
    $ini = "$env:LOCALAPPDATA\BeamNG\BeamNG.drive.ini"
    if (Test-Path $ini) {
        $m = Select-String -Path $ini -Pattern '^\s*userFolder\s*=\s*(.+?)\s*$' | Select-Object -First 1
        if ($m) { $bases = @($m.Matches[0].Groups[1].Value.Trim('"')) + $bases }
    }
    foreach ($base in $bases) {
        if (Test-Path "$base\current") { $BeamNGUserFolder = "$base\current"; break }
        $v = Get-ChildItem $base -Directory -ErrorAction SilentlyContinue | Where-Object { $_.Name -match '^\d' } |
             Sort-Object { [version]($_.Name -replace '[^\d.]', '') } | Select-Object -Last 1
        if ($v) { $BeamNGUserFolder = $v.FullName; break }
        if (Test-Path $base) { $BeamNGUserFolder = $base; break }
    }
}
if (-not $BeamNGUserFolder) { throw "BeamNG's user folder wasn't found; run again with -BeamNGUserFolder <path>" }
$mod = Join-Path $BeamNGUserFolder "mods\unpacked\beamdeck"

New-Item -ItemType Directory -Force $app, "$mod\beamdeck_media" | Out-Null
foreach ($f in "beamdeck-server.py", "beamdeck-ui.html", "beamdeck-icon.svg", "beamdeck-launch.py") {
    Copy-Item (Join-Path $here $f) $app -Force
}
Copy-Item (Join-Path $here "mod\*") $mod -Recurse -Force

# Start menu shortcut: pythonw runs the launcher without a console window
# Microsoft Store Python is skipped: its AppData virtualization hides %LOCALAPPDATA%\BeamDeck from it
$notStore = { $_.Source -and $_.Source -notmatch '\\WindowsApps\\' }
$py = (Get-Command pythonw.exe -All -ErrorAction SilentlyContinue | Where-Object $notStore | Select-Object -First 1).Source
if (-not $py) { $py = (Get-ChildItem "$env:LOCALAPPDATA\Programs\Python\Python3*\pythonw.exe", "$env:ProgramFiles\Python3*\pythonw.exe" -ErrorAction SilentlyContinue | Sort-Object FullName | Select-Object -Last 1).FullName }
if (-not $py) { $py = (Get-Command python.exe -All -ErrorAction SilentlyContinue | Where-Object $notStore | Select-Object -First 1).Source }
if (-not $py) { Write-Warning "Python 3 (python.org, not the Microsoft Store one) wasn't found: winget install Python.Python.3.12" }
else {
    $lnk = Join-Path ([Environment]::GetFolderPath("Programs")) "BeamDeck.lnk"
    $s = (New-Object -ComObject WScript.Shell).CreateShortcut($lnk)
    $s.TargetPath = $py; $s.Arguments = "`"$app\beamdeck-launch.py`""; $s.WorkingDirectory = $app
    $s.Description = "Videos, music and Android Auto in any BeamNG car"; $s.Save()
}

if ($AndroidAuto -and -not (Test-Path "$data\dhu\desktop-head-unit.exe")) {
    $zip = Join-Path $env:TEMP "beamdeck-dhu.zip"
    Invoke-WebRequest "https://dl.google.com/android/repository/desktop-head-unit-windows-x64_r02.1.zip" -OutFile $zip
    if ((Get-FileHash $zip -Algorithm SHA1).Hash.ToLower() -ne "c27bf84e59dda7b79315b6ca2a314063feffd6ac") { throw "Head unit download didn't match Google's checksum" }
    Expand-Archive $zip -DestinationPath "$data\dhu" -Force
    Remove-Item $zip
}

foreach ($t in "ffmpeg", "yt-dlp") { if (-not (Get-Command $t -ErrorAction SilentlyContinue)) { Write-Warning "$t isn't on PATH (winget install Gyan.FFmpeg yt-dlp.yt-dlp)" } }
if (-not (Test-Path "$app\svcl.exe")) { Write-Host "Optional: put NirSoft's svcl.exe in $app and install VB-CABLE for app sound through the car." }
Write-Host "Installed. Open BeamDeck from the Start menu; BeamNG loads its side on the next start."
