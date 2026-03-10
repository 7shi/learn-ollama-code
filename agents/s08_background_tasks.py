#!/usr/bin/env python3
"""
s08_background_tasks.py - Background Tasks

Run commands in background threads. A notification queue is drained
before each LLM call to deliver results.

    Main thread                Background thread
    +-----------------+        +-----------------+
    | agent loop      |        | task executes   |
    | ...             |        | ...             |
    | [LLM call] <---+------- | enqueue(result) |
    |  ^drain queue   |        +-----------------+
    +-----------------+

    Timeline:
    Agent ----[spawn A]----[spawn B]----[other work]----
                 |              |
                 v              v
              [A runs]      [B runs]        (parallel)
                 |              |
                 +-- notification queue --> [results injected]

Key insight: "Fire and forget -- the agent doesn't block while the command runs."
"""

import os
import subprocess
import threading
import uuid
try:
    import gnureadline as readline
except:
    import readline
from pathlib import Path

from dotenv import load_dotenv
from ollama import Client

load_dotenv(override=True)

WORKDIR = Path.cwd()
client = Client()
MODEL = os.environ["MODEL_ID"]
THINK = os.environ.get("THINK") == "1"

SYSTEM = f"You are a coding agent at {WORKDIR}. Use background_run for long-running commands."


# -- BackgroundManager: threaded execution + notification queue --
class BackgroundManager:
    def __init__(self):
        self.tasks = {}  # task_id -> {status, result, command}
        self._notification_queue = []  # completed task results
        self._lock = threading.Lock()

    def run(self, command: str) -> str:
        """Start a background thread, return task_id immediately."""
        task_id = str(uuid.uuid4())[:8]
        self.tasks[task_id] = {"status": "running", "result": None, "command": command}
        thread = threading.Thread(
            target=self._execute, args=(task_id, command), daemon=True
        )
        thread.start()
        return f"Background task {task_id} started: {command[:80]}"

    def _execute(self, task_id: str, command: str):
        """Thread target: run subprocess, capture output, push to queue."""
        try:
            r = subprocess.run(
                command, shell=True, cwd=WORKDIR,
                capture_output=True, text=True, timeout=300
            )
            output = (r.stdout + r.stderr).strip()[:50000]
            status = "completed"
        except subprocess.TimeoutExpired:
            output = "Error: Timeout (300s)"
            status = "timeout"
        except Exception as e:
            output = f"Error: {e}"
            status = "error"
        self.tasks[task_id]["status"] = status
        self.tasks[task_id]["result"] = output or "(no output)"
        with self._lock:
            self._notification_queue.append({
                "task_id": task_id,
                "status": status,
                "command": command[:80],
                "result": (output or "(no output)")[:500],
            })

    def check(self, task_id: str = None) -> str:
        """Check status of one task or list all."""
        if task_id:
            t = self.tasks.get(task_id)
            if not t:
                return f"Error: Unknown task {task_id}"
            return f"[{t['status']}] {t['command'][:60]}\n{t.get('result') or '(running)'}"
        lines = []
        for tid, t in self.tasks.items():
            lines.append(f"{tid}: [{t['status']}] {t['command'][:60]}")
        return "\n".join(lines) if lines else "No background tasks."

    def drain_notifications(self) -> list:
        """Return and clear all pending completion notifications."""
        with self._lock:
            notifs = list(self._notification_queue)
            self._notification_queue.clear()
        return notifs


BG = BackgroundManager()


# -- Tool implementations --
def safe_path(p: str) -> Path:
    path = (WORKDIR / p).resolve()
    if not path.is_relative_to(WORKDIR):
        raise ValueError(f"Path escapes workspace: {p}")
    return path

def bash(command: str) -> str:
    """
    Run a shell command (blocking).

    Args:
        command (str): The shell command to execute

    Returns:
        str: The output of the command
    """
    dangerous = ["rm -rf /", "sudo", "shutdown", "reboot", "> /dev/"]
    if any(d in command for d in dangerous):
        return "Error: Dangerous command blocked"
    try:
        r = subprocess.run(command, shell=True, cwd=WORKDIR,
                           capture_output=True, text=True, timeout=120)
        out = (r.stdout + r.stderr).strip()
        return out[:50000] if out else "(no output)"
    except subprocess.TimeoutExpired:
        return "Error: Timeout (120s)"


def read_file(path: str, limit: int = None) -> str:
    """
    Read file contents.

    Args:
        path (str): Path to the file relative to workspace
        limit (int): Maximum number of lines to return

    Returns:
        str: The file contents
    """
    try:
        lines = safe_path(path).read_text().splitlines()
        if limit and limit < len(lines):
            lines = lines[:limit] + [f"... ({len(lines) - limit} more)"]
        return "\n".join(lines)[:50000]
    except Exception as e:
        return f"Error: {e}"


def write_file(path: str, content: str) -> str:
    """
    Write content to file.

    Args:
        path (str): Path to the file relative to workspace
        content (str): Content to write

    Returns:
        str: Success or error message
    """
    try:
        fp = safe_path(path)
        fp.parent.mkdir(parents=True, exist_ok=True)
        fp.write_text(content)
        return f"Wrote {len(content)} bytes"
    except Exception as e:
        return f"Error: {e}"


def edit_file(path: str, old_text: str, new_text: str) -> str:
    """
    Replace exact text in file.

    Args:
        path (str): Path to the file relative to workspace
        old_text (str): Exact text to find and replace
        new_text (str): Replacement text

    Returns:
        str: Success or error message
    """
    try:
        fp = safe_path(path)
        c = fp.read_text()
        if old_text not in c:
            return f"Error: Text not found in {path}"
        fp.write_text(c.replace(old_text, new_text, 1))
        return f"Edited {path}"
    except Exception as e:
        return f"Error: {e}"


def background_run(command: str) -> str:
    """
    Run command in background thread. Returns task_id immediately.

    Args:
        command (str): The shell command to run in the background

    Returns:
        str: The background task ID
    """
    return BG.run(command)


def check_background(task_id: str = None) -> str:
    """
    Check background task status. Omit task_id to list all.

    Args:
        task_id (str): The background task ID to check

    Returns:
        str: Status and output of the task
    """
    return BG.check(task_id)


TOOL_HANDLERS = {
    "bash":             bash,
    "read_file":        read_file,
    "write_file":       write_file,
    "edit_file":        edit_file,
    "background_run":   background_run,
    "check_background": check_background,
}

TOOLS = [bash, read_file, write_file, edit_file, background_run, check_background]


def agent_loop(messages: list):
    while True:
        # Drain background notifications and inject as system message before LLM call
        notifs = BG.drain_notifications()
        if notifs and messages:
            notif_text = "\n".join(
                f"[bg:{n['task_id']}] {n['status']}: {n['result']}" for n in notifs
            )
            messages.append({"role": "user", "content": f"<background-results>\n{notif_text}\n</background-results>"})
            messages.append({"role": "assistant", "content": "Noted background results."})
        response = client.chat(
            model=MODEL, messages=messages, tools=TOOLS, think=THINK,
        )
        messages.append(response.message)
        if not response.message.tool_calls:
            return
        for tool in response.message.tool_calls:
            print(f"\033[33m{tool.function.name}{tool.function.arguments}\033[0m")
            handler = TOOL_HANDLERS.get(tool.function.name)
            try:
                output = handler(**tool.function.arguments) if handler else f"Unknown tool: {tool.function.name}"
            except Exception as e:
                output = f"Error: {e}"
            print(f"\033[33m{str(output)[:200]}\033[0m")
            messages.append({"role": "tool", "content": str(output), "tool_name": tool.function.name})


if __name__ == "__main__":
    history = [{"role": "system", "content": SYSTEM}]
    while True:
        try:
            query = input("\033[36ms08 >> \033[0m")
        except (EOFError, KeyboardInterrupt):
            break
        if query.strip().lower() in ("q", "exit", ""):
            break
        history.append({"role": "user", "content": query})
        agent_loop(history)
        last = history[-1]
        content = last.content if hasattr(last, "content") else last.get("content", "")
        if content:
            print(content)
        print()
