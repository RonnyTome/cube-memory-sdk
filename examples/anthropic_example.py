"""Anthropic Claude + Cube Memory."""
from anthropic import Anthropic
from cube_memory import Memory

client = Anthropic()
mem = Memory(api_key="cm_live_...", project="proj_...")

def claude(msgs):
    sys = "\n".join(m["content"] for m in msgs if m["role"] == "system")
    turns = [m for m in msgs if m["role"] != "system"]
    r = client.messages.create(model="claude-opus-4-8", max_tokens=1024,
                               system=sys or None, messages=turns)
    return r.content[0].text

reply = mem.chat(
    messages=[{"role": "user", "content": "What's my preferred stack again?"}],
    llm_fn=claude,
)
print(reply)
