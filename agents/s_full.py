#!/usr/bin/env python3
"""
s_full.py - Full Reference Agent

Capstone implementation combining every mechanism from s01-s11.
Session s12 (task-aware worktree isolation) is taught separately.
NOT a teaching session -- this is the "put it all together" reference.

    +------------------------------------------------------------------+
    |                        FULL AGENT                                 |
    |                                                                   |
    |  System prompt (s05 skills, task-first + optional todo nag)      |
    |                                                                   |
    |  Before each LLM call:                                            |
    |  +--------------------+  +------------------+  +--------------+  |
    |  | Microcompact (s06) |  | Drain bg (s08)   |  | Check inbox  |  |
    |  | Auto-compact (s06) |  | notifications    |  | (s09)        |  |
    |  +--------------------+  +------------------+  +--------------+  |
    |                                                                   |
    |  Tool dispatch (s02 pattern):                                     |
    |  +--------+----------+----------+---------+-----------+          |
    |  | bash   | read     | write    | edit    | TodoWrite |          |
    |  | task   | load_sk  | compress | bg_run  | bg_check  |          |
    |  | t_crt  | t_get    | t_upd    | t_list  | spawn_tm  |          |
    |  | list_tm| send_msg | rd_inbox | bcast   | shutdown  |          |
    |  | plan   | idle     | claim    |         |           |          |
    |  +--------+----------+----------+---------+-----------+          |
    |                                                                   |
    |  Subagent (s04):  spawn -> work -> return summary                 |
    |  Teammate (s09):  spawn -> work -> idle -> auto-claim (s11)      |
    |  Shutdown (s10):  request_id handshake                            |
    |  Plan gate (s10): submit -> approve/reject                        |
    +------------------------------------------------------------------+

    REPL commands: /compact /tasks /team /inbox
"""

import json
import os
import re
import subprocess
import threading
import time
import uuid
try:
    import gnureadline as readline
except:
    import readline
from pathlib import Path
from queue import Queue

import colorama
from colorama import Fore, Style
from dotenv import load_dotenv
from ollama import Client

colorama.init()

load_dotenv(override=True)

WORKDIR = Path.cwd()
client = Client()
MODEL = os.environ["MODEL_ID"]
THINK = os.environ.get("THINK") == "1"

TEAM_DIR = WORKDIR / ".team"
INBOX_DIR = TEAM_DIR / "inbox"
TASKS_DIR = WORKDIR / ".tasks"
SKILLS_DIR = WORKDIR / "skills"
TRANSCRIPT_DIR = WORKDIR / ".transcripts"
TOKEN_THRESHOLD = 100000
POLL_INTERVAL = 5
IDLE_TIMEOUT = 60

VALID_MSG_TYPES = {"message", "broadcast", "shutdown_request",
                   "shutdown_response", "plan_approval_response"}


# === SECTION: base_tools ===
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
        confirm = input(f"Execute command? `{command}` [y/N]: ").strip().lower()
    except (EOFError, KeyboardInterrupt):
        return "Error: Command execution canceled by user"
    if confirm not in ("y", "yes"):
        return "Error: Command execution canceled by user"
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
        c = fp.read_text()
        if old_text not in c:
            return f"Error: Text not found in {path}"
        fp.write_text(c.replace(old_text, new_text, 1))
        return f"Edited {path}"
    except Exception as e:
        return f"Error: {e}"


# === SECTION: todos (s03) ===
class TodoManager:
    def __init__(self):
        self.items = []

    def update(self, items: list) -> str:
        validated, ip = [], 0
        for i, item in enumerate(items):
            content = str(item.get("content", "")).strip()
            status = str(item.get("status", "pending")).lower()
            af = str(item.get("activeForm", "")).strip()
            if not content: raise ValueError(f"Item {i}: content required")
            if status not in ("pending", "in_progress", "completed"):
                raise ValueError(f"Item {i}: invalid status '{status}'")
            if not af: raise ValueError(f"Item {i}: activeForm required")
            if status == "in_progress": ip += 1
            validated.append({"content": content, "status": status, "activeForm": af})
        if len(validated) > 20: raise ValueError("Max 20 todos")
        if ip > 1: raise ValueError("Only one in_progress allowed")
        self.items = validated
        return self.render()

    def render(self) -> str:
        if not self.items: return "No todos."
        lines = []
        for item in self.items:
            m = {"completed": "[x]", "in_progress": "[>]", "pending": "[ ]"}.get(item["status"], "[?]")
            suffix = f" <- {item['activeForm']}" if item["status"] == "in_progress" else ""
            lines.append(f"{m} {item['content']}{suffix}")
        done = sum(1 for t in self.items if t["status"] == "completed")
        lines.append(f"\n({done}/{len(self.items)} completed)")
        return "\n".join(lines)

    def has_open_items(self) -> bool:
        return any(item.get("status") != "completed" for item in self.items)


# === SECTION: subagent (s04) ===
def run_subagent(prompt: str, agent_type: str = "Explore") -> str:
    sub_tools = [bash, read_file]
    if agent_type != "Explore":
        sub_tools += [write_file, edit_file]
    sub_handlers = {
        "bash":       bash,
        "read_file":  read_file,
        "write_file": write_file,
        "edit_file":  edit_file,
    }
    sub_msgs = [
        {"role": "system", "content": f"You are a subagent at {WORKDIR}. Complete the task."},
        {"role": "user", "content": prompt},
    ]
    resp = None
    for _ in range(30):
        resp = client.chat(model=MODEL, messages=sub_msgs, tools=sub_tools, think=THINK)
        sub_msgs.append(resp.message)
        if not resp.message.tool_calls:
            break
        for tool in resp.message.tool_calls:
            h = sub_handlers.get(tool.function.name, lambda **kw: "Unknown tool")
            output = str(h(**tool.function.arguments))[:50000]
            sub_msgs.append({"role": "tool", "content": output, "tool_name": tool.function.name})
    if resp:
        return resp.message.content or "(no summary)"
    return "(subagent failed)"


# === SECTION: skills (s05) ===
class SkillLoader:
    def __init__(self, skills_dir: Path):
        self.skills = {}
        if skills_dir.exists():
            for f in sorted(skills_dir.rglob("SKILL.md")):
                text = f.read_text()
                match = re.match(r"^---\n(.*?)\n---\n(.*)", text, re.DOTALL)
                meta, body = {}, text
                if match:
                    for line in match.group(1).strip().splitlines():
                        if ":" in line:
                            k, v = line.split(":", 1)
                            meta[k.strip()] = v.strip()
                    body = match.group(2).strip()
                name = meta.get("name", f.parent.name)
                self.skills[name] = {"meta": meta, "body": body}

    def descriptions(self) -> str:
        if not self.skills: return "(no skills)"
        return "\n".join(f"  - {n}: {s['meta'].get('description', '-')}" for n, s in self.skills.items())

    def load(self, name: str) -> str:
        s = self.skills.get(name)
        if not s: return f"Error: Unknown skill '{name}'. Available: {', '.join(self.skills.keys())}"
        return f"<skill name=\"{name}\">\n{s['body']}\n</skill>"


# === SECTION: compression (s06) ===
def estimate_tokens(messages: list) -> int:
    return len(json.dumps(messages, default=str, ensure_ascii=False)) // 4

def microcompact(messages: list):
    tool_indices = [i for i, m in enumerate(messages)
                    if isinstance(m, dict) and m.get("role") == "tool"]
    if len(tool_indices) <= 3:
        return
    for i in tool_indices[:-3]:
        msg = messages[i]
        if isinstance(msg.get("content"), str) and len(msg["content"]) > 100:
            msg["content"] = f"[Previous: used {msg.get('tool_name', 'unknown')}]"

def auto_compact(messages: list) -> list:
    TRANSCRIPT_DIR.mkdir(exist_ok=True)
    path = TRANSCRIPT_DIR / f"transcript_{int(time.time())}.jsonl"
    with open(path, "w") as f:
        for msg in messages:
            f.write(json.dumps(msg, default=str, ensure_ascii=False) + "\n")
    conv_text = json.dumps(messages, default=str, ensure_ascii=False)[:80000]
    resp = client.chat(
        model=MODEL,
        messages=[{"role": "user", "content": f"Summarize for continuity:\n{conv_text}"}],
        think=THINK,
    )
    summary = resp.message.content
    return [
        {"role": "user", "content": f"[Compressed. Transcript: {path}]\n{summary}"},
        {"role": "assistant", "content": "Understood. Continuing with summary context."},
    ]


# === SECTION: file_tasks (s07) ===
class TaskManager:
    def __init__(self):
        TASKS_DIR.mkdir(exist_ok=True)

    def _next_id(self) -> int:
        ids = [int(f.stem.split("_")[1]) for f in TASKS_DIR.glob("task_*.json")]
        return max(ids, default=0) + 1

    def _load(self, tid: int) -> dict:
        p = TASKS_DIR / f"task_{tid}.json"
        if not p.exists(): raise ValueError(f"Task {tid} not found")
        return json.loads(p.read_text())

    def _save(self, task: dict):
        (TASKS_DIR / f"task_{task['id']}.json").write_text(json.dumps(task, indent=2, ensure_ascii=False))

    def create(self, subject: str, description: str = "") -> str:
        task = {"id": self._next_id(), "subject": subject, "description": description,
                "status": "pending", "owner": None, "blockedBy": [], "blocks": []}
        self._save(task)
        return json.dumps(task, indent=2, ensure_ascii=False)

    def get(self, tid: int) -> str:
        return json.dumps(self._load(tid), indent=2, ensure_ascii=False)

    def update(self, tid: int, status: str = None,
               add_blocked_by: list = None, add_blocks: list = None) -> str:
        task = self._load(tid)
        if status:
            task["status"] = status
            if status == "completed":
                for f in TASKS_DIR.glob("task_*.json"):
                    t = json.loads(f.read_text())
                    if tid in t.get("blockedBy", []):
                        t["blockedBy"].remove(tid)
                        self._save(t)
            if status == "deleted":
                (TASKS_DIR / f"task_{tid}.json").unlink(missing_ok=True)
                return f"Task {tid} deleted"
        if add_blocked_by:
            task["blockedBy"] = list(set(task["blockedBy"] + add_blocked_by))
        if add_blocks:
            task["blocks"] = list(set(task["blocks"] + add_blocks))
        self._save(task)
        return json.dumps(task, indent=2, ensure_ascii=False)

    def list_all(self) -> str:
        tasks = [json.loads(f.read_text()) for f in sorted(TASKS_DIR.glob("task_*.json"))]
        if not tasks: return "No tasks."
        lines = []
        for t in tasks:
            m = {"pending": "[ ]", "in_progress": "[>]", "completed": "[x]"}.get(t["status"], "[?]")
            owner = f" @{t['owner']}" if t.get("owner") else ""
            blocked = f" (blocked by: {t['blockedBy']})" if t.get("blockedBy") else ""
            lines.append(f"{m} #{t['id']}: {t['subject']}{owner}{blocked}")
        return "\n".join(lines)

    def claim(self, tid: int, owner: str) -> str:
        task = self._load(tid)
        task["owner"] = owner
        task["status"] = "in_progress"
        self._save(task)
        return f"Claimed task #{tid} for {owner}"


# === SECTION: background (s08) ===
class BackgroundManager:
    def __init__(self):
        self.tasks = {}
        self.notifications = Queue()

    def run(self, command: str, timeout: int = 120) -> str:
        tid = str(uuid.uuid4())[:8]
        self.tasks[tid] = {"status": "running", "command": command, "result": None}
        threading.Thread(target=self._exec, args=(tid, command, timeout), daemon=True).start()
        return f"Background task {tid} started: {command[:80]}"

    def _exec(self, tid: str, command: str, timeout: int):
        try:
            r = subprocess.run(command, shell=True, cwd=WORKDIR,
                               capture_output=True, text=True, timeout=timeout)
            output = (r.stdout + r.stderr).strip()[:50000]
            self.tasks[tid].update({"status": "completed", "result": output or "(no output)"})
        except Exception as e:
            self.tasks[tid].update({"status": "error", "result": str(e)})
        self.notifications.put({"task_id": tid, "status": self.tasks[tid]["status"],
                                "result": self.tasks[tid]["result"][:500]})

    def check(self, tid: str = None) -> str:
        if tid:
            t = self.tasks.get(tid)
            return f"[{t['status']}] {t.get('result', '(running)')}" if t else f"Unknown: {tid}"
        return "\n".join(f"{k}: [{v['status']}] {v['command'][:60]}" for k, v in self.tasks.items()) or "No bg tasks."

    def drain(self) -> list:
        notifs = []
        while not self.notifications.empty():
            notifs.append(self.notifications.get_nowait())
        return notifs


# === SECTION: messaging (s09) ===
class MessageBus:
    def __init__(self):
        INBOX_DIR.mkdir(parents=True, exist_ok=True)

    def send(self, sender: str, to: str, content: str,
             msg_type: str = "message", extra: dict = None) -> str:
        msg = {"type": msg_type, "from": sender, "content": content,
               "timestamp": time.time()}
        if extra: msg.update(extra)
        with open(INBOX_DIR / f"{to}.jsonl", "a") as f:
            f.write(json.dumps(msg, ensure_ascii=False) + "\n")
        return f"Sent {msg_type} to {to}"

    def read_inbox(self, name: str) -> list:
        path = INBOX_DIR / f"{name}.jsonl"
        if not path.exists(): return []
        msgs = [json.loads(l) for l in path.read_text().strip().splitlines() if l]
        path.write_text("")
        return msgs

    def broadcast(self, sender: str, content: str, names: list) -> str:
        count = 0
        for n in names:
            if n != sender:
                self.send(sender, n, content, "broadcast")
                count += 1
        return f"Broadcast to {count} teammates"


# === SECTION: shutdown + plan tracking (s10) ===
shutdown_requests = {}
plan_requests = {}


# === SECTION: team (s09/s11) ===
class TeammateManager:
    def __init__(self, bus: MessageBus, task_mgr: TaskManager):
        TEAM_DIR.mkdir(exist_ok=True)
        self.bus = bus
        self.task_mgr = task_mgr
        self.config_path = TEAM_DIR / "config.json"
        self.config = self._load()
        self.threads = {}

    def _load(self) -> dict:
        if self.config_path.exists():
            return json.loads(self.config_path.read_text())
        return {"team_name": "default", "members": []}

    def _save(self):
        self.config_path.write_text(json.dumps(self.config, indent=2, ensure_ascii=False))

    def _find(self, name: str) -> dict:
        for m in self.config["members"]:
            if m["name"] == name: return m
        return None

    def spawn(self, name: str, role: str, prompt: str) -> str:
        member = self._find(name)
        if member:
            if member["status"] not in ("idle", "shutdown"):
                return f"Error: '{name}' is currently {member['status']}"
            member["status"] = "working"
            member["role"] = role
        else:
            member = {"name": name, "role": role, "status": "working"}
            self.config["members"].append(member)
        self._save()
        threading.Thread(target=self._loop, args=(name, role, prompt), daemon=True).start()
        return f"Spawned '{name}' (role: {role})"

    def _set_status(self, name: str, status: str):
        member = self._find(name)
        if member:
            member["status"] = status
            self._save()

    def _loop(self, name: str, role: str, prompt: str):
        team_name = self.config["team_name"]
        sys_prompt = (f"You are '{name}', role: {role}, team: {team_name}, at {WORKDIR}. "
                      f"Use idle when done with current work. You may auto-claim tasks.")
        messages = [
            {"role": "system", "content": sys_prompt},
            {"role": "user", "content": prompt},
        ]
        tools = [bash, read_file, write_file, edit_file, SEND_MESSAGE_TOOL, idle, claim_task]
        while True:
            # -- WORK PHASE --
            for _ in range(50):
                inbox = self.bus.read_inbox(name)
                for msg in inbox:
                    if msg.get("type") == "shutdown_request":
                        self._set_status(name, "shutdown")
                        return
                    messages.append({"role": "user", "content": json.dumps(msg, ensure_ascii=False)})
                try:
                    response = client.chat(model=MODEL, messages=messages, tools=tools, think=THINK)
                except Exception:
                    self._set_status(name, "shutdown")
                    return
                messages.append(response.message)
                if not response.message.tool_calls:
                    break
                idle_requested = False
                for tool in response.message.tool_calls:
                    print(f"{Fore.YELLOW}[{name}] {tool.function.name}{tool.function.arguments}{Style.RESET_ALL}")
                    if tool.function.name == "idle":
                        idle_requested = True
                        output = "Entering idle phase."
                    elif tool.function.name == "claim_task":
                        output = self.task_mgr.claim(tool.function.arguments["task_id"], name)
                    elif tool.function.name == "send_message":
                        output = self.bus.send(name, tool.function.arguments["to"], tool.function.arguments["content"])
                    else:
                        dispatch = {"bash": bash, "read_file": read_file,
                                    "write_file": write_file, "edit_file": edit_file}
                        output = dispatch.get(tool.function.name, lambda **kw: "Unknown")(**tool.function.arguments)
                    print(f"{Fore.GREEN}{str(output)[:120]}{Style.RESET_ALL}")
                    messages.append({"role": "tool", "content": str(output), "tool_name": tool.function.name})
                if idle_requested:
                    break
            # -- IDLE PHASE: poll for messages and unclaimed tasks --
            self._set_status(name, "idle")
            resume = False
            for _ in range(IDLE_TIMEOUT // max(POLL_INTERVAL, 1)):
                time.sleep(POLL_INTERVAL)
                inbox = self.bus.read_inbox(name)
                if inbox:
                    for msg in inbox:
                        if msg.get("type") == "shutdown_request":
                            self._set_status(name, "shutdown")
                            return
                        messages.append({"role": "user", "content": json.dumps(msg, ensure_ascii=False)})
                    resume = True
                    break
                unclaimed = []
                for f in sorted(TASKS_DIR.glob("task_*.json")):
                    t = json.loads(f.read_text())
                    if t.get("status") == "pending" and not t.get("owner") and not t.get("blockedBy"):
                        unclaimed.append(t)
                if unclaimed:
                    task = unclaimed[0]
                    self.task_mgr.claim(task["id"], name)
                    # Identity re-injection for compressed contexts
                    if len(messages) <= 3:
                        messages.insert(0, {"role": "user", "content":
                            f"<identity>You are '{name}', role: {role}, team: {team_name}.</identity>"})
                        messages.insert(1, {"role": "assistant", "content": f"I am {name}. Continuing."})
                    messages.append({"role": "user", "content":
                        f"<auto-claimed>Task #{task['id']}: {task['subject']}\n{task.get('description', '')}</auto-claimed>"})
                    messages.append({"role": "assistant", "content": f"Claimed task #{task['id']}. Working on it."})
                    resume = True
                    break
            if not resume:
                self._set_status(name, "shutdown")
                return
            self._set_status(name, "working")

    def list_all(self) -> str:
        if not self.config["members"]: return "No teammates."
        lines = [f"Team: {self.config['team_name']}"]
        for m in self.config["members"]:
            lines.append(f"  {m['name']} ({m['role']}): {m['status']}")
        return "\n".join(lines)

    def member_names(self) -> list:
        return [m["name"] for m in self.config["members"]]


# === SECTION: global_instances ===
TODO = TodoManager()
SKILLS = SkillLoader(SKILLS_DIR)
TASK_MGR = TaskManager()
BG = BackgroundManager()
BUS = MessageBus()
TEAM = TeammateManager(BUS, TASK_MGR)

# === SECTION: system_prompt ===
SYSTEM = f"""You are a coding agent at {WORKDIR}. Use tools to solve tasks.
Prefer task_create/task_update/task_list for multi-step work. Use TodoWrite for short checklists.
Use task for subagent delegation. Use load_skill for specialized knowledge.
Skills: {SKILLS.descriptions()}"""


# === SECTION: tool_functions ===
def load_skill(name: str) -> str:
    """
    Load specialized knowledge by name.

    Args:
        name (str): The skill name to load

    Returns:
        str: The skill content
    """
    return SKILLS.load(name)

def compress(focus: str = ""):
    """Trigger manual conversation compression to free up context."""

def background_run(command: str, timeout: int = 120) -> str:
    """
    Run a shell command in a background thread.

    Args:
        command (str): The shell command to execute
        timeout (int): Timeout in seconds

    Returns:
        str: Background task ID and status
    """
    return BG.run(command, timeout)

def check_background(task_id: str = None) -> str:
    """
    Check background task status.

    Args:
        task_id (str): Specific task ID to check, or omit for all tasks

    Returns:
        str: Task status and result
    """
    return BG.check(task_id)

def task_create(subject: str, description: str = "") -> str:
    """
    Create a persistent file task.

    Args:
        subject (str): Short task title
        description (str): Detailed task description

    Returns:
        str: The created task as JSON
    """
    return TASK_MGR.create(subject, description)

def task_get(task_id: int) -> str:
    """
    Get task details by ID.

    Args:
        task_id (int): The task ID to retrieve

    Returns:
        str: Task details as JSON
    """
    return TASK_MGR.get(task_id)

def task_list() -> str:
    """
    List all tasks.

    Returns:
        str: Formatted list of all tasks
    """
    return TASK_MGR.list_all()

def spawn_teammate(name: str, role: str, prompt: str) -> str:
    """
    Spawn a persistent autonomous teammate.

    Args:
        name (str): Unique teammate name
        role (str): Role description
        prompt (str): Initial task prompt

    Returns:
        str: Confirmation message
    """
    return TEAM.spawn(name, role, prompt)

def list_teammates() -> str:
    """
    List all teammates and their status.

    Returns:
        str: Formatted team roster
    """
    return TEAM.list_all()

def read_inbox() -> str:
    """
    Read and drain the lead's inbox.

    Returns:
        str: Inbox messages as JSON
    """
    return json.dumps(BUS.read_inbox("lead"), indent=2, ensure_ascii=False)

def broadcast(content: str) -> str:
    """
    Send a message to all teammates.

    Args:
        content (str): Message to broadcast

    Returns:
        str: Confirmation with count
    """
    return BUS.broadcast("lead", content, TEAM.member_names())

def idle():
    """Enter idle state when there is no more work to do."""

def claim_task(task_id: int) -> str:
    """
    Claim a task from the board.

    Args:
        task_id (int): The task ID to claim

    Returns:
        str: Confirmation message
    """
    return TASK_MGR.claim(task_id, "lead")


# === SECTION: shutdown_protocol (s10) ===
def shutdown_request(teammate: str) -> str:
    """
    Request a teammate to shut down.

    Args:
        teammate (str): Name of the teammate to shut down

    Returns:
        str: Confirmation with request ID
    """
    req_id = str(uuid.uuid4())[:8]
    shutdown_requests[req_id] = {"target": teammate, "status": "pending"}
    BUS.send("lead", teammate, "Please shut down.", "shutdown_request", {"request_id": req_id})
    return f"Shutdown request {req_id} sent to '{teammate}'"

# === SECTION: plan_approval (s10) ===
def plan_approval(request_id: str, approve: bool, feedback: str = "") -> str:
    """
    Approve or reject a teammate's plan.

    Args:
        request_id (str): The plan request ID to respond to
        approve (bool): Whether to approve the plan
        feedback (str): Optional feedback message

    Returns:
        str: Confirmation of the approval decision
    """
    req = plan_requests.get(request_id)
    if not req: return f"Error: Unknown plan request_id '{request_id}'"
    req["status"] = "approved" if approve else "rejected"
    BUS.send("lead", req["from"], feedback, "plan_approval_response",
             {"request_id": request_id, "approve": approve, "feedback": feedback})
    return f"Plan {req['status']} for '{req['from']}'"


# === SECTION: tool_dispatch (s02) ===
TODO_WRITE_TOOL = {
    "type": "function",
    "function": {
        "name": "TodoWrite",
        "description": "Update task tracking list.",
        "parameters": {
            "type": "object",
            "properties": {
                "items": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "content":    {"type": "string"},
                            "status":     {"type": "string", "enum": ["pending", "in_progress", "completed"]},
                            "activeForm": {"type": "string"},
                        },
                        "required": ["content", "status", "activeForm"],
                    },
                },
            },
            "required": ["items"],
        },
    },
}

TASK_TOOL = {
    "type": "function",
    "function": {
        "name": "task",
        "description": "Spawn a subagent for isolated exploration or work.",
        "parameters": {
            "type": "object",
            "properties": {
                "prompt":     {"type": "string"},
                "agent_type": {"type": "string", "enum": ["Explore", "general-purpose"]},
            },
            "required": ["prompt"],
        },
    },
}

TASK_UPDATE_TOOL = {
    "type": "function",
    "function": {
        "name": "task_update",
        "description": "Update task status or dependencies.",
        "parameters": {
            "type": "object",
            "properties": {
                "task_id":       {"type": "integer"},
                "status":        {"type": "string", "enum": ["pending", "in_progress", "completed", "deleted"]},
                "add_blocked_by": {"type": "array", "items": {"type": "integer"}},
                "add_blocks":    {"type": "array", "items": {"type": "integer"}},
            },
            "required": ["task_id"],
        },
    },
}

SEND_MESSAGE_TOOL = {
    "type": "function",
    "function": {
        "name": "send_message",
        "description": "Send a message to a teammate.",
        "parameters": {
            "type": "object",
            "properties": {
                "to":       {"type": "string"},
                "content":  {"type": "string"},
                "msg_type": {"type": "string", "enum": list(VALID_MSG_TYPES)},
            },
            "required": ["to", "content"],
        },
    },
}

TOOL_HANDLERS = {
    "bash":             bash,
    "read_file":        read_file,
    "write_file":       write_file,
    "edit_file":        edit_file,
    "TodoWrite":        lambda **kw: TODO.update(kw["items"]),
    "task":             lambda **kw: run_subagent(kw["prompt"], kw.get("agent_type", "Explore")),
    "load_skill":       load_skill,
    "compress":         lambda **kw: "Compressing...",
    "background_run":   background_run,
    "check_background": check_background,
    "task_create":      task_create,
    "task_get":         task_get,
    "task_update":      lambda **kw: TASK_MGR.update(kw["task_id"], kw.get("status"), kw.get("add_blocked_by"), kw.get("add_blocks")),
    "task_list":        task_list,
    "spawn_teammate":   spawn_teammate,
    "list_teammates":   list_teammates,
    "send_message":     lambda **kw: BUS.send("lead", kw["to"], kw["content"], kw.get("msg_type", "message")),
    "read_inbox":       read_inbox,
    "broadcast":        broadcast,
    "shutdown_request": shutdown_request,
    "plan_approval":    plan_approval,
    "idle":             lambda **kw: "Lead does not idle.",
    "claim_task":       claim_task,
}

TOOLS = [
    bash, read_file, write_file, edit_file,
    TODO_WRITE_TOOL,
    TASK_TOOL,
    load_skill, compress, background_run, check_background,
    task_create, task_get, TASK_UPDATE_TOOL, task_list,
    spawn_teammate, list_teammates, SEND_MESSAGE_TOOL, read_inbox, broadcast,
    shutdown_request, plan_approval, idle, claim_task,
]


# === SECTION: agent_loop ===
def agent_loop(messages: list):
    rounds_without_todo = 0
    while True:
        # s06: compression pipeline
        microcompact(messages)
        if estimate_tokens(messages) > TOKEN_THRESHOLD:
            print("[auto-compact triggered]")
            messages[:] = auto_compact(messages)
        # s08: drain background notifications
        notifs = BG.drain()
        if notifs:
            txt = "\n".join(f"[bg:{n['task_id']}] {n['status']}: {n['result']}" for n in notifs)
            messages.append({"role": "user", "content": f"<background-results>\n{txt}\n</background-results>"})
            messages.append({"role": "assistant", "content": "Noted background results."})
        # s10: check lead inbox
        inbox = BUS.read_inbox("lead")
        if inbox:
            messages.append({"role": "user", "content": f"<inbox>{json.dumps(inbox, indent=2, ensure_ascii=False)}</inbox>"})
            messages.append({"role": "assistant", "content": "Noted inbox messages."})
        # LLM call
        response = client.chat(model=MODEL, messages=messages, tools=TOOLS, think=THINK)
        messages.append(response.message)
        if not response.message.tool_calls:
            return
        # Tool execution
        used_todo = False
        manual_compress = False
        for tool in response.message.tool_calls:
            print(f"{Fore.YELLOW}{tool.function.name}{tool.function.arguments}{Style.RESET_ALL}")
            if tool.function.name == "compress":
                manual_compress = True
            handler = TOOL_HANDLERS.get(tool.function.name)
            try:
                output = handler(**tool.function.arguments) if handler else f"Unknown tool: {tool.function.name}"
            except Exception as e:
                output = f"Error: {e}"
            print(f"{Fore.GREEN}{str(output)[:200]}{Style.RESET_ALL}")
            messages.append({"role": "tool", "content": str(output), "tool_name": tool.function.name})
            if tool.function.name == "TodoWrite":
                used_todo = True
        # s03: nag reminder (only when todo workflow is active)
        rounds_without_todo = 0 if used_todo else rounds_without_todo + 1
        if TODO.has_open_items() and rounds_without_todo >= 3:
            messages.append({"role": "user", "content": "<reminder>Update your todos.</reminder>"})
            messages.append({"role": "assistant", "content": "Noted. I'll update my todos."})
        # s06: manual compress
        if manual_compress:
            print("[manual compact]")
            messages[:] = auto_compact(messages)


# === SECTION: repl ===
if __name__ == "__main__":
    history = [{"role": "system", "content": SYSTEM}]
    while True:
        try:
            query = input("s_full >> ")
        except (EOFError, KeyboardInterrupt):
            break
        if query.strip().lower() in ("q", "exit", ""):
            break
        if query.strip() == "/compact":
            if history:
                print("[manual compact via /compact]")
                history[:] = auto_compact(history)
            continue
        if query.strip() == "/tasks":
            print(TASK_MGR.list_all())
            continue
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
