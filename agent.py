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

import json
import os
import re
import shutil
import subprocess
import sys
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


def run_command(command: str) -> str:
    """Run a command in Git Bash. Return a compact text report."""
    bash = find_bash()
    try:
        proc = subprocess.run(
            [bash, "-lc", command],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=CMD_TIMEOUT,
            cwd=os.getcwd(),
        )
    except subprocess.TimeoutExpired:
        return "exit_code=124\nstdout:\n(timed out)\nstderr:\n(timed out)"
    out, err, code = proc.stdout, proc.stderr, proc.returncode
    out = _clean(out)[:4000] or "(no stdout)"
    err = _clean(err)[:2000] or "(no stderr)"
    return f"exit_code={code}\nstdout:\n{out}\nstderr:\n{err}"


_ANSI_RE = re.compile(r"\x1b\[[0-9;?]*[A-Za-z]")


def _clean(text: str) -> str:
    return _ANSI_RE.sub("", text)


def execute_tool(name: str, arguments: str, emit: Optional[Emit] = None) -> str:
    if name != "bash":
        return f"Unknown tool: {name}"
    try:
        args = json.loads(arguments)
        command = args.get("command", "")
    except json.JSONDecodeError:
        return f"Bad arguments JSON: {arguments}"
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
    output_items: list[dict] = []

    with httpx.stream("POST", f"{API_BASE}/responses", headers=headers, json=body, timeout=180) as resp:
        resp.raise_for_status()
        printed_reasoning = False
        for line in resp.iter_lines():
            if not line or not line.startswith("data:"):
                continue
            data = line[5:].strip()
            if not data or data == "[DONE]":
                continue
            evt = json.loads(data)
            t = evt.get("type")

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


# ---------------------------------------------------------------- core loop
def run_agent(input_items: list, emit: Optional[Emit] = None) -> None:
    """Keep calling the model, running tools, feeding results back.

    No cap on tool rounds: keep going until the model produces a final message.
    """
    while True:
        try:
            output_items = stream_call(input_items, TOOLS, emit)
        except Exception as e:
            if emit:
                emit("error", {"message": str(e)})
            else:
                raise
        # feed completed items back so the model sees its own reasoning & calls
        input_items.extend(output_items)

        tool_calls = [it for it in output_items if it.get("type") == "function_call"]
        if not tool_calls:
            if emit:
                emit("done", {})
            return

        for call in tool_calls:
            result = execute_tool(call.get("name", ""), call.get("arguments", "{}"), emit)
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
        run_agent(input_items)
    except KeyboardInterrupt:
        print("\n[aborted]", file=sys.stderr)


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
            run_agent(input_items)
        except KeyboardInterrupt:
            print("\n[aborted]", file=sys.stderr)


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
