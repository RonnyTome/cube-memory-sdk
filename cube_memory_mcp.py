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

Tools: search_memory, fetch_memory, store_memory, list_memory, forget_memory,
       search_code, map_code, get_symbol, get_symbol_refs.
`list_memories` is accepted as an alias of `list_memory` for backward
compatibility with builds of this file published before 2026-09-03.

Code layer (v1.3): the project's code can be ingested server-side (per-symbol
parse) and queried with search_code / map_code / get_symbol. get_symbol_refs
adds RELATIONSHIPS between symbols (who uses/calls a symbol, cross-file, with
line + snippet) — IDE "find references" on top of the index. Freshness without
GitHub sync:
  - `python3 cube_memory_mcp.py sync` — delta-ingest: hashes every code file
    under CM_CODE_ROOT, asks the server what changed, re-ingests only that.
    Wire it to a SessionStart hook to keep the index always fresh.
    Auto-root: if CM_CODE_ROOT is not set, the git repo root of the current
    directory is used automatically (no manual setup needed).
  - map_code / get_symbol / get_symbol_refs self-heal: if CM_CODE_ROOT is set
    and the local file differs from what was indexed, that one file is
    re-ingested before answering.
  - every code answer carries the indexing age; stale ones are flagged.
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

# Raiz do código do projeto na máquina do cliente — habilita sync_code e a
# auto-cura (lazy re-ingest). Sem isto, o SDK ainda funciona: só não detecta
# mudança local (respostas vêm carimbadas com a idade da indexação).
CODE_ROOT   = os.getenv("CM_CODE_ROOT", "")
CODE_STALE_H = float(os.getenv("CM_CODE_STALE_H", "24"))
_CODE_EXT   = (".py", ".ts", ".tsx", ".js", ".jsx", ".mjs", ".cjs")
_CODE_SKIP  = {"node_modules", ".git", ".next", "__pycache__", "dist", "build",
               ".venv", "venv", ".mypy_cache", ".pytest_cache", "coverage"}
_CODE_MAX_FILE  = 2 * 1024 * 1024
_CODE_BATCH_MAX = 4 * 1024 * 1024   # < cap de 5 MB do servidor, com folga

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
     "description": "Recall relevant memories before responding. Call this first with the user's message. Optional `escopo` narrows the search ('universal' | 'projeto:<id>' | 'cena:<id>', or a list); default = universal + this project.",
     "inputSchema": {"type": "object",
                     "properties": {"query": {"type": "string"},
                                    "limit": {"type": "integer", "default": 5},
                                    "escopo": {"description": "scope filter"}},
                     "required": ["query"]}},
    {"name": "fetch_memory",
     "description": "Fetch the full text of specific memories by id (ids come from search_memory).",
     "inputSchema": {"type": "object",
                     "properties": {"ids": {"type": "array", "items": {"type": "string"}}},
                     "required": ["ids"]}},
    {"name": "store_memory",
     "description": "Save a memory. Point at the source of truth instead of copying it; if it overlaps an existing memory, update that one instead of appending a second copy. Optional `tipo` (semantica|episodica|procedural|preferencia|entidade|projeto|coletiva | 'auto'), `escopo` ('universal' default | 'projeto:<id>' | 'cena:<id>') and `autor_id`.",
     "inputSchema": {"type": "object",
                     "properties": {"text": {"type": "string"},
                                    "layer": {"type": "string",
                                              "enum": ["short-term", "mid-term", "long-term"],
                                              "default": "long-term"},
                                    "tipo": {"type": "string"},
                                    "escopo": {"type": "string"},
                                    "autor_id": {"type": "string"}},
                     "required": ["text"]}},
    {"name": "list_memory",
     "description": "List stored memories, most recent first.",
     "inputSchema": {"type": "object",
                     "properties": {"limit": {"type": "integer", "default": 20}}}},
    {"name": "forget_memory",
     "description": "Close a memory's validity by id (soft): it stops showing up "
                    "in recall but stays readable by id for audit. Not a physical "
                    "delete.",
     "inputSchema": {"type": "object",
                     "properties": {"id": {"type": "string"},
                                    "motivo": {"type": "string",
                                               "description": "corrigida|refutada|obsoleta|duplicada|removida",
                                               "default": "removida"}},
                     "required": ["id"]}},
    {"name": "search_code",
     "description": "Semantic search inside the project's ingested code symbols "
                    "(functions/classes/methods). Use for 'where is X implemented', "
                    "'which function handles Y'. Returns file path + line range per hit, "
                    "with the indexing age.",
     "inputSchema": {"type": "object",
                     "properties": {"query": {"type": "string"},
                                    "limit": {"type": "integer", "default": 5},
                                    "path": {"type": "string",
                                             "description": "Optional: one file path"}},
                     "required": ["query"]}},
    {"name": "map_code",
     "description": "Skeleton of one ingested code file: every symbol with its line "
                    "range. Use instead of reading the whole file when you only need to "
                    "know WHERE to look. Self-heals against the local file if CM_CODE_ROOT is set.",
     "inputSchema": {"type": "object",
                     "properties": {"path": {"type": "string"}},
                     "required": ["path"]}},
    {"name": "get_symbol",
     "description": "Full body of ONE symbol (function/class/method) from an ingested "
                    "file. Read only the piece you need. Self-heals against the local "
                    "file if CM_CODE_ROOT is set; always reports the indexing age.",
     "inputSchema": {"type": "object",
                     "properties": {"path": {"type": "string"},
                                    "simbolo": {"type": "string"}},
                     "required": ["path", "simbolo"]}},
    {"name": "get_symbol_refs",
     "description": "RELATIONSHIPS between symbols: who uses/calls/references a "
                    "given symbol across the whole project (cross-file). Returns "
                    "every usage point with file, exact line and the code snippet "
                    "of that line - like IDE 'find references'. Use when "
                    "refactoring, renaming, or understanding impact: 'who calls "
                    "foo?', 'where is X used?'. Self-definitions are excluded.",
     "inputSchema": {"type": "object",
                     "properties": {"path": {"type": "string",
                                             "description": "File path where the symbol is defined (as ingested)"},
                                    "simbolo": {"type": "string",
                                                "description": "Symbol name to find references for"},
                                    "limit": {"type": "integer", "default": 50}},
                     "required": ["path", "simbolo"]}},
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


# ── Camada de código: frescor sem sync GitHub ───────────────────────────────
def _code_root():
    """Raiz do código do projeto. Ordem: CM_CODE_ROOT explícito → auto-detect
    (sobe do cwd até achar .git — o cliente roda o SDK dentro do repo dele e o
    Cube ingere sozinho, sem setup). Retorna None se nada encontrado."""
    if CODE_ROOT:
        r = os.path.abspath(os.path.expanduser(CODE_ROOT))
        if os.path.isdir(r):
            return r
    # auto-detect: sobe até 8 níveis procurando .git
    d = os.path.abspath(os.getcwd())
    for _ in range(9):
        if os.path.isdir(os.path.join(d, ".git")) or os.path.isfile(os.path.join(d, ".git")):
            return d
        parent = os.path.dirname(d)
        if parent == d:
            break
        d = parent
    return None


def _iter_code_files(root):
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d not in _CODE_SKIP and not d.startswith(".")]
        for fn in filenames:
            if not fn.endswith(_CODE_EXT):
                continue
            ap = os.path.join(dirpath, fn)
            try:
                if os.path.getsize(ap) > _CODE_MAX_FILE:
                    continue
            except OSError:
                continue
            rel = os.path.relpath(ap, root).replace(os.sep, "/")
            yield rel, ap


def _sha256_file(ap):
    h = hashlib.sha256()
    try:
        with open(ap, "rb") as f:
            for chunk in iter(lambda: f.read(65536), b""):
                h.update(chunk)
    except OSError:
        return None
    return h.hexdigest()


def _age_note(indexed_at) -> str:
    if not indexed_at:
        return "(indexing age unknown)"
    h = (time.time() - float(indexed_at)) / 3600.0
    base = f"indexed {h:.1f}h ago" if h >= 1 else f"indexed {h * 60:.0f}min ago"
    return base + ("  ⚠ possibly stale — file may have changed since" if h > CODE_STALE_H else "")


def _ingest_paths(root, rels) -> tuple:
    """Ingesta uma lista de paths relativos em lotes < cap do servidor."""
    sent = 0
    batch, size = [], 0
    def flush():
        nonlocal batch, size
        if not batch:
            return
        _request("POST", "/v1/code/ingest", {"files": batch})
        batch, size = [], 0
    for rel in rels:
        ap = os.path.join(root, rel)
        try:
            content = open(ap, encoding="utf-8", errors="replace").read()
        except OSError:
            continue
        b = len(content.encode("utf-8", "replace"))
        if size + b > _CODE_BATCH_MAX or len(batch) >= 180:
            flush()
        batch.append({"path": rel, "content": content})
        size += b
        sent += 1
    flush()
    return sent


def _ensure_fresh(path: str):
    """Auto-cura (§5.2): se a raiz local existe e o arquivo difere do indexado,
    re-ingesta esse arquivo antes de responder. No-op sem CM_CODE_ROOT."""
    root = _code_root()
    if not root:
        return
    ap = os.path.join(root, path.replace("/", os.sep))
    if not os.path.isfile(ap):
        return
    sha = _sha256_file(ap)
    if not sha:
        return
    st, body = _request("POST", "/v1/code/manifest", {"files": [{"path": path, "sha256": sha}]})
    if st != 200 or not body:
        return
    if path in (body.get("stale") or []) or path in (body.get("missing") or []):
        _ingest_paths(root, [path])


def sync_code() -> str:
    """Delta-ingest (§5.1): hash de cada arquivo sob CM_CODE_ROOT → o servidor diz
    o que mudou → re-ingesta só isso. Também remove do índice o que sumiu."""
    root = _code_root()
    if not root:
        return ("CM_CODE_ROOT não definido (ou não é um diretório). "
                "Defina para o diretório do projeto para habilitar o sync.")
    manifest = [{"path": rel, "sha256": _sha256_file(ap)}
                for rel, ap in _iter_code_files(root)]
    if not manifest:
        return f"Nenhum arquivo de código encontrado sob {root}."
    st, body = _request("POST", "/v1/code/manifest", {"files": manifest})
    if st != 200 or body is None:
        return f"Error: /v1/code/manifest HTTP {st}"
    stale = body.get("stale") or []
    missing = body.get("missing") or []
    deleted = body.get("deleted") or []
    todo = missing + stale
    n = _ingest_paths(root, todo) if todo else 0
    if deleted:
        _request("POST", "/v1/code/forget", {"paths": deleted})
    return (f"sync: {len(manifest)} arquivo(s) escaneado(s); "
            f"{len(missing)} novo(s), {len(stale)} alterado(s) re-ingerido(s) ({n}); "
            f"{len(deleted)} removido(s); {body.get('ok', 0)} já em dia.")


def call_tool(name: str, args: dict) -> str:
    if name == "search_memory":
        _p = {"query": args["query"], "limit": args.get("limit", 5)}
        if args.get("escopo"):
            _p["escopo"] = args["escopo"]
        st, body = _request("POST", "/v1/memory/search", _p)
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
        _p = {"id": mid, "text": text, "layer": args.get("layer", "long-term")}
        if args.get("tipo"):
            _p["tipo"] = args["tipo"]
        if args.get("escopo"):
            _p["escopo"] = args["escopo"]
        if args.get("autor_id"):
            _p["autor_id"] = args["autor_id"]
        st, body = _request("POST", "/v1/memory", _p)
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
        # Fase 1: fechar vigência (soft), não deletar físico. A memória some da
        # busca mas continua legível por id (auditoria).
        st, _ = _request("POST", f"/v1/memory/{mid}/close",
                         {"motivo": args.get("motivo", "removida")})
        if st == 200:
            return "Memory closed (removed from recall; still readable by id)."
        if st == 404:
            return "Memory not found."
        return f"Error: HTTP {st}"

    if name == "search_code":
        payload = {"query": args.get("query", ""), "limit": args.get("limit", 5)}
        if args.get("path"):
            payload["path"] = args["path"]
        st, body = _request("POST", "/v1/code/search", payload)
        if st != 200:
            return f"Error: HTTP {st}"
        res = (body or {}).get("results", [])
        if not res:
            return "No matching code symbols. (Run `cube_memory_mcp.py sync` to ingest code.)"
        return "\n".join(
            f"[{i}] {h.get('caminho')}:{h.get('linha_ini')}-{h.get('linha_fim')}  "
            f"{h.get('simbolo')} ({h.get('kind')})  score {float(h.get('score', 0)):.3f}  "
            f"{_age_note(h.get('indexed_at'))}\n    {h.get('assinatura', '')}"
            for i, h in enumerate(res, 1))

    if name == "map_code":
        path = (args.get("path") or "").strip()
        if not path:
            return "Error: path is required."
        _ensure_fresh(path)
        st, body = _request("POST", "/v1/code/map", {"path": path})
        if st == 404:
            return f"File not indexed: {path}"
        if st != 200:
            return f"Error: HTTP {st}"
        return f"{(body or {}).get('mapa', '')}\n\n({_age_note((body or {}).get('indexed_at'))})"

    if name == "get_symbol":
        path = (args.get("path") or "").strip()
        simbolo = (args.get("simbolo") or args.get("symbol") or "").strip()
        if not path or not simbolo:
            return "Error: path and simbolo are required."
        _ensure_fresh(path)
        st, body = _request("POST", "/v1/code/symbol", {"path": path, "simbolo": simbolo})
        if st == 404:
            return f"Symbol not found: {simbolo} in {path}"
        if st != 200:
            return f"Error: HTTP {st}"
        matches = (body or {}).get("matches", [])
        if not matches:
            return f"Symbol not found: {simbolo} in {path}"
        m0 = matches[0]
        head = (f"{m0.get('caminho')}:{m0.get('linha_ini')}-{m0.get('linha_fim')}  "
                f"{m0.get('simbolo')} ({m0.get('kind')})  {_age_note(m0.get('indexed_at'))}")
        if (body or {}).get("ambiguo"):
            head += f"  [+{(body or {}).get('total', 1) - 1} other definition(s) same name]"
        return f"{head}\n\n{m0.get('corpo', '')}"

    if name == "get_symbol_refs":
        path = (args.get("path") or "").strip()
        simbolo = (args.get("simbolo") or args.get("symbol") or "").strip()
        if not path or not simbolo:
            return "Error: path and simbolo are required."
        _ensure_fresh(path)
        payload = {"path": path, "simbolo": simbolo, "limit": args.get("limit", 50)}
        st, body = _request("POST", "/v1/code/refs", payload)
        if st == 404:
            return f"Symbol not found: {simbolo} in {path}"
        if st != 200:
            return f"Error: HTTP {st}"
        matches = (body or {}).get("matches", [])
        if not matches:
            return (f"{simbolo} em {path}: nenhum uso encontrado no projeto "
                    f"(0 referências).")
        total = (body or {}).get("total", len(matches))
        out = [f"{simbolo} em {path} é usado em {total} ponto(s)"
               + (" (truncado)" if (body or {}).get("truncado") else "") + ":"]
        cur = None
        for i, h in enumerate(matches, 1):
            ref = f"{h.get('caminho')}:{h.get('linha')}  em {h.get('simbolo')} ({h.get('kind')})"
            if ref != cur:
                out.append(f"[{i}] {ref}")
                cur = ref
            out.append(f"      {h.get('trecho', '')}")
        return "\n".join(out)

    return f"Unknown tool: {name}"


def send(obj):
    sys.stdout.write(json.dumps(obj) + "\n")
    sys.stdout.flush()


def main():
    if not API_KEY or not PID:
        sys.stderr.write("Error: set CM_API_KEY and CM_PROJECT_ID env vars\n")
        sys.exit(1)
    # `cube_memory_mcp.py sync` — delta-ingest do código (wire num SessionStart hook)
    if len(sys.argv) > 1 and sys.argv[1] in ("sync", "sync-code", "code-sync"):
        sys.stdout.write(sync_code() + "\n")
        sys.exit(0)
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
                "serverInfo": {"name": "cube-memory", "version": "1.3"}}})
        elif method == "notifications/initialized":
            # SessionStart: sync automático do código do projeto. O cliente só
            # precisa rodar o SDK dentro do repo (ou CM_CODE_ROOT) — a raiz é
            # auto-detectada e o delta-ingest acontece sozinho, sem setup.
            try:
                if _code_root():
                    sync_code()
            except Exception:
                pass  # best-effort: nunca derruba a sessão por sync
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
