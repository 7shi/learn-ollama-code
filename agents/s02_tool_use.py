#!/usr/bin/env python3
"""
s02_tool_use.py - Tools

The agent loop from s01 didn't change. We just added tools to the array
and a dispatch map to route calls.

    +----------+      +-------+      +------------------+
    |   User   | ---> |  LLM  | ---> | Tool Dispatch    |
    |  prompt  |      |       |      | {                |
    +----------+      +---+---+      |   bash: run_bash |
                          ^          |   read: run_read |
                          |          |   write: run_wr  |
                          +----------+   edit: run_edit |
                          tool_result| }                |
                                     +------------------+

Key insight: "The loop didn't change at all. I just added tools."
"""

import os
import subprocess
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

SYSTEM = f"You are a coding agent at {WORKDIR}. Use tools to solve tasks. Act, don't explain."


def safe_path(p: str) -> Path:
    path = (WORKDIR / p).resolve()
    if not path.is_relative_to(WORKDIR):
        raise ValueError(f"Path escapes workspace: {p}")
    return path


def bash(command: str) -> str:
    """
    Run a shell command.

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
        text = safe_path(path).read_text()
        lines = text.splitlines()
        if limit and limit < len(lines):
            lines = lines[:limit] + [f"... ({len(lines) - limit} more lines)"]
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
        return f"Wrote {len(content)} bytes to {path}"
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
        content = fp.read_text()
        if old_text not in content:
            return f"Error: Text not found in {path}"
        fp.write_text(content.replace(old_text, new_text, 1))
        return f"Edited {path}"
    except Exception as e:
        return f"Error: {e}"


# -- The dispatch map: {tool_name: handler} --
TOOL_HANDLERS = {
    "bash":       bash,
    "read_file":  read_file,
    "write_file": write_file,
    "edit_file":  edit_file,
}

TOOLS = [bash, read_file, write_file, edit_file]


def agent_loop(messages: list):
    while True:
        response = client.chat(
            model=MODEL, messages=messages, tools=TOOLS, think=THINK,
        )
        messages.append(response.message)
        if not response.message.tool_calls:
            return
        for tool in response.message.tool_calls:
            handler = TOOL_HANDLERS.get(tool.function.name)
            output = handler(**tool.function.arguments) if handler else f"Unknown tool: {tool.function.name}"
            print(f"> {tool.function.name}: {str(output)[:200]}")
            messages.append({"role": "tool", "content": str(output), "tool_name": tool.function.name})


if __name__ == "__main__":
    history = [{"role": "system", "content": SYSTEM}]
    while True:
        try:
            query = input("\033[36ms02 >> \033[0m")
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
