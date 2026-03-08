# s11: Autonomous Agents

`s01 > s02 > s03 > s04 > s05 > s06 | s07 > s08 > s09 > s10 > [ s11 ] s12`

> *"Teammates scan the board and claim tasks themselves"* -- no need for the lead to assign each one.

## Problem

In s09-s10, teammates only work when explicitly told to. The lead must spawn each one with a specific prompt. 10 unclaimed tasks on the board? The lead assigns each one manually. Doesn't scale.

True autonomy: teammates scan the task board themselves, claim unclaimed tasks, work on them, then look for more.

One subtlety: after context compression (s06), the agent might forget who it is. Identity re-injection fixes this.

## Solution

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

## How It Works

1. The teammate loop has two phases: WORK and IDLE. When the LLM stops calling tools (or calls `idle`), the teammate enters IDLE.

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

2. The idle phase polls inbox and task board in a loop.

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
        claim_task(task["id"], name)
        messages.append({"role": "user",
            "content": f"<auto-claimed>Task #{task['id']}: "
                       f"{task['subject']}</auto-claimed>"})
        resume = True
        break
```

3. Task board scanning: find pending, unowned, unblocked tasks.

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

4. Identity re-injection: when context is too short (compression happened), insert an identity block.

```python
if len(messages) <= 3:
    messages.insert(0, {"role": "user",
        "content": f"<identity>You are '{name}', role: {role}, "
                   f"team: {team_name}. Continue your work.</identity>"})
    messages.insert(1, {"role": "assistant",
        "content": f"I am {name}. Continuing."})
```

## What Changed From s10

| Component      | Before (s10)     | After (s11)                |
|----------------|------------------|----------------------------|
| Tools          | 12               | 14 (+idle, +claim_task)    |
| Autonomy       | Lead-directed    | Self-organizing            |
| Idle phase     | None             | Poll inbox + task board    |
| Task claiming  | Manual only      | Auto-claim unclaimed tasks |
| Identity       | System prompt    | + re-injection after compress|
| Timeout        | None             | 60s idle -> auto shutdown  |

## Try It

```sh
cd learn-ollama-code
uv run agents/s11_autonomous_agents.py
```

1. `Create 3 tasks on the board, then spawn alice and bob. Watch them auto-claim.`
2. `Spawn a coder teammate and let it find work from the task board itself`
3. `Create tasks with dependencies. Watch teammates respect the blocked order.`
4. Type `/tasks` to see the task board with owners
5. Type `/team` to monitor who is working vs idle
