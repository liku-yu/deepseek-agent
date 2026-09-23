#!/usr/bin/env python3
"""
Minimal DeepSeek agent.

- Uses the DeepSeek Responses API: POST /v1/responses  (streaming)
- Model: deepseek-v4-flash-vision-exp, reasoning effort: high
- Has ONE tool: `bash` (runs a shell command in Git Bash)
- Managed by uv:  uv run python agent.py

Usage:
    uv run python agent.py            # start interactive TUI (prompt_toolkit + Rich)
    uv run python agent.py --plain    # start plain interactive REPL
    uv run python agent.py --one "..." # single-shot (streaming to stdout)
    /exit / /quit / Ctrl+D            # leave the session
"""

from __future__ import annotations

import contextlib
import json
import os
import re
import shutil
import signal
import subprocess
import sys
import threading
import time
from typing import Callable, Optional

import httpx


def _env_int(name: str, default: int) -> int:
    """Read an int env var, falling back to `default` if missing/invalid."""
    raw = (os.environ.get(name) or "").strip()
    if not raw:
        return default
    try:
        return int(raw)
    except ValueError:
        return default


# ---------------------------------------------------------------- config
# Every value can be overridden with an env var (helpful if the experimental
# model is not enabled on your account). Defaults match the upstream repo.
API_BASE = os.environ.get("DEEPSEEK_API_BASE", "https://api.deepseek.com/v1")
MODEL = os.environ.get("DEEPSEEK_MODEL", "deepseek-v4-flash-vision-exp")
EFFORT = os.environ.get("DEEPSEEK_EFFORT", "high")   # thinking effort: high
CMD_TIMEOUT = _env_int("DEEPSEEK_CMD_TIMEOUT", 120)  # seconds per shell command
MAX_RETRIES = _env_int("DEEPSEEK_MAX_RETRIES", 3)    # retries for transient API errors
MAX_CONTEXT_CHARS = _env_int("DEEPSEEK_MAX_CONTEXT_CHARS", 200_000)  # rough budget before trimming

# HTTP statuses worth retrying
_TRANSIENT_STATUS = {408, 409, 429, 500, 502, 503, 504}
# --- safety policy (heuristic, defence-in-depth; NOT a sandbox) ---
_NET_CMDS = {"curl", "wget", "nc", "ncat", "netcat", "ssh", "scp", "sftp",
             "rsync", "telnet", "ftp"}
_DESTRUCTIVE_RE = re.compile("|".join([
    r"\bmkfs\b", r"\bdd\s+if=", r":\(\)\s*\{",
    r"\b(shutdown|reboot|halt|poweroff)\b", r"\bformat\s+[a-z]:",
    r"\bdel\s+/[sfq]\b", r"remove-item[^\n]*-recurse",
    r"\bchmod\s+-r\s+777\s+/", r"\bshred\b", r"\btruncate\s+-s\s*0",
    r"\bfind\b[^\n]*\s-delete\b", r"\bgit\s+clean\b[^\n]*-[a-z]*[dfx]",
    r">\s*/etc/", r">\s*/dev/sd",
]), re.I)
_SENSITIVE_RE = re.compile(
    r"(^|[\s=<>@/\\\"'`])(\.env(\.[\w.-]+)?|\.ssh/|id_rsa|id_ed25519|"
    r"\.aws/credentials|\.git-credentials|\.netrc|\.npmrc|\.pypirc|"
    r"credentials(\.json)?)(?=$|[\s\"'`);|&><])", re.I)
# Secrets are stripped from the bash tool's environment (anti-exfiltration)
_ENV_SECRET_RE = re.compile(r"(KEY|TOKEN|SECRET|PASSWORD|CREDENTIAL|PASSWD)", re.I)


def _normalize(command: str) -> str:
    """Lowercase + drop quotes/backslashes + collapse whitespace for matching."""
    s = re.sub(r"[\"'`\\]", "", command)
    return re.sub(r"\s+", " ", s).lower()


def _rm_recursive_force(s: str) -> bool:
    for seg in re.split(r"[;&|]+", s):
        toks = seg.split()
        if not toks or "rm" not in toks[0]:
            continue
        flags = " ".join(t for t in toks[1:] if t.startswith("-"))
        if re.search(r"(^|\s)-[a-z]*r|--recursive", flags) and \
           re.search(r"(^|\s)-[a-z]*f|--force", flags):
            return True
    return False


def _has_network_egress(s: str) -> bool:
    for seg in re.split(r"[;&|]+", s):
        toks = seg.split()
        if toks and toks[0] in _NET_CMDS:
            return True
    return False


def classify_command(command: str) -> list[str]:
    """Risk categories for a shell command (empty list = allow without asking)."""
    if not command:
        return []
    s = _normalize(command)
    reasons = []
    if _rm_recursive_force(s) or _DESTRUCTIVE_RE.search(s):
        reasons.append("destructive")
    if _SENSITIVE_RE.search(command):
        reasons.append("sensitive-file")
    if _has_network_egress(s):
        reasons.append("network-egress")
    return reasons


def is_dangerous(command: str) -> bool:
    return bool(classify_command(command))


# --- cancellation ---
_cancel = threading.Event()


class AgentError(RuntimeError):
    """The API reported a failure inside the stream (response.failed/error)."""


class CancelledError(RuntimeError):
    """The user cancelled the current turn."""


def request_cancel() -> None:
    _cancel.set()


def clear_cancel() -> None:
    _cancel.clear()


@contextlib.contextmanager
def cancellable():
    """While active, Ctrl+C requests cancellation instead of raising."""
    clear_cancel()
    old = None
    if threading.current_thread() is threading.main_thread():
        try:
            old = signal.getsignal(signal.SIGINT)
            signal.signal(signal.SIGINT, lambda *_: request_cancel())
        except Exception:
            old = None
    try:
        yield
    finally:
        if old is not None:
            try:
                signal.signal(signal.SIGINT, old)
            except Exception:
                pass
        clear_cancel()

SYSTEM = (
    "You are a minimal command-line agent operated inside Git Bash. "
    "You have one tool, `bash`, to run shell commands on the user's machine. "
    "Use it freely to inspect files, run commands, and solve the user's request. "
    "Be concise. Never invent command output; always run the tool to get real results."
)

TOOLS = [
    {
        "type": "function",
        "name": "bash",
        "description": "Run a shell command in Git Bash and return its stdout, stderr and exit code.",
        "parameters": {
            "type": "object",
            "properties": {
                "command": {
                    "type": "string",
                    "description": "The shell command to run.",
                }
            },
            "required": ["command"],
        },
    }
]

# An emit callback receives (event, payload) as the model streams.
#   event == "reasoning"        payload: {"text": delta}
#   event == "reasoning_done"   payload: {}
#   event == "text"             payload: {"text": delta}
#   event == "text_done"        payload: {}
#   event == "tool"             payload: {"command", "result", "status"}
#   event == "done"             payload: {}
#   event == "error"            payload: {"message"}
Emit = Callable[[str, dict], None]
Confirm = Callable[[str], bool]


# ---------------------------------------------------------------- helpers
def _set_utf8_io() -> None:
    """Prefer UTF-8 for stdin/stdout/stderr on Windows.

    The model returns UTF-8 text; on a GBK/CP936 Windows console the default
    stream encoding would garble it (or crash on non-GBK chars) and reading
    piped UTF-8 stdin would produce surrogate code points that break JSON
    encoding. This reconfigures stdout/stderr to UTF-8 always, and stdin to
    UTF-8 when it is a pipe (not an interactive console).
    """
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is not None:
            try:
                reconfigure(encoding="utf-8", errors="replace")
            except Exception:
                pass
    try:
        if not sys.stdin.isatty():
            sys.stdin.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass


def read_stdin_text() -> str:
    """Read piped stdin as UTF-8 text (avoids GBK surrogate corruption)."""
    try:
        return sys.stdin.buffer.read().decode("utf-8", errors="replace").strip()
    except Exception:
        try:
            return sys.stdin.read().strip()
        except Exception:
            return ""


def _key() -> str:
    """Read the DeepSeek API key at runtime from the DEEPSEEK_API_KEY env var.

    Falls back to a .env file next to the script / executable. Raises if not set,
    so the key is never baked into the binary.
    """
    env = os.environ.get("DEEPSEEK_API_KEY", "").strip()
    if env:
        return env

    # When frozen (PyInstaller onefile), __file__ is the temp extraction dir,
    # so also look for .env next to the actual executable.
    base = os.path.dirname(os.path.abspath(sys.executable)) if getattr(sys, "frozen", False) else \
        os.path.dirname(os.path.abspath(__file__))
    for env_path in (os.path.join(base, ".env"), os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env")):
        if os.path.exists(env_path):
            for line in open(env_path, encoding="utf-8"):
                line = line.strip()
                if line and not line.startswith("#"):
                    k, _, v = line.partition("=")
                    if k.strip() == "DEEPSEEK_API_KEY":
                        v = v.strip().strip('"').strip("'")
                        if v:
                            return v
    raise RuntimeError(
        "DEEPSEEK_API_KEY is not set. Provide it via the environment variable "
        "(e.g. `export DEEPSEEK_API_KEY=sk-...`) or a .env file containing "
        "DEEPSEEK_API_KEY=sk-..."
    )


def find_bash() -> str:
    """Locate a bash executable (Git Bash on Windows, /usr/bin/bash otherwise)."""
    p = shutil.which("bash")
    if p:
        return p
    for candidate in (
        "C:/Program Files/Git/bin/bash.exe",
        "C:/Program Files/Git/usr/bin/bash.exe",
        "/usr/bin/bash",
        "/bin/bash",
    ):
        if os.path.exists(candidate):
            return candidate
    return "bash"


def _kill_tree(proc: "subprocess.Popen") -> None:
    """Kill a process and all of its descendants."""
    try:
        if os.name == "nt":
            subprocess.run(["taskkill", "/F", "/T", "/PID", str(proc.pid)],
                           capture_output=True)
        else:
            os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
    except Exception:
        try:
            proc.kill()
        except Exception:
            pass


def run_command(command: str) -> str:
    """Run a command in Git Bash. Return a compact text report.

    bash runs in its own process group, so a timeout or cancel kills the whole
    process tree (not just bash). Polls for cancellation every 0.2s.
    """
    bash = find_bash()
    popen_kwargs: dict = dict(
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
        errors="replace",
        cwd=os.getcwd(),
        env=_child_env(),
    )
    if os.name == "nt":
        popen_kwargs["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP
    else:
        popen_kwargs["start_new_session"] = True

    proc = subprocess.Popen([bash, "-lc", command], **popen_kwargs)
    deadline = time.monotonic() + CMD_TIMEOUT
    try:
        while True:
            try:
                out, err = proc.communicate(timeout=0.2)
                code = proc.returncode
                break
            except subprocess.TimeoutExpired:
                if _cancel.is_set():
                    _kill_tree(proc)
                    try:
                        proc.communicate(timeout=5)
                    except Exception:
                        pass
                    return "exit_code=130\nstdout:\n(cancelled)\nstderr:\n(cancelled)"
                if time.monotonic() >= deadline:
                    _kill_tree(proc)
                    try:
                        proc.communicate(timeout=5)
                    except Exception:
                        pass
                    return "exit_code=124\nstdout:\n(timed out)\nstderr:\n(timed out)"
    except KeyboardInterrupt:
        _kill_tree(proc)
        try:
            proc.communicate(timeout=5)
        except Exception:
            pass
        raise
    out = _clean(out or "")[:4000] or "(no stdout)"
    err = _clean(err or "")[:2000] or "(no stderr)"
    return f"exit_code={code}\nstdout:\n{out}\nstderr:\n{err}"


_ANSI_RE = re.compile(r"\x1b\[[0-9;?]*[A-Za-z]")


def _clean(text: str) -> str:
    return _ANSI_RE.sub("", text)


def _child_env() -> dict:
    """Environment for the bash tool with secrets removed (anti-exfiltration)."""
    return {k: v for k, v in os.environ.items() if not _ENV_SECRET_RE.search(k)}


def is_dangerous(command: str) -> bool:
    """Heuristic: commands that should require explicit approval."""
    return bool(command) and bool(_DANGEROUS_RE.search(command))


def _is_transient(exc: Exception) -> bool:
    """True for network / 5xx / 429 errors worth retrying."""
    if isinstance(exc, httpx.TransportError):
        return True
    if isinstance(exc, httpx.HTTPStatusError):
        return exc.response.status_code in _TRANSIENT_STATUS
    return False


def execute_tool(name: str, arguments: str, emit: Optional[Emit] = None,
                 confirm: Optional[Confirm] = None) -> str:
    if name != "bash":
        return f"Unknown tool: {name}"
    try:
        args = json.loads(arguments)
        command = args.get("command") or ""
    except json.JSONDecodeError:
        return f"Bad arguments JSON: {arguments}"
    # safety policy: risky commands need explicit approval
    reasons = classify_command(command)
    if reasons:
        allow = (os.environ.get("DEEPSEEK_ALLOW_DANGEROUS", "").strip().lower()
                 in ("1", "true", "yes", "on"))
        if not allow:
            tag = ", ".join(reasons)
            if confirm is None:
                return (f"BLOCKED ({tag}): this command looks risky and no approval channel "
                        "is available. Ask the user to run it manually, or set "
                        "DEEPSEEK_ALLOW_DANGEROUS=1 to override.")
            if not confirm(command):
                return f"BLOCKED ({tag}): the user denied this command."
    if emit is None:
        # plain REPL: print command + first line of output to stderr
        print(f"  {_CYAN}[bash] $ {command}{_RESET}", file=sys.stderr)
    result = run_command(command)
    first = result.splitlines()[0] if result else ""
    if emit is None:
        print(f"  {_CYAN}{first}{_RESET}", file=sys.stderr)
    else:
        status = "success" if result.startswith("exit_code=0") else "error"
        emit("tool", {"command": command, "result": result, "status": status})
    return result


# ---------------------------------------------------------------- streaming
_DIM = "\033[2m"
_CYAN = "\033[36m"
_RESET = "\033[0m"


def stream_call(input_items: list, tools: list, emit: Optional[Emit] = None) -> list[dict]:
    """Send one streaming request. Print reasoning/text as it arrives.

    Returns the authoritative `output_item.done` items (for turn-to-turn state).
    When `emit` is given, events are forwarded to it instead of being printed.
    """
    body = {
        "model": MODEL,
        "input": input_items,
        "reasoning": {"effort": EFFORT},
        "tools": tools,
        "stream": True,
        "temperature": 0.0,
    }
    headers = {"Authorization": f"Bearer {_key()}", "Content-Type": "application/json"}
    attempt = 0
    while True:
        if _cancel.is_set():
            raise CancelledError("cancelled by user")
        started = False   # set once the server has produced any event
        output_items: list[dict] = []
        try:
            with httpx.stream("POST", f"{API_BASE}/responses", headers=headers, json=body, timeout=180) as resp:
                resp.raise_for_status()
                printed_reasoning = False
                for line in resp.iter_lines():
                    if _cancel.is_set():
                        raise CancelledError("cancelled by user")
                    if not line or not line.startswith("data:"):
                        continue
                    data = line[5:].strip()
                    if not data:
                        continue
                    started = True
                    if data == "[DONE]":
                        continue
                    try:
                        evt = json.loads(data)
                    except json.JSONDecodeError:
                        continue   # skip malformed keep-alive / partial frames
                    t = evt.get("type")

                    # surface API-side failures instead of silently "finishing"
                    if t in ("response.failed", "error", "response.incomplete"):
                        err = evt.get("error") or (evt.get("response") or {}).get("error") or t
                        raise AgentError(f"{t}: {err}")
                    if t == "response.completed":
                        usage = (evt.get("response") or {}).get("usage")
                        if emit and usage:
                            emit("usage", {"usage": usage})
                        continue

                    if t == "response.reasoning_text.delta":
                        printed_reasoning = True
                        delta = evt.get("delta", "")
                        if emit:
                            emit("reasoning", {"text": delta})
                        else:
                            sys.stderr.write(f"{_DIM}{delta}{_RESET}")
                            sys.stderr.flush()
                    elif t == "response.reasoning_text.done":
                        if printed_reasoning:
                            if emit:
                                emit("reasoning_done", {})
                            else:
                                sys.stderr.write("\n")
                                sys.stderr.flush()
                        printed_reasoning = False
                    elif t == "response.output_text.delta":
                        delta = evt.get("delta", "")
                        if emit:
                            emit("text", {"text": delta})
                        else:
                            sys.stdout.write(delta)
                            sys.stdout.flush()
                    elif t == "response.output_text.done":
                        if emit:
                            emit("text_done", {})
                        else:
                            sys.stdout.write("\n")
                            sys.stdout.flush()
                    elif t == "response.output_item.done":
                        item = evt.get("item") or {}
                        output_items.append(item)
            return output_items
        except Exception as e:
            # Retry transient failures only if nothing has streamed yet
            # (avoid duplicating already-rendered output).
            if _is_transient(e) and not started and attempt < MAX_RETRIES:
                attempt += 1
                delay = min(2 ** attempt, 8)
                if emit:
                    emit("notice", {"message": f"transient error ({e}); "
                                                  f"retry {attempt}/{MAX_RETRIES} in {delay}s…"})
                time.sleep(delay)
                continue
            raise


# ---------------------------------------------------------------- context budget
def _items_size(items: list) -> int:
    return sum(len(json.dumps(it, ensure_ascii=False, default=str)) for it in items)


def _compact(input_items: list) -> None:
    """Trim the oldest whole turns so the request stays under a rough char budget."""
    if len(input_items) <= 2 or _items_size(input_items) <= MAX_CONTEXT_CHARS:
        return
    system = input_items[0]
    groups: list[list] = []
    cur: list = []
    for it in input_items[1:]:
        if it.get("type") == "message" and it.get("role") == "user":
            if cur:
                groups.append(cur)
            cur = [it]
        else:
            cur.append(it)
    if cur:
        groups.append(cur)
    while len(groups) > 1 and \
            _items_size([system] + [x for g in groups for x in g]) > MAX_CONTEXT_CHARS:
        groups.pop(0)
    input_items[:] = [system] + [x for g in groups for x in g]


def _abort(emit: Optional[Emit]) -> None:
    if emit:
        emit("aborted", {})
    else:
        print("\n[aborted]", file=sys.stderr)


# ---------------------------------------------------------------- core loop
def run_agent(input_items: list, emit: Optional[Emit] = None,
              confirm: Optional[Confirm] = None) -> None:
    """Keep calling the model, running tools, feeding results back.

    No cap on tool rounds: keep going until the model produces a final message.
    """
    while True:
        if _cancel.is_set():
            _abort(emit)
            return
        _compact(input_items)
        try:
            output_items = stream_call(input_items, TOOLS, emit)
        except CancelledError:
            _abort(emit)
            return
        except Exception as e:
            # Abort this turn on error. (Previously it fell through and used a
            # stale/undefined `output_items`, causing UnboundLocalError or loops.)
            if emit:
                emit("error", {"message": str(e)})
                return
            raise
        # feed completed items back so the model sees its own reasoning & calls
        input_items.extend(output_items)

        tool_calls = [it for it in output_items if it.get("type") == "function_call"]
        if not tool_calls:
            if emit:
                emit("done", {})
            return

        for call in tool_calls:
            result = execute_tool(call.get("name", ""), call.get("arguments", "{}"), emit, confirm)
            input_items.append(
                {
                    "type": "function_call_output",
                    "call_id": call.get("call_id"),
                    "output": result,
                }
            )


# ---------------------------------------------------------------- front-ends
def _system_item() -> dict:
    return {"type": "message", "role": "system", "content": [{"type": "input_text", "text": SYSTEM}]}


def _user_item(text: str) -> dict:
    return {"type": "message", "role": "user", "content": [{"type": "input_text", "text": text}]}


def single_shot(text: str) -> None:
    """Run one prompt and stream to stdout (plain mode)."""
    input_items = [_system_item()]
    if text:
        input_items.append(_user_item(text))
    try:
        with cancellable():
            run_agent(input_items)
    except KeyboardInterrupt:
        print("\n[aborted]", file=sys.stderr)
    except Exception as e:
        print(f"[error] {e}", file=sys.stderr)


def _plain_confirm(command: str) -> bool:
    """Ask the user to approve a risky command in the plain REPL."""
    try:
        ans = input(f"\n[approve] risky command:\n  {command}\nRun it? [y/N] ").strip().lower()
    except (EOFError, KeyboardInterrupt):
        return False
    return ans in ("y", "yes")


def repl() -> None:
    """Plain interactive REPL (no TUI)."""
    input_items = [_system_item()]
    print(f"DeepSeek agent ({MODEL}, effort={EFFORT}, streaming). '/exit' to quit.", file=sys.stderr)
    while True:
        try:
            raw = input("\n\033[1magent> \033[0m").strip()
        except (EOFError, KeyboardInterrupt):
            print(file=sys.stderr)
            break
        if not raw:
            continue
        if raw.lower() in ("/exit", "/quit", "exit", "quit"):
            break
        input_items.append(_user_item(raw))
        try:
            with cancellable():
                run_agent(input_items, confirm=_plain_confirm)
        except KeyboardInterrupt:
            print("\n[aborted]", file=sys.stderr)
        except Exception as e:
            print(f"[error] {e}", file=sys.stderr)


def main() -> None:
    _set_utf8_io()

    # Fail fast with a clear message if the API key is missing.
    try:
        _key()
    except RuntimeError as e:
        print(f"[error] {e}", file=sys.stderr)
        sys.exit(1)

    args = sys.argv[1:]
    force_tui = "--tui" in args
    force_plain = "--plain" in args
    args = [a for a in args if a not in ("--tui", "--plain")]

    # --one "text"  (also: echo "..." | agent.py --one)
    if "--one" in args:
        i = args.index("--one")
        rest = " ".join(args[i + 1:]).strip()
        if rest:
            text = rest
        elif sys.stdin.isatty():
            # interactive terminal with no piped prompt: don't block reading stdin
            sys.stderr.write("Usage: agent.py --one \"your prompt\"   "
                             "(or pipe: echo '...' | agent.py --one)\n")
            sys.exit(2)
        else:
            text = read_stdin_text()
        single_shot(text)
        return

    # positional text => single-shot (backward compatible: `agent.py "hello"`)
    if args:
        single_shot(" ".join(args))
        return

    # interactive
    if force_tui or not force_plain:
        try:
            from tui import run_tui   # lazy import so plain mode never needs the deps
        except ImportError:
            sys.stderr.write("[warn] TUI unavailable (prompt_toolkit/rich not installed); "
                             "falling back to plain REPL.\n")
            repl()
            return
        run_tui()
    else:
        repl()


if __name__ == "__main__":
    main()
