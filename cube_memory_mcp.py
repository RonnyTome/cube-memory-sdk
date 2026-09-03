#!/usr/bin/env python3
"""
Cube Memory MCP Server — stdio transport. MIT licensed.
Plug into dsh (DeepSeek Harness), Claude Code, Cursor, Zed, Windsurf, or any MCP client.

Zero dependencies: Python 3.8+ standard library only (urllib). Nothing is
installed at import time — if something is missing you get an error, never a
silent `pip install` into your environment.

Setup (Claude Code .mcp.json / dsh via dsh-mcp-client):
  {
    "mcpServers": {
      "cube-memory": {
        "command": "python3",
        "args": ["/path/to/cube_memory_mcp.py"],
        "env": {"CM_API_KEY": "cm_live_...", "CM_PROJECT_ID": "proj_..."}
      }
    }
  }

Or the hosted endpoint, no local file:
  {
    "mcpServers": {
      "cube-memory": {
        "type": "http",
        "url": "https://cubememory.com.br/gateway/v1/mcp",
        "headers": {"Authorization": "Bearer cm_live_...", "X-Project-Id": "proj_..."}
      }
    }
  }

Tools: search_memory, fetch_memory, store_memory, list_memory, forget_memory.
`list_memories` is accepted as an alias of `list_memory` for backward
compatibility with builds of this file published before 2026-09-03.
"""
import hashlib
import json
import os
import sys
import time
import urllib.error
import urllib.request

BASE    = os.getenv("CM_BASE_URL", "https://cubememory.com.br/gateway")
API_KEY = os.getenv("CM_API_KEY") or os.getenv("OMNIBUS_API_KEY", "")
PID     = os.getenv("CM_PROJECT_ID") or os.getenv("CUBE_PROJECT_ID", "")
TIMEOUT = float(os.getenv("CM_TIMEOUT", "30"))

# Fallback: config.json gravado pelo instalador (install.sh / install.ps1).
# Caminhos: Linux/macOS ~/.cube-memory/config.json ; Windows %USERPROFILE%\.cube-memory\config.json
if (not API_KEY or not PID):
    _CFG = {}
    try:
        _p = os.path.expanduser("~/.cube-memory/config.json")
        if os.path.exists(_p):
            with open(_p, encoding="utf-8") as _f:
                _CFG = json.load(_f)
    except Exception:
        _CFG = {}
    if not API_KEY:
        API_KEY = _CFG.get("api_key", "")
    if not PID:
        PID = _CFG.get("project_id", "")
    if not BASE.startswith("http") and _CFG.get("base_url"):
        BASE = _CFG["base_url"]

TOOLS = [
    {"name": "search_memory",
     "description": "Recall relevant memories before responding. Call this first with the user's message.",
     "inputSchema": {"type": "object",
                     "properties": {"query": {"type": "string"},
                                    "limit": {"type": "integer", "default": 5}},
                     "required": ["query"]}},
    {"name": "fetch_memory",
     "description": "Fetch the full text of specific memories by id (ids come from search_memory).",
     "inputSchema": {"type": "object",
                     "properties": {"ids": {"type": "array", "items": {"type": "string"}}},
                     "required": ["ids"]}},
    {"name": "store_memory",
     "description": "Save a memory. Point at the source of truth instead of copying it; if it overlaps an existing memory, update that one instead of appending a second copy.",
     "inputSchema": {"type": "object",
                     "properties": {"text": {"type": "string"},
                                    "layer": {"type": "string",
                                              "enum": ["short-term", "mid-term", "long-term"],
                                              "default": "long-term"}},
                     "required": ["text"]}},
    {"name": "list_memory",
     "description": "List stored memories, most recent first.",
     "inputSchema": {"type": "object",
                     "properties": {"limit": {"type": "integer", "default": 20}}}},
    {"name": "forget_memory",
     "description": "Delete a specific memory by id.",
     "inputSchema": {"type": "object",
                     "properties": {"id": {"type": "string"}},
                     "required": ["id"]}},
]


def _request(method: str, path: str, payload=None):
    """Minimal JSON HTTP call on the stdlib. Returns (status, decoded_body|None)."""
    data = json.dumps(payload).encode("utf-8") if payload is not None else None
    req = urllib.request.Request(f"{BASE}{path}", data=data, method=method)
    req.add_header("Authorization", f"Bearer {API_KEY}")
    req.add_header("X-Project-Id", PID)
    if data is not None:
        req.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
            body = r.read().decode("utf-8")
            return r.status, (json.loads(body) if body.strip() else None)
    except urllib.error.HTTPError as e:
        return e.code, None
    except urllib.error.URLError as e:
        return 0, {"_error": str(e.reason)}


def _text_of(item: dict) -> str:
    return item.get("text") or item.get("text_preview") or ""


def call_tool(name: str, args: dict) -> str:
    if name == "search_memory":
        st, body = _request("POST", "/v1/memory/search",
                            {"query": args["query"], "limit": args.get("limit", 5)})
        if st != 200:
            return f"Error: HTTP {st}"
        results = (body or {}).get("results", [])
        if not results:
            return "No relevant memories found."
        return "\n".join(
            f"[{i}] id={x.get('id')} (score {float(x.get('score', 0)):.3f}) {_text_of(x)}"
            for i, x in enumerate(results, 1))

    if name == "fetch_memory":
        ids = args.get("ids") or []
        if not ids:
            return "Error: ids is required."
        st, body = _request("POST", "/v1/memory/fetch", {"ids": ids})
        if st != 200:
            return f"Error: HTTP {st}"
        mems = (body or {}).get("memories", [])
        missing = (body or {}).get("missing", [])
        out = [f"[{m.get('id')}] {_text_of(m)}" for m in mems]
        if missing:
            out.append(f"(not found: {', '.join(missing)})")
        return "\n".join(out) or "No memories found."

    if name == "store_memory":
        text = (args.get("text") or "").strip()
        if not text:
            return "Error: text is required."
        mid = f"mem_{hashlib.sha1(f'{text}{time.time()}'.encode()).hexdigest()[:10]}"
        st, body = _request("POST", "/v1/memory",
                            {"id": mid, "text": text, "layer": args.get("layer", "long-term")})
        if st != 200:
            return f"Error: HTTP {st}"
        return f"Memory stored (id={(body or {}).get('id', mid)}). Indexing is async — allow a few seconds before recall."

    if name in ("list_memory", "list_memories"):   # alias kept for older configs
        limit = args.get("limit", 20)
        st, body = _request("GET", f"/v1/memory?limit={int(limit)}")
        if st != 200:
            return f"Error: HTTP {st}"
        mems = (body or {}).get("memories", [])[:limit]
        return "\n".join(
            f"[{m.get('id')}] ({m.get('layer', '?')}) {_text_of(m)[:120]}" for m in mems
        ) or "No memories yet."

    if name == "forget_memory":
        mid = args.get("id")
        if not mid:
            return "Error: id is required."
        st, _ = _request("DELETE", f"/v1/memory/{mid}")
        return "Deleted." if st == 200 else f"Error: HTTP {st}"

    return f"Unknown tool: {name}"


def send(obj):
    sys.stdout.write(json.dumps(obj) + "\n")
    sys.stdout.flush()


def main():
    if not API_KEY or not PID:
        sys.stderr.write("Error: set CM_API_KEY and CM_PROJECT_ID env vars\n")
        sys.exit(1)
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            msg = json.loads(line)
        except json.JSONDecodeError:
            continue
        method, rid = msg.get("method", ""), msg.get("id", 0)
        if method == "initialize":
            send({"jsonrpc": "2.0", "id": rid, "result": {
                "protocolVersion": "2024-11-05",
                "capabilities": {"tools": {}},
                "serverInfo": {"name": "cube-memory", "version": "1.1"}}})
        elif method == "notifications/initialized":
            pass
        elif method == "tools/list":
            send({"jsonrpc": "2.0", "id": rid, "result": {"tools": TOOLS}})
        elif method == "tools/call":
            params = msg.get("params", {})
            try:
                result = call_tool(params.get("name", ""), params.get("arguments", {}))
                send({"jsonrpc": "2.0", "id": rid,
                      "result": {"content": [{"type": "text", "text": result}]}})
            except Exception as e:
                send({"jsonrpc": "2.0", "id": rid, "error": {"code": -32000, "message": str(e)}})
        else:
            send({"jsonrpc": "2.0", "id": rid,
                  "error": {"code": -32601, "message": f"Method not found: {method}"}})


if __name__ == "__main__":
    main()

# sync-pipeline smoke test 2026-09-03T10:45:03Z
