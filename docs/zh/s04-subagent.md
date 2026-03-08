# s04: Subagents (子智能体)

`s01 > s02 > s03 > [ s04 ] s05 > s06 | s07 > s08 > s09 > s10 > s11 > s12`

> *"大任务拆小, 每个小任务干净的上下文"* -- 子智能体用独立 messages[], 不污染主对话。

## 问题

智能体工作越久, messages 数组越胖。每次读文件、跑命令的输出都永久留在上下文里。"这个项目用什么测试框架?" 可能要读 5 个文件, 但父智能体只需要一个词: "pytest。"

## 解决方案

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

## 工作原理

1. 父智能体有一个 `task` 工具。子智能体拥有除 `task` 外的所有基础工具 (禁止递归生成)。

```python
PARENT_TOOLS = CHILD_TOOLS + [
    {"type": "function", "function": {"name": "task",
     "description": "Spawn a subagent with fresh context.",
     "parameters": {
         "type": "object",
         "properties": {"prompt": {"type": "string"}},
         "required": ["prompt"],
     }}},
]
```

2. 子智能体以 `messages=[]` 启动, 运行自己的循环。只有最终文本返回给父智能体。

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

子智能体可能跑了 30+ 次工具调用, 但整个消息历史直接丢弃。父智能体收到的只是一段摘要文本, 作为普通 `tool_result` 返回。

## 相对 s03 的变更

| 组件           | 之前 (s03)       | 之后 (s04)                    |
|----------------|------------------|-------------------------------|
| Tools          | 5                | 5 (基础) + task (仅父端)      |
| 上下文         | 单一共享         | 父 + 子隔离                   |
| Subagent       | 无               | `run_subagent()` 函数         |
| 返回值         | 不适用           | 仅摘要文本                    |

## 试一试

```sh
cd learn-ollama-code
uv run agents/s04_subagent.py
```

试试这些 prompt (英文 prompt 对 LLM 效果更好, 也可以用中文):

1. `Use a subtask to find what testing framework this project uses`
2. `Delegate: read all .py files and summarize what each one does`
3. `Use a task to create a new module, then verify it from here`
