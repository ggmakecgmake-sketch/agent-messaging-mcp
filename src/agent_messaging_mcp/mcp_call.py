from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from pathlib import Path


def _send(proc: subprocess.Popen, payload: dict) -> None:
    assert proc.stdin is not None
    proc.stdin.write(json.dumps(payload) + "\n")
    proc.stdin.flush()


def _read(proc: subprocess.Popen) -> dict:
    assert proc.stdout is not None
    while True:
        line = proc.stdout.readline()
        if not line:
            raise RuntimeError("MCP server exited before responding")
        line = line.strip()
        if not line:
            continue
        return json.loads(line)


def call_tool(tool: str, args: dict) -> dict:
    root = Path(__file__).resolve().parents[2]
    env = os.environ.copy()
    env["PYTHONPATH"] = str(root / "src") + os.pathsep + env.get("PYTHONPATH", "")
    proc = subprocess.Popen(
        [sys.executable, "-m", "agent_messaging_mcp.server"],
        cwd=str(root),
        env=env,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    try:
        _send(
            proc,
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "initialize",
                "params": {
                    "protocolVersion": "2024-11-05",
                    "capabilities": {},
                    "clientInfo": {"name": "agent-messaging-call", "version": "0.1.0"},
                },
            },
        )
        init = _read(proc)
        if init.get("error"):
            return init
        _send(proc, {"jsonrpc": "2.0", "method": "notifications/initialized", "params": {}})
        _send(proc, {"jsonrpc": "2.0", "id": 2, "method": "tools/call", "params": {"name": tool, "arguments": args}})
        return _read(proc)
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Call a tool from the local agent-messaging MCP server.")
    parser.add_argument("tool", help="Tool name, e.g. database_status or send_message")
    parser.add_argument("--args", default="{}", help="JSON object with tool arguments")
    ns = parser.parse_args(argv)
    result = call_tool(ns.tool, json.loads(ns.args))
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if not result.get("error") else 1


if __name__ == "__main__":
    sys.exit(main())
