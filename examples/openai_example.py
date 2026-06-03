"""OpenAI + Cube Memory — persistent memory across sessions."""
from openai import OpenAI
from cube_memory import Memory

client = OpenAI()
mem = Memory(api_key="cm_live_...", project="proj_...")

reply = mem.chat(
    messages=[{"role": "user", "content": "Remind me what we decided about the database."}],
    llm_fn=lambda msgs: client.chat.completions.create(
        model="gpt-4o", messages=msgs
    ).choices[0].message.content,
)
print(reply)
