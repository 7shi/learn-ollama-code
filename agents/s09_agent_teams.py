#!/usr/bin/env python3
"""
s09_agent_teams.py - Agent Teams

Persistent named agents with file-based JSONL inboxes. Each teammate runs
its own agent loop in a separate thread. Communication via append-only inboxes.

    Subagent (s04):  spawn -> execute -> return summary -> destroyed
    Teammate (s09):  spawn -> work -> idle -> work -> ... -> shutdown

    .team/config.json                   .team/inbox/
    +----------------------------+      +------------------+
    | {"team_name": "default",   |      | alice.jsonl      |
    |  "members": [              |      | bob.jsonl        |
    |    {"name":"alice",        |      | lead.jsonl       |
    |     "role":"coder",        |      +------------------+
    |     "status":"idle"}       |
    |  ]}                        |      send_message("alice", "fix bug"):
    +----------------------------+        open("alice.jsonl", "a").write(msg)

                                        read_inbox("alice"):
    spawn_teammate("alice","coder",...)   msgs = [json.loads(l) for l in ...]
         |                                open("alice.jsonl", "w").close()
         v                                return msgs  # drain
    Thread: alice             Thread: bob
    +------------------+      +------------------+
    | agent_loop       |      | agent_loop       |
    | status: working  |      | status: idle     |
    | ... runs tools   |      | ... waits ...    |
    | status -> idle   |      |                  |
    +------------------+      +------------------+

    5 message types (all declared, not all handled here):
    +-------------------------+-----------------------------------+
    | message                 | Normal text message               |
    | broadcast               | Sent to all teammates             |
    | shutdown_request        | Request graceful shutdown (s10)   |
    | shutdown_response       | Approve/reject shutdown (s10)     |
    | plan_approval_response  | Approve/reject plan (s10)         |
    +-------------------------+-----------------------------------+

Key insight: "Teammates that can talk to each other."
"""

import json
import os
import subprocess
import threading
import time
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
TEAM_DIR = WORKDIR / ".team"
INBOX_DIR = TEAM_DIR / "inbox"

SYSTEM = f"You are a team lead at {WORKDIR}. Spawn teammates and communicate via inboxes."

VALID_MSG_TYPES = {
    "message",
    "broadcast",
    "shutdown_request",
    "shutdown_response",
    "plan_approval_response",
}


# -- MessageBus: JSONL inbox per teammate --
class MessageBus:
    def __init__(self, inbox_dir: Path):
        self.dir = inbox_dir
        self.dir.mkdir(parents=True, exist_ok=True)

    def send(self, sender: str, to: str, content: str,
             msg_type: str = "message", extra: dict = None) -> str:
        if msg_type not in VALID_MSG_TYPES:
            return f"Error: Invalid type '{msg_type}'. Valid: {VALID_MSG_TYPES}"
        msg = {
            "type": msg_type,
            "from": sender,
            "content": content,
            "timestamp": time.time(),
        }
        if extra:
            msg.update(extra)
        inbox_path = self.dir / f"{to}.jsonl"
        with open(inbox_path, "a") as f:
            f.write(json.dumps(msg, ensure_ascii=False) + "\n")
        return f"Sent {msg_type} to {to}"

    def read_inbox(self, name: str) -> list:
        inbox_path = self.dir / f"{name}.jsonl"
        if not inbox_path.exists():
            return []
        messages = []
        for line in inbox_path.read_text().strip().splitlines():
            if line:
                messages.append(json.loads(line))
        inbox_path.write_text("")
        return messages

    def broadcast(self, sender: str, content: str, teammates: list) -> str:
        count = 0
        for name in teammates:
            if name != sender:
                self.send(sender, name, content, "broadcast")
                count += 1
        return f"Broadcast to {count} teammates"


BUS = MessageBus(INBOX_DIR)


# -- TeammateManager: persistent named agents with config.json --
class TeammateManager:
    def __init__(self, team_dir: Path):
        self.dir = team_dir
        self.dir.mkdir(exist_ok=True)
        self.config_path = self.dir / "config.json"
        self.config = self._load_config()
        self.threads = {}

    def _load_config(self) -> dict:
        if self.config_path.exists():
            return json.loads(self.config_path.read_text())
        return {"team_name": "default", "members": []}

    def _save_config(self):
        self.config_path.write_text(json.dumps(self.config, indent=2, ensure_ascii=False))

    def _find_member(self, name: str) -> dict:
        for m in self.config["members"]:
            if m["name"] == name:
                return m
        return None

    def spawn(self, name: str, role: str, prompt: str) -> str:
        member = self._find_member(name)
        if member:
            if member["status"] not in ("idle", "shutdown"):
                return f"Error: '{name}' is currently {member['status']}"
            member["status"] = "working"
            member["role"] = role
        else:
            member = {"name": name, "role": role, "status": "working"}
            self.config["members"].append(member)
        self._save_config()
        thread = threading.Thread(
            target=self._teammate_loop,
            args=(name, role, prompt),
            daemon=True,
        )
        self.threads[name] = thread
        thread.start()
        return f"Spawned '{name}' (role: {role})"

    def _teammate_loop(self, name: str, role: str, prompt: str):
        sys_prompt = (
            f"You are '{name}', role: {role}, at {WORKDIR}. "
            f"Use send_message to communicate. Complete your task."
        )
        messages = [
            {"role": "system", "content": sys_prompt},
            {"role": "user", "content": prompt},
        ]
        tools = self._teammate_tools()
        for _ in range(50):
            inbox = BUS.read_inbox(name)
            for msg in inbox:
                messages.append({"role": "user", "content": json.dumps(msg, ensure_ascii=False)})
            try:
                response = client.chat(
                    model=MODEL, messages=messages, tools=tools, think=THINK,
                )
            except Exception:
                break
            messages.append(response.message)
            if not response.message.tool_calls:
                break
            for tool in response.message.tool_calls:
                print(f"\033[33m[{name}] {tool.function.name}{tool.function.arguments}\033[0m")
                output = self._exec(name, tool.function.name, tool.function.arguments)
                print(f"\033[33m{str(output)[:120]}\033[0m")
                messages.append({"role": "tool", "content": str(output), "tool_name": tool.function.name})
        member = self._find_member(name)
        if member and member["status"] != "shutdown":
            member["status"] = "idle"
            self._save_config()

    def _exec(self, sender: str, tool_name: str, args: dict) -> str:
        # these base tools are unchanged from s02
        if tool_name == "bash":
            return bash(args["command"])
        if tool_name == "read_file":
            return read_file(args["path"])
        if tool_name == "write_file":
            return write_file(args["path"], args["content"])
        if tool_name == "edit_file":
            return edit_file(args["path"], args["old_text"], args["new_text"])
        if tool_name == "send_message":
            return BUS.send(sender, args["to"], args["content"], args.get("msg_type", "message"))
        if tool_name == "read_inbox":
            return json.dumps(BUS.read_inbox(sender), indent=2, ensure_ascii=False)
        return f"Unknown tool: {tool_name}"

    def _teammate_tools(self) -> list:
        return [bash, read_file, write_file, edit_file, SEND_MESSAGE_TOOL, read_inbox]

    def list_all(self) -> str:
        if not self.config["members"]:
            return "No teammates."
        lines = [f"Team: {self.config['team_name']}"]
        for m in self.config["members"]:
            lines.append(f"  {m['name']} ({m['role']}): {m['status']}")
        return "\n".join(lines)

    def member_names(self) -> list:
        return [m["name"] for m in self.config["members"]]


TEAM = TeammateManager(TEAM_DIR)


# -- Base tool implementations (these base tools are unchanged from s02) --
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
    dangerous = ["rm -rf /", "sudo", "shutdown", "reboot"]
    if any(d in command for d in dangerous):
        return "Error: Dangerous command blocked"
    try:
        r = subprocess.run(
            command, shell=True, cwd=WORKDIR,
            capture_output=True, text=True, timeout=120,
        )
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


def read_inbox() -> str:
    """
    Read and drain your inbox.

    Returns:
        str: Inbox messages as JSON
    """
    return json.dumps(BUS.read_inbox("lead"), indent=2, ensure_ascii=False)


# send_message has msg_type enum that can't be expressed with type hints alone
SEND_MESSAGE_TOOL = {"type": "function", "function": {"name": "send_message",
    "description": "Send a message to a teammate's inbox.",
    "parameters": {"type": "object", "properties": {
        "to":       {"type": "string"},
        "content":  {"type": "string"},
        "msg_type": {"type": "string", "enum": list(VALID_MSG_TYPES)},
    }, "required": ["to", "content"]}}}


def spawn_teammate(name: str, role: str, prompt: str) -> str:
    """
    Spawn a persistent teammate that runs in its own thread.

    Args:
        name (str): Unique name for the teammate
        role (str): Role description for the teammate
        prompt (str): Initial task prompt for the teammate

    Returns:
        str: Confirmation message
    """
    return TEAM.spawn(name, role, prompt)


def list_teammates() -> str:
    """
    List all teammates with name, role, status.

    Returns:
        str: Team roster
    """
    return TEAM.list_all()


def broadcast(content: str) -> str:
    """
    Send a message to all teammates.

    Args:
        content (str): Message to broadcast

    Returns:
        str: Confirmation message
    """
    return BUS.broadcast("lead", content, TEAM.member_names())


# -- Lead tool dispatch (9 tools) --
TOOL_HANDLERS = {
    "bash":            bash,
    "read_file":       read_file,
    "write_file":      write_file,
    "edit_file":       edit_file,
    "spawn_teammate":  spawn_teammate,
    "list_teammates":  list_teammates,
    "send_message":    lambda **kw: BUS.send("lead", kw["to"], kw["content"], kw.get("msg_type", "message")),
    "read_inbox":      read_inbox,
    "broadcast":       broadcast,
}

TOOLS = [bash, read_file, write_file, edit_file,
         spawn_teammate, list_teammates, SEND_MESSAGE_TOOL, read_inbox, broadcast]


def agent_loop(messages: list):
    while True:
        inbox = BUS.read_inbox("lead")
        if inbox:
            messages.append({
                "role": "user",
                "content": f"<inbox>{json.dumps(inbox, indent=2, ensure_ascii=False)}</inbox>",
            })
            messages.append({
                "role": "assistant",
                "content": "Noted inbox messages.",
            })
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
            query = input("\033[36ms09 >> \033[0m")
        except (EOFError, KeyboardInterrupt):
            break
        if query.strip().lower() in ("q", "exit", ""):
            break
        if query.strip() == "/team":
            print(TEAM.list_all())
            continue
        if query.strip() == "/inbox":
            print(json.dumps(BUS.read_inbox("lead"), indent=2, ensure_ascii=False))
            continue
        history.append({"role": "user", "content": query})
        agent_loop(history)
        last = history[-1]
        content = last.content if hasattr(last, "content") else last.get("content", "")
        if content:
            print(content)
        print()
