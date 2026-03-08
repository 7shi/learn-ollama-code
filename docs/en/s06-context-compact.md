# s06: Context Compact

`s01 > s02 > s03 > s04 > s05 > [ s06 ] | s07 > s08 > s09 > s10 > s11 > s12`

> *"Context will fill up; you need a way to make room"* -- three-layer compression strategy for infinite sessions.

## Problem

The context window is finite. A single `read_file` on a 1000-line file costs ~4000 tokens. After reading 30 files and running 20 bash commands, you hit 100,000+ tokens. The agent cannot work on large codebases without compression.

## Solution

Three layers, increasing in aggressiveness:

```
Every turn:
+------------------+
| Tool call result |
+------------------+
        |
        v
[Layer 1: micro_compact]        (silent, every turn)
  Replace tool_result > 3 turns old
  with "[Previous: used {tool_name}]"
        |
        v
[Check: tokens > 50000?]
   |               |
   no              yes
   |               |
   v               v
continue    [Layer 2: auto_compact]
              Save transcript to .transcripts/
              LLM summarizes conversation.
              Replace all messages with [summary].
                    |
                    v
            [Layer 3: compact tool]
              Model calls compact explicitly.
              Same summarization as auto_compact.
```

## How It Works

1. **Layer 1 -- micro_compact**: Before each LLM call, replace old tool results with placeholders.

```python
def micro_compact(messages: list) -> list:
    # Collect indices of all tool result messages
    tool_indices = [i for i, m in enumerate(messages)
                    if isinstance(m, dict) and m.get("role") == "tool"]
    if len(tool_indices) <= KEEP_RECENT:
        return messages
    # Clear old results (keep last KEEP_RECENT)
    for i in tool_indices[:-KEEP_RECENT]:
        msg = messages[i]
        if isinstance(msg.get("content"), str) and len(msg["content"]) > 100:
            msg["content"] = f"[Previous: used {msg.get('tool_name', 'unknown')}]"
    return messages
```

2. **Layer 2 -- auto_compact**: When tokens exceed threshold, save full transcript to disk, then ask the LLM to summarize.

```python
def auto_compact(messages: list) -> list:
    # Save transcript for recovery
    transcript_path = TRANSCRIPT_DIR / f"transcript_{int(time.time())}.jsonl"
    with open(transcript_path, "w") as f:
        for msg in messages:
            f.write(json.dumps(msg, default=str, ensure_ascii=False) + "\n")
    # LLM summarizes
    response = client.chat(
        model=MODEL,
        messages=[{"role": "user", "content":
            "Summarize this conversation for continuity..."
            + json.dumps(messages, default=str, ensure_ascii=False)[:80000]}],
        think=THINK,
    )
    summary = response.message.content
    return [
        {"role": "user", "content": f"[Conversation compressed. Transcript: {transcript_path}]\n\n{summary}"},
        {"role": "assistant", "content": "Understood. I have the context from the summary. Continuing."},
    ]
```

3. **Layer 3 -- manual compact**: The `compact` tool triggers the same summarization on demand.

4. The loop integrates all three:

```python
def agent_loop(messages: list):
    while True:
        micro_compact(messages)                        # Layer 1
        if estimate_tokens(messages) > THRESHOLD:
            messages[:] = auto_compact(messages)       # Layer 2
        response = client.chat(
            model=MODEL, messages=messages, tools=TOOLS, think=THINK,
        )
        # ... tool execution ...
        if manual_compact:
            messages[:] = auto_compact(messages)       # Layer 3
```

Transcripts preserve full history on disk. Nothing is truly lost -- just moved out of active context.

## What Changed From s05

| Component      | Before (s05)     | After (s06)                |
|----------------|------------------|----------------------------|
| Tools          | 5                | 5 (base + compact)         |
| Context mgmt   | None             | Three-layer compression    |
| Micro-compact  | None             | Old results -> placeholders|
| Auto-compact   | None             | Token threshold trigger    |
| Transcripts    | None             | Saved to .transcripts/     |

## Try It

```sh
cd learn-ollama-code
uv run agents/s06_context_compact.py
```

1. `Read every Python file in the agents/ directory one by one` (watch micro-compact replace old results)
2. `Keep reading files until compression triggers automatically`
3. `Use the compact tool to manually compress the conversation`
