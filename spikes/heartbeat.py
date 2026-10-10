"""Spike M0.1: a detached process that writes a heartbeat until it is killed or its time runs out.

  python spikes/heartbeat.py spawn <out_file> [--minutes N]   start a detached heartbeat and print its PID
  python spikes/heartbeat.py beat <out_file> <minutes>          (what the detached process runs)

The heartbeat file gets one line every 5 seconds: time, PID, and how the process was launched. If it keeps growing
after the Claude Code session that started it has ended, detached workers survive the session.
"""

import ctypes
import datetime as dt
import json
import os
import subprocess
import sys
import time

DETACHED_PROCESS = 0x00000008
CREATE_NEW_PROCESS_GROUP = 0x00000200
CREATE_BREAKAWAY_FROM_JOB = 0x01000000
CREATE_NO_WINDOW = 0x08000000


def in_job():
    """True if this process belongs to a Windows job object (None if it can't be told)."""
    if os.name != "nt":
        return None
    from ctypes import wintypes
    k32 = ctypes.WinDLL("kernel32", use_last_error=True)
    k32.GetCurrentProcess.restype = wintypes.HANDLE
    k32.IsProcessInJob.argtypes = [wintypes.HANDLE, wintypes.HANDLE, ctypes.POINTER(wintypes.BOOL)]
    result = wintypes.BOOL(0)
    ok = k32.IsProcessInJob(k32.GetCurrentProcess(), None, ctypes.byref(result))
    return bool(result.value) if ok else None


def spawn(out_file, minutes):
    """Start the heartbeat detached. Tries to break away from any job object first."""
    args = [sys.executable, os.path.abspath(__file__), "beat", out_file, str(minutes)]
    base = DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP | CREATE_NO_WINDOW
    attempts = [("breakaway", base | CREATE_BREAKAWAY_FROM_JOB), ("no-breakaway", base)]
    for name, flags in attempts:
        try:
            proc = subprocess.Popen(args, creationflags=flags, close_fds=True, stdin=subprocess.DEVNULL,
                                    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            return {"pid": proc.pid, "flags": name, "launcher_in_job": in_job(), "launcher_pid": os.getpid()}
        except OSError as e:
            last = f"{name}: {e}"
    return {"error": last, "launcher_in_job": in_job()}


def beat(out_file, minutes):
    end = time.monotonic() + minutes * 60
    while time.monotonic() < end:
        line = {"t": dt.datetime.now().isoformat(timespec="seconds"), "pid": os.getpid(), "in_job": in_job(),
                "ppid": os.getppid()}
        with open(out_file, "a", encoding="utf-8") as f:
            f.write(json.dumps(line) + "\n")
        time.sleep(5)


if __name__ == "__main__":
    if sys.argv[1] == "spawn":
        minutes = int(sys.argv[sys.argv.index("--minutes") + 1]) if "--minutes" in sys.argv else 30
        print(json.dumps(spawn(os.path.abspath(sys.argv[2]), minutes)))
    elif sys.argv[1] == "beat":
        beat(sys.argv[2], float(sys.argv[3]))
