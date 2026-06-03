# Cube Memory

**Persistent semantic memory + PKM for any LLM.** One API. 100% recall. MCP-native.

> What one AI learns, all your AIs know. Claude, GPT, Gemini, local models — same memory, one endpoint.

[![Get a free key](https://img.shields.io/badge/get-free%20key-6366f1)](https://cubememory.com.br)
[![MCP](https://img.shields.io/badge/MCP-native-06b6d4)](https://cubememory.com.br)
[![License](https://img.shields.io/badge/license-MIT-green)](LICENSE)

---

## 30-second quickstart

```bash
pip install requests
```

```python
from cube_memory import Memory

mem = Memory(api_key="cm_live_...", project="proj_...")

# before the LLM call — recall relevant context
context = mem.recall("what did we decide about auth?", limit=5)

# after the LLM call — remember the exchange
mem.remember("user prefers JWT over sessions")
```

Get your free key at **[cubememory.com.br](https://cubememory.com.br)** (100k vectors free).

## Connect any AI via MCP — zero local setup

Drop this into your Claude Code / Cursor / Windsurf `.mcp.json`:

```json
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
```

Now your AI has `search_memory`, `store_memory`, `search_notes` and `get_note` — automatically.

## Why Cube Memory

| | Cube Memory | Shared-index ANN (HNSW) |
|---|---|---|
| Recall (exact search) | **100%** | ~99.7% |
| Multi-tenant filtered recall | **100%** | collapses to ~10% with naive filtering* |
| Per-tenant isolation | by construction | manual, recall-degrading |
| Zero-Knowledge encryption | ✅ AES-256-GCM, per-project key | ✗ |
| PKM notes + auto-linking | ✅ | ✗ |
| Predictable flat pricing | ✅ | usually metered |

<sub>*Measured: a shared HNSW index with naive metadata post-filtering on 20k vectors / 10 tenants drops to ~10% recall@10 vs cosine ground truth; recovering 100% requires ~20× over-fetch. Isolated indexes avoid this entirely. Numbers reproducible — see [the benchmark](#benchmark).</sub>

## Auto-memory wrapper (the part nobody ships)

```python
from openai import OpenAI
client = OpenAI()

reply = mem.chat(
    messages=[{"role": "user", "content": "what did we discuss last time?"}],
    llm_fn=lambda msgs: client.chat.completions.create(
        model="gpt-4o", messages=msgs
    ).choices[0].message.content,
)
# memories are injected before the call and the exchange stored after — automatically
```

## Files

- [`cube_memory.py`](cube_memory.py) — the client (only dependency: `requests`)
- [`cube_memory_mcp.py`](cube_memory_mcp.py) — stdio MCP server for local clients
- [`examples/`](examples/) — OpenAI, Anthropic, and MCP setup

## Benchmark

Reproducible recall + latency comparison (exact vs HNSW, plus the multi-tenant
filtered-recall test) lives at **[cubememory.com.br](https://cubememory.com.br)**.
Same machine, same 384D embeddings, cosine ground truth.

## License

MIT — the client SDK is open. The hosted engine is proprietary.

---

Built by [cubememory.com.br](https://cubememory.com.br) · memory that any LLM can hold.
