# s11: Autonomous Agents

`s01 > s02 > s03 > s04 > s05 > s06 | s07 > s08 > s09 > s10 > [ s11 ] s12`

> *"チームメイトが自らボードを見て、仕事を取る"* -- リーダーが逐一割り振る必要はない。

## 問題

s09-s10では、チームメイトは明示的に指示された時のみ作業する。リーダーは各チームメイトを特定のプロンプトでspawnしなければならない。タスクボードに未割り当てのタスクが10個あっても、リーダーが手動で各タスクを割り当てる。これはスケールしない。

真の自律性とは、チームメイトが自分で作業を見つけること: タスクボードをスキャンし、未確保のタスクを確保し、作業し、完了したら次を探す。

もう1つの問題: コンテキスト圧縮(s06)後にエージェントが自分の正体を忘れる可能性がある。アイデンティティ再注入がこれを解決する。

## 解決策

```
Teammate lifecycle with idle cycle:

+-------+
| spawn |
+---+---+
    |
    v
+-------+   tool_calls   +-------+
| WORK  | <------------- |  LLM  |
+---+---+                +-------+
    |
    | not response.message.tool_calls (or idle tool called)
    v
+--------+
|  IDLE  |  poll every 5s for up to 60s
+---+----+
    |
    +---> check inbox --> message? ----------> WORK
    |
    +---> scan .tasks/ --> unclaimed? -------> claim -> WORK
    |
    +---> 60s timeout ----------------------> SHUTDOWN

Identity re-injection after compression:
  if len(messages) <= 3:
    messages.insert(0, identity_block)
```

## 仕組み

1. チームメイトのループはWORKとIDLEの2フェーズ。LLMがツール呼び出しを止めた時(または`idle`ツールを呼んだ時)、IDLEフェーズに入る。

```python
def _loop(self, name, role, prompt):
    while True:
        # -- WORK PHASE --
        for _ in range(50):
            response = client.chat(
                model=MODEL, messages=messages, tools=tools, think=THINK,
            )
            messages.append(response.message)
            if not response.message.tool_calls:
                break
            for tool in response.message.tool_calls:
                if tool.function.name == "idle":
                    idle_requested = True
                else:
                    output = self._exec(name, tool.function.name, tool.function.arguments)
                messages.append({"role": "tool", "content": str(output), "tool_name": tool.function.name})
            if idle_requested:
                break

        # -- IDLE PHASE --
        self._set_status(name, "idle")
        # poll inbox and task board...
        if not resume:
            self._set_status(name, "shutdown")
            return
        self._set_status(name, "working")
```

2. IDLEフェーズがインボックスとタスクボードをポーリングする。

```python
# -- IDLE PHASE --
polls = IDLE_TIMEOUT // max(POLL_INTERVAL, 1)
for _ in range(polls):  # 60s / 5s = 12
    time.sleep(POLL_INTERVAL)
    inbox = BUS.read_inbox(name)
    if inbox:
        for msg in inbox:
            messages.append({"role": "user", "content": json.dumps(msg)})
        resume = True
        break
    unclaimed = scan_unclaimed_tasks()
    if unclaimed:
        task = unclaimed[0]
        _claim_task(task["id"], name)
        messages.append({"role": "user",
            "content": f"<auto-claimed>Task #{task['id']}: "
                       f"{task['subject']}</auto-claimed>"})
        resume = True
        break
```

3. タスクボードスキャン: pendingかつ未割り当てかつブロックされていないタスクを探す。

```python
def scan_unclaimed_tasks() -> list:
    unclaimed = []
    for f in sorted(TASKS_DIR.glob("task_*.json")):
        task = json.loads(f.read_text())
        if (task.get("status") == "pending"
                and not task.get("owner")
                and not task.get("blockedBy")):
            unclaimed.append(task)
    return unclaimed
```

4. アイデンティティ再注入: コンテキストが短すぎる(圧縮が起きた)場合にアイデンティティブロックを挿入する。

```python
if len(messages) <= 3:
    messages.insert(0, {"role": "user",
        "content": f"<identity>You are '{name}', role: {role}, "
                   f"team: {team_name}. Continue your work.</identity>"})
    messages.insert(1, {"role": "assistant",
        "content": f"I am {name}. Continuing."})
```

## s10からの変更点

| Component      | Before (s10)     | After (s11)                |
|----------------|------------------|----------------------------|
| Tools          | 12               | 14 (+idle, +claim_task)    |
| Autonomy       | Lead-directed    | Self-organizing            |
| Idle phase     | None             | Poll inbox + task board    |
| Task claiming  | Manual only      | Auto-claim unclaimed tasks |
| Identity       | System prompt    | + re-injection after compress|
| Timeout        | None             | 60s idle -> auto shutdown  |

## 試してみる

```sh
cd learn-ollama-code
uv run agents/s11_autonomous_agents.py
```

1. `Create 3 tasks on the board, then spawn alice and bob. Watch them auto-claim.`
2. `Spawn a coder teammate and let it find work from the task board itself`
3. `Create tasks with dependencies. Watch teammates respect the blocked order.`
4. `/tasks`と入力してオーナー付きのタスクボードを確認する
5. `/team`と入力して誰が作業中でアイドルかを監視する
