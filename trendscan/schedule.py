"""Daily scheduled scans on macOS via launchd (the operating system's own scheduler).

`install` writes ~/Library/LaunchAgents/local.trendscan.scan.plist and loads it. launchd then
runs `python -m trendscan scan --scheduled` at the chosen time. If the Mac was asleep at that
time, launchd runs the scan when it wakes up (not if it was switched off).

The plist contains only paths. The API key stays in .env and is read when the scan starts.
On Windows or Linux, use Task Scheduler or cron with the same command (see README).
"""

from __future__ import annotations

import os
import plistlib
import subprocess
import sys
from pathlib import Path

from .config import DATA_DIR, PROJECT_ROOT

LABEL = "local.trendscan.scan"
LOG_DIR = DATA_DIR / "logs"
WEEKDAYS = range(1, 6)    # launchd: 0/7 = Sunday, 1 = Monday … 5 = Friday


def supported() -> bool:
    return sys.platform == "darwin"


def plist_path() -> Path:
    return Path.home() / "Library" / "LaunchAgents" / f"{LABEL}.plist"


def parse_time(text: str) -> tuple[int, int]:
    """'07:30' -> (7, 30)."""
    try:
        hour, minute = (int(x) for x in text.split(":"))
    except ValueError:
        raise ValueError(f"time must look like 07:30, got {text!r}") from None
    if not (0 <= hour < 24 and 0 <= minute < 60):
        raise ValueError(f"time out of range: {text!r}")
    return hour, minute


def build_plist(hour: int, minute: int, weekdays_only: bool = False,
                python: str = sys.executable, project_root: Path = PROJECT_ROOT) -> bytes:
    if weekdays_only:
        when = [{"Weekday": d, "Hour": hour, "Minute": minute} for d in WEEKDAYS]
    else:
        when = {"Hour": hour, "Minute": minute}
    return plistlib.dumps({
        "Label": LABEL,
        "ProgramArguments": [python, "-m", "trendscan", "scan", "--scheduled"],
        "WorkingDirectory": str(project_root),
        "StartCalendarInterval": when,
        "StandardOutPath": str(LOG_DIR / "launchd.log"),
        "StandardErrorPath": str(LOG_DIR / "launchd.log"),
        "ProcessType": "Background",     # low priority: don't slow down interactive work
        "RunAtLoad": False,              # installing does not start a scan
    })


def _launchctl(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run(["launchctl", *args], capture_output=True, text=True)


def _domain() -> str:
    return f"gui/{os.getuid()}"


def install(hour: int, minute: int, weekdays_only: bool = False) -> Path:
    if not supported():
        raise RuntimeError("Automatic installation is only implemented for macOS. See README.")
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    path = plist_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    _launchctl("bootout", _domain(), str(path))          # unload an older schedule, if any
    path.write_bytes(build_plist(hour, minute, weekdays_only))
    result = _launchctl("bootstrap", _domain(), str(path))
    if result.returncode != 0:
        raise RuntimeError(f"launchctl bootstrap failed: {result.stderr.strip() or result.returncode}")
    return path


def remove() -> bool:
    """Unload and delete the schedule. Returns False if none was installed."""
    path = plist_path()
    if not path.exists():
        return False
    _launchctl("bootout", _domain(), str(path))
    path.unlink()
    return True


def status() -> dict | None:
    """None if no schedule is installed, else {'time', 'weekdays_only', 'loaded', 'path'}."""
    if not supported():
        return None
    path = plist_path()
    if not path.exists():
        return None
    data = plistlib.loads(path.read_bytes())
    when = data.get("StartCalendarInterval", {})
    first = when[0] if isinstance(when, list) else when
    loaded = _launchctl("print", f"{_domain()}/{LABEL}").returncode == 0
    return {"time": f"{first.get('Hour', 0):02d}:{first.get('Minute', 0):02d}",
            "weekdays_only": isinstance(when, list), "loaded": loaded, "path": str(path)}
