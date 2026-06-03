#!/usr/bin/env python3
"""
Cube Memory MCP Server — stdio transport.
Plug into Claude Code, Cursor, Zed, Windsurf, or any MCP client.

Setup (Claude Code .mcp.json):
  {
    "mcpServers": {
      "cube-memory": {
        "command": "python3",
        "args": ["/path/to/cube_memory_mcp.py"],
        "env": {
          "CM_API_KEY":    "cm_live_...",
          "CM_PROJECT_ID": "proj_..."
        }
      }
    }
  }

Or use the hosted endpoint (no local file needed):
  {
    "mcpServers": {
      "cube-memory": {
        "type": "http",
        "url": "https://cubememory.com.br/gateway/v1/mcp",
        "headers": {
          "Authorization": "Bearer cm_live_...",
          "X-Project-Id": "proj_..."
        }
      }
    }
  }
"""
import os, sys, json, time, hashlib
try:
    import httpx
except ImportError:
    import subprocess; subprocess.check_call([sys.executable, "-m", "pip", "install", "-q", "httpx"])
    import httpx

BASE    = os.getenv("CM_BASE_URL", "https://cubememory.com.br/gateway")
API_KEY = os.getenv("CM_API_KEY") or os.getenv("OMNIBUS_API_KEY", "")
PID     = os.getenv("CM_PROJECT_ID") or os.getenv("CUBE_PROJECT_ID", "")
HEADERS = {"Authorization": f"Bearer {API_KEY}", "X-Project-Id": PID, "Content-Type": "application/json"}

TOOLS = [
    {"name": "search_memory",  "description": "Recall relevant memories before responding. Always call this first with the user's message.",
     "inputSchema": {"type":"object","properties":{"query":{"type":"string"},"limit":{"type":"integer","default":5}},"required":["query"]}},
    {"name": "store_memory",   "description": "Save a memory permanently after each exchange.",
     "inputSchema": {"type":"object","properties":{"text":{"type":"string"},"layer":{"type":"string","enum":["long-term","short-term"],"default":"long-term"}},"required":["text"]}},
    {"name": "list_memories",  "description": "List all stored memories.",
     "inputSchema": {"type":"object","properties":{"limit":{"type":"integer","default":20}}}},
    {"name": "forget_memory",  "description": "Delete a specific memory by ID.",
     "inputSchema": {"type":"object","properties":{"id":{"type":"string"}},"required":["id"]}},
]

def call_tool(name: str, args: dict) -> str:
    if name == "search_memory":
        r = httpx.post(f"{BASE}/v1/memory/search", json={"query": args["query"], "limit": args.get("limit",5)}, headers=HEADERS, timeout=15)
        if r.status_code != 200: return f"Error: {r.status_code}"
        results = r.json().get("results", [])
        if not results: return "No relevant memories found."
        return "\n".join(f"[{i+1}] (score {x['score']:.3f}) {x['text_preview']}" for i,x in enumerate(results))
    elif name == "store_memory":
        text = args["text"].strip()
        mid = f"mem_{hashlib.sha1(f'{text}{time.time()}'.encode()).hexdigest()[:10]}"
        r = httpx.post(f"{BASE}/v1/memory", json={"id": mid, "text": text, "layer": args.get("layer","long-term")}, headers=HEADERS, timeout=15)
        return f"Memory stored (id={mid})." if r.status_code == 200 else f"Error: {r.status_code}"
    elif name == "list_memories":
        r = httpx.get(f"{BASE}/v1/memory", headers=HEADERS, timeout=15)
        if r.status_code != 200: return f"Error: {r.status_code}"
        mems = r.json().get("memories", [])[:args.get("limit",20)]
        return "\n".join(f"[{m['id']}] ({m['layer']}) {m['text'][:120]}" for m in mems) or "No memories yet."
    elif name == "forget_memory":
        r = httpx.delete(f"{BASE}/v1/memory/{args['id']}", headers=HEADERS, timeout=15)
        return "Deleted." if r.status_code == 200 else f"Error: {r.status_code}"
    return f"Unknown tool: {name}"

def send(obj): sys.stdout.write(json.dumps(obj) + "\n"); sys.stdout.flush()

def main():
    if not API_KEY or not PID:
        sys.stderr.write("Error: set CM_API_KEY and CM_PROJECT_ID env vars\n"); sys.exit(1)
    for line in sys.stdin:
        line = line.strip()
        if not line: continue
        try:
            msg = json.loads(line)
        except json.JSONDecodeError:
            continue
        method = msg.get("method",""); rid = msg.get("id",0)
        if method == "initialize":
            send({"jsonrpc":"2.0","id":rid,"result":{"protocolVersion":"2024-11-05","capabilities":{"tools":{}},"serverInfo":{"name":"cube-memory","version":"1.0"}}})
        elif method == "notifications/initialized":
            pass
        elif method == "tools/list":
            send({"jsonrpc":"2.0","id":rid,"result":{"tools":TOOLS}})
        elif method == "tools/call":
            params = msg.get("params",{}); name = params.get("name",""); args = params.get("arguments",{})
            try:
                result = call_tool(name, args)
                send({"jsonrpc":"2.0","id":rid,"result":{"content":[{"type":"text","text":result}]}})
            except Exception as e:
                send({"jsonrpc":"2.0","id":rid,"error":{"code":-32000,"message":str(e)}})
        else:
            send({"jsonrpc":"2.0","id":rid,"error":{"code":-32601,"message":f"Method not found: {method}"}})

if __name__ == "__main__":
    main()
