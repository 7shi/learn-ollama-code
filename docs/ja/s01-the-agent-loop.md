# s01: The Agent Loop

`[ s01 ] s02 > s03 > s04 > s05 > s06 | s07 > s08 > s09 > s10 > s11 > s12`

> *"One loop & Bash is all you need"* -- 1つのツール + 1つのループ = エージェント。

## 問題

言語モデルはコードについて推論できるが、現実世界に触れられない。ファイルを読めず、テストを実行できず、エラーを確認できない。ループがなければ、ツール呼び出しのたびにユーザーが手動で結果をコピーペーストする必要がある。つまりユーザー自身がループになる。

## 解決策

```
+--------+      +-------+      +---------+
|  User  | ---> |  LLM  | ---> |  Tool   |
| prompt |      |       |      | execute |
+--------+      +---+---+      +----+----+
                    ^                |
                    |   tool_result  |
                    +----------------+
                    (loop until tool_calls is None)
```

1つの終了条件がフロー全体を制御する。モデルがツール呼び出しを止めるまでループが回り続ける。

## 仕組み

1. ユーザーのプロンプトが最初のメッセージになる。

```python
messages.append({"role": "user", "content": query})
```

2. メッセージとツール定義をLLMに送信する。

```python
response = client.chat(
    model=MODEL, messages=messages, tools=TOOLS, think=THINK,
)
```

3. アシスタントのレスポンスを追加し、`tool_calls`を確認する。ツールが呼ばれなければ終了。

```python
messages.append(response.message)
if not response.message.tool_calls:
    return
```

4. 各ツール呼び出しを実行し、結果をtoolメッセージとして追加。ステップ2に戻る。

```python
for tool in response.message.tool_calls:
    output = bash(tool.function.arguments["command"])
    messages.append({
        "role": "tool",
        "content": output,
        "tool_name": tool.function.name,
    })
```

1つの関数にまとめると:

```python
def agent_loop(messages):
    while True:
        response = client.chat(
            model=MODEL, messages=messages, tools=TOOLS, think=THINK,
        )
        messages.append(response.message)

        if not response.message.tool_calls:
            return

        for tool in response.message.tool_calls:
            output = bash(tool.function.arguments["command"])
            messages.append({
                "role": "tool",
                "content": output,
                "tool_name": tool.function.name,
            })
```

これでエージェント全体が30行未満に収まる。本コースの残りはすべてこのループの上に積み重なる -- ループ自体は変わらない。

## 変更点

| Component     | Before     | After                          |
|---------------|------------|--------------------------------|
| Agent loop    | (none)     | `while True` + stop_reason     |
| Tools         | (none)     | `bash` (one tool)              |
| Messages      | (none)     | Accumulating list              |
| Control flow  | (none)     | `not tool_calls`               |

## 試してみる

```sh
cd learn-ollama-code
uv run agents/s01_agent_loop.py
```

1. `Create a file called hello.py that prints "Hello, World!"`
2. `List all Python files in this directory`
3. `What is the current git branch?`
4. `Create a directory called test_output and write 3 files in it`
