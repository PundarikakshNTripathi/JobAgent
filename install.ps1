# JobAgent installer for Windows (PowerShell 5.1+).
#   irm https://raw.githubusercontent.com/PundarikakshNTripathi/JobAgent/main/install.ps1 | iex
# Re-running it updates the code and keeps your data. Unattended: $env:JOBAGENT_DEFAULTS = "1" first.
# Code goes to %USERPROFILE%\.jobagent\app (override $env:JOBAGENT_DIR); your personal workspace defaults to %USERPROFILE%\JobAgent.
$ErrorActionPreference = "Stop"
$Repo = if ($env:JOBAGENT_REPO) { $env:JOBAGENT_REPO } else { "https://github.com/PundarikakshNTripathi/JobAgent.git" }
$App = if ($env:JOBAGENT_DIR) { $env:JOBAGENT_DIR } else { Join-Path $env:USERPROFILE ".jobagent\app" }
$Defaults = $env:JOBAGENT_DEFAULTS -eq "1"
function Say($m) { Write-Host "==> $m" -ForegroundColor Cyan }
function Warn($m) { Write-Host "!!  $m" -ForegroundColor Yellow }
function Ask($q, $d) { if ($Defaults) { return $d }; $a = Read-Host "$q [$d]"; if ([string]::IsNullOrWhiteSpace($a)) { $d } else { $a } }
function Have($c) { [bool](Get-Command $c -ErrorAction SilentlyContinue) }

Say "JobAgent: Find the role. Reach the people. You press Submit."
Say "Installer for Windows"
# 1. Python 3.10+
$Py = $null
foreach ($c in @("py -3.13", "py -3.12", "py -3.11", "py -3.10", "python")) {
  $exe, $arg = $c.Split(" ", 2)
  if (Have $exe) {
    try { $v = & $exe $(if ($arg) { $arg }) -c "import sys; print(sys.version_info >= (3,10))" 2>$null; if ($v -eq "True") { $Py = $c; break } } catch {}
  }
}
if (-not $Py) {
  Warn "Python 3.10 or newer is needed."
  if ((Have winget) -and ((Ask "Install Python 3.12 with winget? (y/n)" "y") -eq "y")) {
    winget install -e --id Python.Python.3.12 --accept-package-agreements --accept-source-agreements
    $env:Path = [Environment]::GetEnvironmentVariable("Path", "Machine") + ";" + [Environment]::GetEnvironmentVariable("Path", "User")
    $Py = "py -3.12"
  } else { Warn "Install Python from https://www.python.org/downloads/ (tick 'Add to PATH') and run this again."; exit 1 }
}
$PyExe, $PyArg = $Py.Split(" ", 2)
Say "Python: $Py"
# 2. Code
if (Have git) {
  if (Test-Path (Join-Path $App ".git")) { Say "Updating $App"; git -C $App pull --ff-only }
  else { Say "Downloading to $App"; New-Item -ItemType Directory -Force (Split-Path $App) | Out-Null; git clone --depth 1 $Repo $App }
} else {
  if ((Have winget) -and ((Ask "git not found. Install Git with winget? (y/n)" "y") -eq "y")) {
    winget install -e --id Git.Git --accept-package-agreements --accept-source-agreements
    $env:Path = [Environment]::GetEnvironmentVariable("Path", "Machine") + ";" + [Environment]::GetEnvironmentVariable("Path", "User")
    git clone --depth 1 $Repo $App
  } else {
    Say "Downloading a zip"
    $zip = Join-Path $env:TEMP "jobagent.zip"
    Invoke-WebRequest -Uri (($Repo -replace "\.git$", "") + "/archive/refs/heads/main.zip") -OutFile $zip -UseBasicParsing
    Expand-Archive $zip -DestinationPath $env:TEMP -Force
    New-Item -ItemType Directory -Force $App | Out-Null
    Copy-Item -Recurse -Force (Join-Path $env:TEMP "JobAgent-main\*") $App
  }
}
Set-Location $App
# 3. Virtual environment + packages
if (-not (Test-Path ".venv")) { & $PyExe $(if ($PyArg) { $PyArg }) -m venv .venv }
$VPy = Join-Path $App ".venv\Scripts\python.exe"
& $VPy -m pip install -q --upgrade pip
& $VPy -m pip install -q -r requirements.txt
# 4. Browser
$chrome = @("$env:ProgramFiles\Google\Chrome\Application\chrome.exe", "${env:ProgramFiles(x86)}\Google\Chrome\Application\chrome.exe", "$env:LOCALAPPDATA\Google\Chrome\Application\chrome.exe",
            "${env:ProgramFiles(x86)}\Microsoft\Edge\Application\msedge.exe") | Where-Object { Test-Path $_ }
if (-not $chrome) { Say "No Chrome/Edge found: installing Playwright's Chromium (used only by JobAgent)"; & $VPy -m playwright install chromium }
if (-not (Have node)) { Warn "Node.js not found (optional: only for the fallback browser mode). https://nodejs.org" }
# 5. Workspace location
if (-not $env:JOBAGENT_HOME -and -not (Test-Path ".jobagent_home")) {
  $ws = Ask "Where should your personal workspace live" (Join-Path $env:USERPROFILE "JobAgent")
  Set-Content -Path ".jobagent_home" -Value $ws -Encoding utf8
}
# 6. Launcher: `jobagent <command>`
$Bin = Join-Path $env:USERPROFILE ".jobagent\bin"
New-Item -ItemType Directory -Force $Bin | Out-Null
Set-Content -Path (Join-Path $Bin "jobagent.cmd") -Encoding ascii -Value "@echo off`r`nif `"%~1`"==`"`" (`"$VPy`" `"$App\agent\run.py`" app) else (`"$VPy`" `"$App\agent\run.py`" %*)"
$userPath = [Environment]::GetEnvironmentVariable("Path", "User")
if ($userPath -notlike "*$Bin*") { [Environment]::SetEnvironmentVariable("Path", "$userPath;$Bin", "User"); $env:Path += ";$Bin" }
# 7. Guided setup
Say "Setup"
if ($Defaults) { & $VPy agent\run.py init --defaults } else { & $VPy agent\run.py init }
& $VPy agent\run.py doctor
# 8. Desktop app: Start menu + desktop shortcut that opens JobAgent in its own window
& $VPy agent\run.py shortcuts
Say "Done. Open JobAgent from the Start menu or the desktop, or run:  jobagent   (new terminals pick up the 'jobagent' command)"
