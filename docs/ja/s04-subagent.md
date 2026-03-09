# s04: Subagents

`s01 > s02 > s03 > [ s04 ] s05 > s06 | s07 > s08 > s09 > s10 > s11 > s12`

> *"大きなタスクを分割し、各サブタスクにクリーンなコンテキストを"* -- サブエージェントは独立した messages[] を使い、メイン会話を汚さない。

## 問題

エージェントが作業するにつれ、messages配列は膨張し続ける。すべてのファイル読み取り、すべてのbash出力がコンテキストに永久に残る。「このプロジェクトはどのテストフレームワークを使っているか」という質問は5つのファイルを読む必要があるかもしれないが、親に必要なのは「pytest」という答えだけだ。

## 解決策

```
Parent agent                     Subagent
+------------------+             +------------------+
| messages=[...]   |             | messages=[]      | <-- fresh
|                  |  dispatch   |                  |
| tool: task       | ----------> | while tool_use:  |
|   prompt="..."   |             |   call tools     |
|                  |  summary    |   append results |
|   result = "..." | <---------- | return last text |
+------------------+             +------------------+

Parent context stays clean. Subagent context is discarded.
```

## 仕組み

1. 親に`task`ツールを追加する。子は`task`を除くすべての基本ツールを取得する(再帰的な生成は不可)。

```python
def task(prompt: str, description: str = ""):
    """
    Spawn a subagent with fresh context. It shares the filesystem but not conversation history.

    Args:
        prompt (str): The task for the subagent to complete
        description (str): Short label describing the subtask
    """

CHILD_TOOLS = [bash, read_file, write_file, edit_file]
PARENT_TOOLS = CHILD_TOOLS + [task]
```

2. サブエージェントは`messages=[]`で開始し、自身のループを実行する。最終テキストだけが親に返る。

```python
def run_subagent(prompt: str) -> str:
    sub_messages = [
        {"role": "system", "content": SUBAGENT_SYSTEM},
        {"role": "user", "content": prompt},
    ]  # fresh context
    response = None
    for _ in range(30):  # safety limit
        response = client.chat(
            model=MODEL, messages=sub_messages, tools=CHILD_TOOLS, think=THINK,
        )
        sub_messages.append(response.message)
        if not response.message.tool_calls:
            break
        for tool in response.message.tool_calls:
            handler = TOOL_HANDLERS.get(tool.function.name)
            output = handler(**tool.function.arguments) if handler else f"Unknown tool: {tool.function.name}"
            sub_messages.append({"role": "tool", "content": str(output)[:50000],
                                 "tool_name": tool.function.name})
    # Only the final text returns to the parent -- child context is discarded
    return (response.message.content if response else None) or "(no summary)"
```

子のメッセージ履歴全体(30回以上のツール呼び出し)は破棄される。親は1段落の要約を通常の`tool_result`として受け取る。

## s03からの変更点

| Component      | Before (s03)     | After (s04)               |
|----------------|------------------|---------------------------|
| Tools          | 5                | 5 (base) + task (parent)  |
| Context        | Single shared    | Parent + child isolation  |
| Subagent       | None             | `run_subagent()` function |
| Return value   | N/A              | Summary text only         |

## 試してみる

```sh
cd learn-ollama-code
uv run agents/s04_subagent.py
```

1. `Use a subtask to find what testing framework this project uses`
2. `Delegate: read all .py files and summarize what each one does`
3. `Use a task to create a new module, then verify it from here`
