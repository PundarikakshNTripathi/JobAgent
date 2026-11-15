"""JobAgent as a desktop app: its own window, taskbar/dock icon and Start-menu/desktop/launcher entry, without a browser tab.

    python agent/run.py app            open the app window (starts the local server in the background if it isn't running)
    python agent/run.py shortcuts      add JobAgent to the Start menu + desktop (Windows), app launcher (Linux) or ~/Applications (macOS)

How: the GUI is still the local web server (nothing leaves your machine). The window is Chrome/Edge/Chromium in "app mode"
(--app=URL: no tabs, no address bar) with its own small profile, so it gets its own window, title and icon, and it never mixes with
your normal browsing or with the agent's automation browser. No extra runtime (Electron etc.) is downloaded."""
import json, os, shutil, subprocess, sys, time, urllib.request
import common
from common import DATA

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
STATIC = os.path.join(HERE, "gui_static")


def url():
    return f"http://127.0.0.1:{common.SETTINGS.get('gui_port', 8777)}"


def server_up(timeout=1.5):
    try:
        with urllib.request.urlopen(url() + "/api/ping", timeout=timeout) as r:
            return r.status == 200
    except Exception:
        return False


def _py(windowless=True):
    """The interpreter to run JobAgent with: the venv's, and on Windows its console-less pythonw for the background server."""
    exe = sys.executable
    if os.name == "nt" and windowless:
        w = os.path.join(os.path.dirname(exe), "pythonw.exe")
        if os.path.exists(w):
            return w
    return exe


def start_server(wait=20):
    if server_up():
        return True
    cmd = [_py(), os.path.join(HERE, "run.py"), "gui", "--no-open"]
    log = open(os.path.join(common.LOGS, "gui.log"), "a", encoding="utf-8")
    kw = {"cwd": ROOT, "stdout": log, "stderr": log, "stdin": subprocess.DEVNULL}
    if os.name == "nt":
        kw["creationflags"] = 0x00000008 | 0x00000200 | 0x08000000  # DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP | CREATE_NO_WINDOW
    else:
        kw["start_new_session"] = True
    subprocess.Popen(cmd, **kw)
    t0 = time.time()
    while time.time() - t0 < wait:
        if server_up(0.5):
            return True
        time.sleep(0.3)
    return False


def browser():
    """A Chromium-family browser that supports --app. Edge first on Windows (always installed), then Chrome, then Chromium."""
    import fill
    c = list(fill.CHROME_CANDIDATES)
    if os.name == "nt":
        edge = [os.path.expandvars(r"%ProgramFiles(x86)%\Microsoft\Edge\Application\msedge.exe"), os.path.expandvars(r"%ProgramFiles%\Microsoft\Edge\Application\msedge.exe")]
        c = [p for p in c if "ms-playwright" not in p] + edge + [p for p in c if "ms-playwright" in p]
    return next((p for p in c if p and os.path.exists(p)), None)


def open_window():
    if not start_server():
        print("The JobAgent server did not start; see logs/gui.log")
        return False
    exe = browser()
    if not exe:  # no Chromium-family browser: a normal tab is the best available
        import webbrowser
        webbrowser.open(url())
        print("Opened in your default browser (install Chrome, Edge or Chromium for a separate app window).")
        return True
    prof = os.path.join(DATA, "app-window")
    os.makedirs(prof, exist_ok=True)
    args = [exe, f"--app={url()}", f"--user-data-dir={prof}", "--window-size=1280,820", "--no-first-run", "--no-default-browser-check",
            "--disable-features=Translate", "--class=JobAgent"]
    kw = {"stdout": subprocess.DEVNULL, "stderr": subprocess.DEVNULL, "stdin": subprocess.DEVNULL}
    if os.name == "nt":
        kw["creationflags"] = 0x00000008 | 0x00000200
    else:
        kw["start_new_session"] = True
    subprocess.Popen(args, **kw)
    print("JobAgent is open in its own window.", url())
    return True


# ---------- shortcuts

def _windows_shortcuts():
    ico = os.path.join(STATIC, "icon.ico")
    target, run_py = _py(), os.path.join(HERE, "run.py")
    places = [os.path.join(os.environ.get("APPDATA", ""), r"Microsoft\Windows\Start Menu\Programs"), os.path.join(os.path.expanduser("~"), "Desktop")]
    try:  # OneDrive-redirected desktops
        out = subprocess.run(["powershell", "-NoProfile", "-Command", "[Environment]::GetFolderPath('Desktop')"], capture_output=True, text=True, timeout=20).stdout.strip()
        if out:
            places[1] = out
    except Exception:
        pass
    made = []
    for d in places:
        if not os.path.isdir(d):
            continue
        lnk = os.path.join(d, "JobAgent.lnk")
        ps = ("$s=(New-Object -ComObject WScript.Shell).CreateShortcut($env:JA_LNK);$s.TargetPath=$env:JA_T;$s.Arguments=$env:JA_A;"
              "$s.WorkingDirectory=$env:JA_W;$s.IconLocation=$env:JA_I;$s.Description='JobAgent: find the role, reach the people';$s.Save()")
        env = dict(os.environ, JA_LNK=lnk, JA_T=target, JA_A=f'"{run_py}" app', JA_W=ROOT, JA_I=ico + ",0")
        r = subprocess.run(["powershell", "-NoProfile", "-Command", ps], env=env, capture_output=True, text=True, timeout=30)
        if r.returncode == 0 and os.path.exists(lnk):
            made.append(lnk)
    return made


def _linux_shortcut():
    d = os.path.expanduser("~/.local/share/applications")
    os.makedirs(d, exist_ok=True)
    p = os.path.join(d, "jobagent.desktop")
    with open(p, "w", encoding="utf-8") as f:
        f.write("[Desktop Entry]\nType=Application\nName=JobAgent\nComment=Find the role, reach the people\n"
                f"Exec=\"{_py(False)}\" \"{os.path.join(HERE, 'run.py')}\" app\nIcon={os.path.join(STATIC, 'icon-512.png')}\n"
                "Terminal=false\nCategories=Office;Utility;\nStartupWMClass=JobAgent\n")
    os.chmod(p, 0o755)
    return [p]


def _mac_shortcut():
    app = os.path.expanduser("~/Applications/JobAgent.app")
    macos, res = os.path.join(app, "Contents", "MacOS"), os.path.join(app, "Contents", "Resources")
    os.makedirs(macos, exist_ok=True)
    os.makedirs(res, exist_ok=True)
    exe = os.path.join(macos, "JobAgent")
    with open(exe, "w", encoding="utf-8") as f:
        f.write(f"#!/bin/sh\nexec \"{_py(False)}\" \"{os.path.join(HERE, 'run.py')}\" app\n")
    os.chmod(exe, 0o755)
    icon = ""
    if shutil.which("sips") and shutil.which("iconutil"):  # build a real .icns from the 512 px PNG
        iset = os.path.join(res, "JobAgent.iconset")
        os.makedirs(iset, exist_ok=True)
        for s in (16, 32, 128, 256, 512):
            subprocess.run(["sips", "-z", str(s), str(s), os.path.join(STATIC, "icon-512.png"), "--out", os.path.join(iset, f"icon_{s}x{s}.png")], capture_output=True)
        if subprocess.run(["iconutil", "-c", "icns", iset, "-o", os.path.join(res, "JobAgent.icns")], capture_output=True).returncode == 0:
            icon = "<key>CFBundleIconFile</key><string>JobAgent</string>"
        shutil.rmtree(iset, ignore_errors=True)
    with open(os.path.join(app, "Contents", "Info.plist"), "w", encoding="utf-8") as f:
        f.write('<?xml version="1.0" encoding="UTF-8"?>\n<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">\n'
                f'<plist version="1.0"><dict><key>CFBundleName</key><string>JobAgent</string><key>CFBundleExecutable</key><string>JobAgent</string>'
                f'<key>CFBundleIdentifier</key><string>local.jobagent</string><key>CFBundlePackageType</key><string>APPL</string>{icon}</dict></plist>\n')
    return [app]


def install_shortcuts():
    if os.name == "nt":
        made = _windows_shortcuts()
    elif sys.platform == "darwin":
        made = _mac_shortcut()
    else:
        made = _linux_shortcut()
    for m in made:
        print("added", m)
    if not made:
        print("No shortcut could be created; run `python agent/run.py app` to open the window.")
    return made


def manifest():
    return json.dumps({"name": "JobAgent", "short_name": "JobAgent", "description": "Find the role. Reach the people. You press Submit.",
                       "start_url": "/", "display": "standalone", "background_color": "#f3f1ec", "theme_color": "#f3f1ec",
                       "icons": [{"src": "/icon-192.png", "sizes": "192x192", "type": "image/png"}, {"src": "/icon-512.png", "sizes": "512x512", "type": "image/png"}]})
