# s06: Context Compact

`s01 > s02 > s03 > s04 > s05 > [ s06 ] | s07 > s08 > s09 > s10 > s11 > s12`

> *"コンテキストはいつか溢れる、空ける手段が要る"* -- 3層圧縮で無限セッションを実現。

## 問題

コンテキストウィンドウは有限だ。1000行のファイルに対する`read_file`1回で約4000トークンを消費する。30ファイルを読み20回のbashコマンドを実行すると、100,000トークン超。圧縮なしでは、エージェントは大規模コードベースで作業できない。

## 解決策

積極性を段階的に上げる3層構成:

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

## 仕組み

1. **第1層 -- micro_compact**: 各LLM呼び出しの前に、古いツール結果をプレースホルダーに置換する。

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

2. **第2層 -- auto_compact**: トークンが閾値を超えたら、完全なトランスクリプトをディスクに保存し、LLMに要約を依頼する。

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

3. **第3層 -- manual compact**: `compact`ツールが同じ要約処理をオンデマンドでトリガーする。

4. ループが3層すべてを統合する:

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

トランスクリプトがディスク上に完全な履歴を保持する。何も真に失われず、アクティブなコンテキストの外に移動されるだけ。

## s05からの変更点

| Component      | Before (s05)     | After (s06)                |
|----------------|------------------|----------------------------|
| Tools          | 5                | 5 (base + compact)         |
| Context mgmt   | None             | Three-layer compression    |
| Micro-compact  | None             | Old results -> placeholders|
| Auto-compact   | None             | Token threshold trigger    |
| Transcripts    | None             | Saved to .transcripts/     |

## 試してみる

```sh
cd learn-ollama-code
uv run agents/s06_context_compact.py
```

1. `agents/ディレクトリ内のすべてのPythonファイルを一つずつ読む` (micro-compactが古い結果を置換するのを観察する)
2. `圧縮が自動的にトリガーされるまでファイルを読み続ける`
3. `compactツールを使って会話を手動で圧縮する`
