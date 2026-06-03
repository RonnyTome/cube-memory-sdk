"""
Cube Memory SDK — persistent semantic memory for any LLM.
Works with OpenAI, Anthropic, Gemini, Ollama — anything.

Install:  pip install requests           (only dependency)
Docs:     https://cubememory.com.br/docs

Quick start:
    from cube_memory import Memory

    mem = Memory(api_key="cm_live_...", project="proj_...")

    # before LLM call
    context = mem.recall("user question here", limit=5)

    # after LLM call
    mem.remember("user said X, assistant replied Y")
"""
import time, hashlib, json
from typing import Optional

try:
    import requests as _req
except ImportError:
    raise ImportError("pip install requests")

_BASE = "https://cubememory.com.br/gateway"


class Memory:
    """Persistent semantic memory for one project."""

    def __init__(self, api_key: str, project: str, base_url: str = _BASE):
        self._key = api_key
        self._pid = project
        self._base = base_url.rstrip("/")
        self._h = {
            "Authorization": f"Bearer {api_key}",
            "X-Project-Id":  project,
            "Content-Type":  "application/json",
        }
        self._verify()

    def _verify(self):
        r = _req.get(f"{self._base}/v1/memory", headers=self._h, timeout=10)
        if r.status_code == 401:
            raise ValueError("Invalid API key or project ID")

    # ── core ──────────────────────────────────────────────────────────────────

    def remember(self, text: str, layer: str = "long-term", mem_id: Optional[str] = None) -> str:
        """Store a memory. Returns the memory ID."""
        mid = mem_id or f"mem_{hashlib.sha1(f'{text}{time.time()}'.encode()).hexdigest()[:12]}"
        r = _req.post(f"{self._base}/v1/memory", headers=self._h, timeout=15,
                      json={"id": mid, "text": text.strip(), "layer": layer})
        r.raise_for_status()
        return mid

    def recall(self, query: str, limit: int = 5, as_messages: bool = False):
        """
        Semantic search over stored memories.

        as_messages=False → list of {"text", "score", "layer"} dicts
        as_messages=True  → list of {"role": "system", "content": "..."} ready to inject into LLM
        """
        r = _req.post(f"{self._base}/v1/memory/search", headers=self._h, timeout=15,
                      json={"query": query, "limit": limit})
        r.raise_for_status()
        results = r.json().get("results", [])
        if not results:
            return [] if not as_messages else []
        if not as_messages:
            return [{"text": x["text_preview"], "score": x["score"],
                     "layer": x["metadata"].get("layer", "long-term")} for x in results]
        # format as system message block ready for any LLM
        block = "\n".join(f"- {x['text_preview']}" for x in results)
        return [{"role": "system", "content": f"[Relevant memories]\n{block}"}]

    def list(self, limit: int = 50):
        """List all stored memories, newest first."""
        r = _req.get(f"{self._base}/v1/memory", headers=self._h, timeout=15)
        r.raise_for_status()
        return r.json().get("memories", [])[:limit]

    def forget(self, mem_id: str) -> bool:
        """Delete a memory by ID. Returns True if deleted."""
        r = _req.delete(f"{self._base}/v1/memory/{mem_id}", headers=self._h, timeout=15)
        return r.status_code in (200, 404)

    def forget_all(self):
        """Delete every memory in this project."""
        for m in self.list(limit=1000):
            self.forget(m["id"])

    # ── auto-memory: the part no company ships ────────────────────────────────

    def chat(self, messages: list, llm_fn, query: Optional[str] = None,
             remember_turns: bool = True, recall_limit: int = 5):
        """
        Drop-in wrapper: injects relevant memories before calling your LLM,
        then stores the exchange automatically.

        llm_fn: callable(messages) → str  (the LLM call)
        query:  what to search in memory (defaults to last user message)

        Example with OpenAI:
            from openai import OpenAI
            client = OpenAI()
            reply = mem.chat(
                messages=[{"role":"user","content":"what did we discuss last time?"}],
                llm_fn=lambda msgs: client.chat.completions.create(
                    model="gpt-4o", messages=msgs
                ).choices[0].message.content
            )
        """
        # find the last user message to use as recall query
        user_text = query or next(
            (m["content"] for m in reversed(messages) if m.get("role") == "user"), ""
        )

        # inject relevant memories as a system message before the conversation
        mem_msgs = self.recall(user_text, limit=recall_limit, as_messages=True) if user_text else []
        augmented = mem_msgs + messages

        # call the LLM
        reply = llm_fn(augmented)

        # auto-store the exchange
        if remember_turns and user_text:
            self.remember(f"User: {user_text[:500]}", layer="short-term")
        if remember_turns and reply:
            self.remember(f"Assistant: {str(reply)[:500]}", layer="short-term")

        return reply

    def __repr__(self):
        return f"Memory(project={self._pid!r})"


class Notes:
    """
    PKM notes with automatic heading-chunking and semantic search.

    Notes are stored as markdown, chunked by heading (#, ##, ###) on the server,
    and encrypted at rest. Search returns the exact heading section that matched.

        from cube_memory import Notes
        notes = Notes(api_key="cm_live_...", project="proj_...")
        notes.save(title="Auth design", text="# Tokens\nWe use JWT...")
        hits = notes.search("how do we authenticate?")
        print(hits[0]["matched_section"])
    """

    def __init__(self, api_key: str, project: str, base_url: str = _BASE):
        self._base = base_url.rstrip("/")
        self._h = {
            "Authorization": f"Bearer {api_key}",
            "X-Project-Id":  project,
            "Content-Type":  "application/json",
        }

    @staticmethod
    def _md_to_doc(text: str) -> dict:
        """Markdown → minimal TipTap doc so the server can chunk by heading."""
        content = []
        for line in text.splitlines():
            s = line.strip()
            if not s:
                continue
            if s.startswith("#"):
                level = len(s) - len(s.lstrip("#"))
                content.append({"type": "heading", "attrs": {"level": min(level, 3)},
                                "content": [{"type": "text", "text": s.lstrip("#").strip()}]})
            else:
                content.append({"type": "paragraph",
                                "content": [{"type": "text", "text": s}]})
        return {"type": "doc", "content": content}

    def save(self, title: str, text: str, tags: Optional[list] = None,
             note_id: Optional[str] = None) -> str:
        """Save a markdown note. Headings become searchable chunks. Returns the note ID."""
        body = {"title": title, "text": text,
                "tiptap_json": json.dumps(self._md_to_doc(text)), "tags": tags or []}
        if note_id:
            body["id"] = note_id
        r = _req.post(f"{self._base}/v1/notes", headers=self._h, json=body, timeout=20)
        r.raise_for_status()
        return r.json()["id"]

    def search(self, query: str, limit: int = 10):
        """
        Semantic search across note chunks. Each result includes 'matched_section'
        — the heading where the match was found — plus 'title', 'score', 'tags'.
        """
        r = _req.post(f"{self._base}/v1/notes/search", headers=self._h,
                      json={"query": query, "limit": limit}, timeout=15)
        r.raise_for_status()
        return r.json().get("results", [])

    def get(self, note_id: str):
        """Load a full note by ID (includes 'related_notes' AI auto-links)."""
        r = _req.get(f"{self._base}/v1/notes/{note_id}", headers=self._h, timeout=15)
        r.raise_for_status()
        return r.json()

    def list(self, limit: int = 50):
        """List all notes, newest first."""
        r = _req.get(f"{self._base}/v1/notes", headers=self._h, timeout=15)
        r.raise_for_status()
        return r.json().get("notes", [])[:limit]

    def delete(self, note_id: str) -> bool:
        """Delete a note and its chunks by ID."""
        r = _req.delete(f"{self._base}/v1/notes/{note_id}", headers=self._h, timeout=15)
        return r.status_code in (200, 404)

    def __repr__(self):
        return "Notes(cube-memory)"
