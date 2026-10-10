"""Spike M0.2: a minimal legacy-era (initialize handshake) MCP stdio server, own code, standard library only.

Tools:
  echo(text)                 returns the text
  sleep(seconds)             waits, sending a progress notification every 10 s (tests auto-backgrounding)
  spawn_heartbeat(out_file)  starts spikes/heartbeat.py detached from this server (spike M0.1)

Every message in and out is appended to spikes/mcp_echo.log so the handshake can be inspected afterwards.
"""

import datetime as dt
import json
import os
import sys
import threading
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import heartbeat  # noqa: E402

LOG = os.path.join(os.path.dirname(os.path.abspath(__file__)), "mcp_echo.log")
LEGACY_VERSIONS = ("2025-11-25", "2025-06-18", "2025-03-26", "2024-11-05")
_out_lock = threading.Lock()

TOOLS = [
    {"name": "echo", "description": "Return the given text.",
     "inputSchema": {"type": "object", "properties": {"text": {"type": "string"}}, "required": ["text"],
                     "additionalProperties": False}},
    {"name": "sleep", "description": "Wait the given number of seconds, reporting progress every 10 s.",
     "inputSchema": {"type": "object", "properties": {"seconds": {"type": "integer", "minimum": 1}},
                     "required": ["seconds"], "additionalProperties": False}},
    {"name": "spawn_heartbeat", "description": "Start a detached heartbeat process writing to out_file.",
     "inputSchema": {"type": "object", "properties": {"out_file": {"type": "string"},
                                                      "minutes": {"type": "integer", "minimum": 1}},
                     "required": ["out_file"], "additionalProperties": False}},
]


def log(direction, msg):
    with open(LOG, "a", encoding="utf-8") as f:
        f.write(f"{dt.datetime.now().isoformat(timespec='milliseconds')} {direction} {json.dumps(msg)}\n")


def send(msg):
    log("->", msg)
    with _out_lock:
        sys.stdout.write(json.dumps(msg) + "\n")
        sys.stdout.flush()


def text_result(text):
    return {"content": [{"type": "text", "text": text}]}


def call_tool(req):
    params = req.get("params") or {}
    name, args = params.get("name"), params.get("arguments") or {}
    token = (params.get("_meta") or {}).get("progressToken")
    try:
        if name == "echo":
            result = text_result(args["text"])
        elif name == "sleep":
            total = int(args["seconds"])
            for done in range(0, total, 10):
                if token is not None:
                    send({"jsonrpc": "2.0", "method": "notifications/progress",
                          "params": {"progressToken": token, "progress": done, "total": total,
                                     "message": f"slept {done}s of {total}s"}})
                time.sleep(min(10, total - done))
            result = text_result(f"slept {total}s (progress token: {token!r})")
        elif name == "spawn_heartbeat":
            info = heartbeat.spawn(os.path.abspath(args["out_file"]), int(args.get("minutes", 30)))
            result = text_result(json.dumps(info))
        else:
            send({"jsonrpc": "2.0", "id": req["id"], "error": {"code": -32602, "message": f"unknown tool {name}"}})
            return
    except Exception as e:  # spike: report anything as a tool error
        result = {"content": [{"type": "text", "text": f"{type(e).__name__}: {e}"}], "isError": True}
    send({"jsonrpc": "2.0", "id": req["id"], "result": result})


def handle(req):
    method, rid = req.get("method"), req.get("id")
    if rid is None:  # notification
        return
    if method == "initialize":
        asked = (req.get("params") or {}).get("protocolVersion")
        version = asked if asked in LEGACY_VERSIONS else LEGACY_VERSIONS[0]
        send({"jsonrpc": "2.0", "id": rid, "result": {
            "protocolVersion": version,
            "capabilities": {"tools": {}},
            "serverInfo": {"name": "harness-spike", "version": "0.0.1"},
            "instructions": "Spike server for the agent harness. Tools: echo, sleep, spawn_heartbeat.",
        }})
    elif method == "ping":
        send({"jsonrpc": "2.0", "id": rid, "result": {}})
    elif method == "tools/list":
        send({"jsonrpc": "2.0", "id": rid, "result": {"tools": TOOLS}})
    elif method == "tools/call":
        threading.Thread(target=call_tool, args=(req,), daemon=True).start()
    else:  # includes server/discover, so modern clients fall back to initialize
        send({"jsonrpc": "2.0", "id": rid, "error": {"code": -32601, "message": f"method not found: {method}"}})


def main():
    log("--", {"start": os.getpid(), "in_job": heartbeat.in_job()})
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            req = json.loads(line)
        except json.JSONDecodeError:
            send({"jsonrpc": "2.0", "id": None, "error": {"code": -32700, "message": "parse error"}})
            continue
        log("<-", req)
        handle(req)
    log("--", {"stdin closed": os.getpid()})


if __name__ == "__main__":
    main()
