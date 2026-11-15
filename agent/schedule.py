"""Run the pipeline at fixed times of day (default 09:00 and 18:00), on any OS.
Windows: Task Scheduler (one task per time, JobAgentCycle-HHMM). Linux: a tagged crontab line. macOS: a LaunchAgent with StartCalendarInterval.
The interpreter is the project venv's when present (it has Playwright), else the current Python."""
import os, re, subprocess, sys, plistlib, shutil
import common
from common import CORE, LOGS, SETTINGS

TASK = "JobAgentCycle"
TAG = "# JobAgent cycle"
PLIST = os.path.expanduser("~/Library/LaunchAgents/com.jobagent.cycle.plist")


def times():
    t = SETTINGS.get("schedule_times") or ["09:00", "18:00"]
    return [x for x in t if re.fullmatch(r"([01]\d|2[0-3]):[0-5]\d", x)]


def python(windowless=True):
    v = os.path.join(CORE, ".venv", "Scripts" if os.name == "nt" else "bin")
    for name in (("pythonw.exe", "python.exe") if (os.name == "nt" and windowless) else ("python.exe",) if os.name == "nt" else ("python3", "python")):
        p = os.path.join(v, name)
        if os.path.exists(p):
            return p
    exe = sys.executable
    if os.name == "nt" and windowless and exe.lower().endswith("python.exe") and os.path.exists(exe[:-10] + "pythonw.exe"):
        return exe[:-10] + "pythonw.exe"
    return exe


def _cmd():
    return [python(), os.path.join(CORE, "agent", "run.py"), "cycle"]


def _run(args):
    r = subprocess.run(args, capture_output=True, text=True)
    return (r.stdout or r.stderr or "").strip()


def _win_tasks():
    out = _run(["schtasks", "/Query", "/FO", "CSV", "/NH"])
    return sorted({m for m in re.findall(r'"\\(' + TASK + r'[^"]*)"', out)})


def install(ts=None):
    ts = ts or times()
    common.save_override("settings.json", {"schedule_times": ts})
    remove()
    if os.name == "nt":
        cmd = " ".join(f'"{x}"' for x in _cmd())
        return "\n".join(_run(["schtasks", "/Create", "/F", "/SC", "DAILY", "/ST", t, "/TN", f"{TASK}-{t.replace(':', '')}", "/TR", cmd]) for t in ts)
    if sys.platform == "darwin":
        os.makedirs(os.path.dirname(PLIST), exist_ok=True)
        plistlib.dump({"Label": "com.jobagent.cycle", "ProgramArguments": _cmd(), "StartCalendarInterval": [{"Hour": int(t[:2]), "Minute": int(t[3:])} for t in ts],
                       "StandardOutPath": os.path.join(LOGS, "schedule.out"), "StandardErrorPath": os.path.join(LOGS, "schedule.out"), "WorkingDirectory": CORE}, open(PLIST, "wb"))
        return _run(["launchctl", "load", "-w", PLIST]) or f"LaunchAgent installed for {', '.join(ts)}"
    if not shutil.which("crontab"):
        return "crontab not found: install cron, or run `python agent/run.py cycle` from your own scheduler"
    run = " ".join(f"'{x}'" for x in _cmd()) + f" >> '{os.path.join(LOGS, 'schedule.out')}' 2>&1 {TAG}"
    lines = [f"{int(t[3:])} {int(t[:2])} * * * {run}" for t in ts]
    cur = subprocess.run(["crontab", "-l"], capture_output=True, text=True).stdout
    keep = [l for l in cur.splitlines() if TAG not in l]
    subprocess.run(["crontab", "-"], input="\n".join(keep + lines) + "\n", text=True)
    return f"cron installed for {', '.join(ts)}"


def remove():
    if os.name == "nt":
        out = [_run(["schtasks", "/Delete", "/F", "/TN", t]) for t in _win_tasks()]
        return "\n".join(out) or "no scheduled task"
    if sys.platform == "darwin":
        if os.path.exists(PLIST):
            _run(["launchctl", "unload", "-w", PLIST])
            os.remove(PLIST)
        return "LaunchAgent removed"
    if shutil.which("crontab"):
        cur = subprocess.run(["crontab", "-l"], capture_output=True, text=True).stdout
        subprocess.run(["crontab", "-"], input="\n".join(l for l in cur.splitlines() if TAG not in l) + "\n", text=True)
    return "cron entry removed"


def enable(on=True):
    if os.name == "nt":
        return "\n".join(_run(["schtasks", "/Change", "/TN", t, "/ENABLE" if on else "/DISABLE"]) for t in _win_tasks())
    return install() if on else remove()


def info():
    ts = times()
    if os.name == "nt":
        tasks = _win_tasks()
        st, nxt, last = [], [], []
        for t in tasks:
            txt = _run(["schtasks", "/Query", "/TN", t, "/V", "/FO", "LIST"])
            g = lambda k: (re.search(rf"^{k}:\s*(.*)$", txt, re.M) or [None, ""])[1].strip()
            st.append(g("Status"))
            nxt.append(g("Next Run Time"))
            if "1999" not in g("Last Run Time"):  # Windows' placeholder for 'never ran'
                last.append(g("Last Run Time"))
        return {"supported": True, "installed": bool(tasks), "times": ts, "status": ", ".join(sorted(set(st))), "next": min(nxt) if nxt else "", "last": max(last) if last else ""}
    if sys.platform == "darwin":
        return {"supported": True, "installed": os.path.exists(PLIST), "times": ts, "status": "", "next": "", "last": ""}
    cur = subprocess.run(["crontab", "-l"], capture_output=True, text=True).stdout if shutil.which("crontab") else ""
    return {"supported": bool(shutil.which("crontab")), "installed": TAG in cur, "times": ts, "status": "", "next": "", "last": ""}


def migrate_legacy():
    """Old installs used one task repeating every N minutes; replace it with the daily times."""
    if os.name == "nt" and "JobAgentCycle" in _run(["schtasks", "/Query", "/TN", TASK]):
        _run(["schtasks", "/Delete", "/F", "/TN", TASK])
        return install()
    return ""
