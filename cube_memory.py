"""
Cube Memory SDK — persistent semantic memory + PKM notes for any LLM.
Zero-proxy: your LLM API keys NEVER leave your app.

Install:  pip install requests           (only dependency)
Docs:     https://cubememory.com.br/docs

Quick start (2 lines):
    from cube_memory import Memory
    mem = Memory(api_key="cm_live_...", project="proj_...")
    mem.wrap(openai_client)   # done — every LLM call is captured automatically

Manual:
    context = mem.recall("user question", limit=5)
    mem.remember("user said X, assistant replied Y")

PKM notes (separate namespace, heading-chunked + semantic search):
    from cube_memory import Notes
    notes = Notes(api_key="cm_live_...", project="proj_...")
    notes.save(title="Auth design", text="# Tokens\nWe use JWT...")
"""
import time, hashlib, json, threading, functools
from typing import Optional, Any

try:
    import requests as _req
except ImportError:
    raise ImportError("pip install requests")

_BASE = "https://cubememory.com.br/gateway"


class Memory:
    """Persistent semantic memory for one project."""

    def __init__(self, api_key: str, project: str, base_url: str = _BASE):
        self._key  = api_key
        self._pid  = project
        self._base = base_url.rstrip("/")
        self._h    = {
            "Authorization": f"Bearer {api_key}",
            "X-Project-Id":  project,
            "Content-Type":  "application/json",
        }
        self._verify()

    def _verify(self):
        # limit=1: valida a chave sem baixar a base inteira (payload de MB e
        # segundos em projetos grandes; era a causa de timeouts no init).
        r = _req.get(f"{self._base}/v1/memory", headers=self._h,
                     params={"limit": 1}, timeout=15)
        if r.status_code == 401:
            raise ValueError("Invalid API key or project ID")

    # ── core ──────────────────────────────────────────────────────────────────

    def checkpoint(self, workflow_id: str, **checkpoint) -> dict:
        """Save a versioned checkpoint; expected_revision=0 creates it.

        Retry with the same request_id and identical body after a timeout.
        A completed action requires source/content evidence; this records the
        caller's evidence and does not independently verify the external action.
        """
        from urllib.parse import quote
        r = _req.put(f"{self._base}/v1/workflows/{quote(workflow_id, safe='')}/checkpoint",
                     headers=self._h, json=checkpoint, timeout=30)
        r.raise_for_status()
        return r.json()

    def resume(self, workflow_id: str) -> dict:
        """Fetch pending work after interruption; read before issuing new actions."""
        from urllib.parse import quote
        r = _req.get(f"{self._base}/v1/workflows/{quote(workflow_id, safe='')}/resume",
                     headers=self._h, timeout=30)
        r.raise_for_status()
        return r.json()

    def list_work(self, limit: int = 50) -> list:
        r = _req.get(f"{self._base}/v1/workflows", headers=self._h,
                     params={'limit': limit}, timeout=30)
        r.raise_for_status()
        return r.json()['workflows']

    def ingest_tracked(self, workflow_id: str, index_name: str, documents: list,
                       request_id: str) -> dict:
        """Ingest a batch with a persisted trace. Reuse IDs/body on retries."""
        from urllib.parse import quote
        r = _req.post(f"{self._base}/v1/workflows/{quote(workflow_id, safe='')}/ingest",
                      headers=self._h, json={'index_name': index_name,
                      'documents': documents, 'request_id': request_id}, timeout=120)
        r.raise_for_status()
        return r.json()

    def remember(self, text: str, layer: str = "short-term", mem_id: Optional[str] = None) -> str:
        """Store a memory. Returns the memory ID."""
        mid = mem_id or f"mem_{hashlib.sha1(f'{text}{time.time()}'.encode()).hexdigest()[:12]}"
        r = _req.post(f"{self._base}/v1/memory", headers=self._h, timeout=15,
                      json={"id": mid, "text": text.strip(), "layer": layer})
        r.raise_for_status()
        return mid

    def _remember_bg(self, text: str, layer: str = "short-term"):
        """Fire-and-forget memory store (background thread, never blocks LLM calls)."""
        def _store():
            try:
                self.remember(text, layer=layer)
            except Exception:
                pass
        threading.Thread(target=_store, daemon=True).start()

    def recall(self, query: str, limit: int = 5, as_messages: bool = False):
        """
        Semantic search over stored memories.

        as_messages=False → list of {"text", "score", "layer"} dicts
        as_messages=True  → list of {"role": "system", "content": "..."} ready to inject into LLM
        """
        r = _req.post(f"{self._base}/v1/memory/search", headers=self._h, timeout=60,
                      json={"query": query, "limit": limit})
        r.raise_for_status()
        results = r.json().get("results", [])
        if not results:
            return []
        if not as_messages:
            return [{"text": x["text_preview"], "score": x["score"],
                     "layer": x["metadata"].get("layer", "short-term")} for x in results]
        block = "\n".join(f"- {x['text_preview']}" for x in results)
        return [{"role": "system", "content": f"[Relevant memories]\n{block}"}]

    def list(self, limit: int = 50):
        """List stored memories, newest first (paginated server-side)."""
        r = _req.get(f"{self._base}/v1/memory", headers=self._h,
                     params={"limit": limit}, timeout=30)
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

    # ── auto-capture: wrap any LLM client transparently ──────────────────────

    def wrap(self, client: Any) -> Any:
        """
        Transparently patches any LLM client to auto-capture all conversations.
        Your LLM API key NEVER touches Cube Memory — only conversation text is stored.

        Supports: openai.OpenAI, anthropic.Anthropic, google.generativeai.GenerativeModel,
                  any object with chat.completions.create / messages.create / generate_content.

        Example:
            from openai import OpenAI
            from cube_memory import Memory

            client = OpenAI()                          # normal OpenAI client
            mem = Memory("cm_live_...", "proj_...")
            mem.wrap(client)                           # ← 1 line, done

            # existing code unchanged — every response auto-memorized
            resp = client.chat.completions.create(model="gpt-4o", messages=[...])
        """
        mod = type(client).__module__.split(".")[0]

        if mod == "openai" or hasattr(client, "chat"):
            self._wrap_openai(client)
        elif mod == "anthropic" or hasattr(client, "messages"):
            self._wrap_anthropic(client)
        elif hasattr(client, "generate_content"):
            self._wrap_google(client)
        else:
            # Generic: try both patterns silently
            try: self._wrap_openai(client)
            except Exception: pass
            try: self._wrap_anthropic(client)
            except Exception: pass

        return client

    def _wrap_openai(self, client):
        orig = client.chat.completions.create

        @functools.wraps(orig)
        def _patched(*args, **kwargs):
            response = orig(*args, **kwargs)
            try:
                msgs   = list(args[0]) if args else kwargs.get("messages", [])
                model  = kwargs.get("model", "?")
                user   = next((m.get("content","") for m in reversed(msgs)
                               if m.get("role") == "user"), "")
                asst   = ""
                if hasattr(response, "choices") and response.choices:
                    asst = response.choices[0].message.content or ""
                if isinstance(asst, list):
                    asst = " ".join(c.get("text","") for c in asst if isinstance(c,dict))
                if user or asst:
                    self._remember_bg(
                        f"[openai/{model}]\n[User]: {str(user)[:600]}\n[Assistant]: {str(asst)[:1400]}"
                    )
            except Exception:
                pass
            return response

        client.chat.completions.create = _patched

    def _wrap_anthropic(self, client):
        orig = client.messages.create

        @functools.wraps(orig)
        def _patched(*args, **kwargs):
            response = orig(*args, **kwargs)
            try:
                msgs  = kwargs.get("messages", [])
                model = kwargs.get("model", "?")
                user  = next((m.get("content","") for m in reversed(msgs)
                              if m.get("role") == "user"), "")
                if isinstance(user, list):
                    user = " ".join(b.get("text","") for b in user if isinstance(b,dict))
                asst = ""
                if hasattr(response, "content") and response.content:
                    asst = " ".join(
                        b.text for b in response.content if hasattr(b, "text")
                    )
                if user or asst:
                    self._remember_bg(
                        f"[anthropic/{model}]\n[User]: {str(user)[:600]}\n[Assistant]: {str(asst)[:1400]}"
                    )
            except Exception:
                pass
            return response

        client.messages.create = _patched

    def _wrap_google(self, model):
        orig = model.generate_content

        @functools.wraps(orig)
        def _patched(*args, **kwargs):
            response = orig(*args, **kwargs)
            try:
                prompt = str(args[0])[:600] if args else ""
                asst   = ""
                if hasattr(response, "text"):
                    asst = response.text or ""
                elif hasattr(response, "candidates") and response.candidates:
                    parts = response.candidates[0].content.parts
                    asst  = " ".join(p.text for p in parts if hasattr(p, "text"))
                if prompt or asst:
                    self._remember_bg(
                        f"[google/gemini]\n[User]: {prompt}\n[Assistant]: {str(asst)[:1400]}"
                    )
            except Exception:
                pass
            return response

        model.generate_content = _patched

    # ── decorator: capture any custom function ────────────────────────────────

    def capture(self, fn=None, *, extract=None):
        """
        Decorator to auto-capture any function that returns LLM text.

        @mem.capture
        def ask(prompt):
            return my_llm_client.complete(prompt)

        # or with custom extractor:
        @mem.capture(extract=lambda r: r["output"]["text"])
        def ask(prompt):
            return my_custom_llm(prompt)
        """
        def decorator(func):
            @functools.wraps(func)
            def wrapper(*args, **kwargs):
                result = func(*args, **kwargs)
                try:
                    text = extract(result) if extract else (
                        result if isinstance(result, str) else str(result)
                    )
                    prompt = str(args[0])[:400] if args else ""
                    self._remember_bg(
                        f"[{func.__name__}]\n[Input]: {prompt}\n[Output]: {text[:1400]}"
                    )
                except Exception:
                    pass
                return result
            return wrapper
        return decorator(fn) if fn else decorator

    # ── manual chat with auto-inject + auto-store ─────────────────────────────

    def chat(self, messages: list, llm_fn, query: Optional[str] = None,
             recall_limit: int = 5):
        """
        Wraps one LLM call: injects relevant memories before, stores exchange after.

        llm_fn: callable(messages) → str

        Example:
            reply = mem.chat(
                messages=[{"role":"user","content":"what did we discuss?"}],
                llm_fn=lambda msgs: client.chat.completions.create(
                    model="gpt-4o", messages=msgs
                ).choices[0].message.content
            )
        """
        user_text = query or next(
            (m["content"] for m in reversed(messages) if m.get("role") == "user"), ""
        )
        mem_msgs  = self.recall(user_text, limit=recall_limit, as_messages=True) if user_text else []
        reply     = llm_fn(mem_msgs + messages)

        if user_text:
            self._remember_bg(f"User: {user_text[:500]}")
        if reply:
            self._remember_bg(f"Assistant: {str(reply)[:1000]}")

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
