# s01: The Agent Loop

`[ s01 ] s02 > s03 > s04 > s05 > s06 | s07 > s08 > s09 > s10 > s11 > s12`

> *"One loop & Bash is all you need"* -- one tool + one loop = an agent.

## Problem

A language model can reason about code, but it can't *touch* the real world -- can't read files, run tests, or check errors. Without a loop, every tool call requires you to manually copy-paste results back. You become the loop.

## Solution

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

One exit condition controls the entire flow. The loop runs until the model stops calling tools.

## How It Works

1. User prompt becomes the first message.

```python
messages.append({"role": "user", "content": query})
```

2. Send messages + tool definitions to the LLM.

```python
response = client.chat(
    model=MODEL, messages=messages, tools=TOOLS, think=THINK,
)
```

3. Append the assistant response. Check `tool_calls` -- if the model didn't call a tool, we're done.

```python
messages.append(response.message)
if not response.message.tool_calls:
    return
```

4. Execute each tool call, append results as tool messages. Loop back to step 2.

```python
for tool in response.message.tool_calls:
    output = bash(tool.function.arguments["command"])
    messages.append({
        "role": "tool",
        "content": output,
        "tool_name": tool.function.name,
    })
```

Assembled into one function:

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

That's the entire agent in under 30 lines. Everything else in this course layers on top -- without changing the loop.

## What Changed

| Component     | Before     | After                          |
|---------------|------------|--------------------------------|
| Agent loop    | (none)     | `while True` + stop_reason     |
| Tools         | (none)     | `bash` (one tool)              |
| Messages      | (none)     | Accumulating list              |
| Control flow  | (none)     | `not tool_calls`               |

## Try It

```sh
cd learn-ollama-code
uv run agents/s01_agent_loop.py
```

1. `Create a file called hello.py that prints "Hello, World!"`
2. `List all Python files in this directory`
3. `What is the current git branch?`
4. `Create a directory called test_output and write 3 files in it`
