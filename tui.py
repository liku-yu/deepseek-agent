#!/usr/bin/env python3
"""A terminal TUI for the DeepSeek agent, built with prompt_toolkit + Rich.

- prompt_toolkit: styled input line, persistent history, command autocompletion,
  a bottom toolbar (model / effort).
- Rich: styled transcript, streamed reasoning (dim) and answer (normal),
  boxed user messages and `bash` tool results.

Launch with:  uv run python agent.py      (interactive default)
              uv run python agent.py --tui
The core agent logic lives in agent.py; this module only renders it.
"""

from __future__ import annotations

import os
import sys

import agent as agent_mod
from prompt_toolkit import PromptSession
from prompt_toolkit.auto_suggest import AutoSuggestFromHistory
from prompt_toolkit.completion import WordCompleter
from prompt_toolkit.formatted_text import ANSI, HTML
from prompt_toolkit.history import FileHistory
from rich.console import Console
from rich.panel import Panel
from rich.text import Text

# ---------------------------------------------------------------- palette (pi dark)
ACCENT = "#8abeb7"      # accent / prompt
BORDER = "#5f87ff"      # borders
SUCCESS = "#b5bd68"     # tool success
ERROR = "#cc6666"       # tool error / errors
DIM = "#666666"         # reasoning / muted
TEXT = "#d4d4d4"        # default text
USER_BG = "#343541"     # user message background
TOOL_OK_BG = "#283228"
TOOL_ERR_BG = "#3c2828"

COMMANDS = ["/exit", "/quit", "/clear", "/help", "/one"]
PROMPT = ANSI("\x1b[1;96magent> \x1b[0m")  # bright-cyan prompt (visible: "agent> ")

console = Console()


def _banner() -> None:
    console.print(
        Panel(
            Text(f" DeepSeek Agent  ·  {agent_mod.MODEL}  ·  effort={agent_mod.EFFORT}", style=ACCENT),
            border_style=BORDER,
            title=Text("agent", style=ACCENT),
            title_align="left",
            padding=(0, 1),
        )
    )
    console.print(Text("  '/help' for commands · '/exit' to quit", style=DIM))
    console.print()


def _render_user(text: str) -> None:
    console.print()
    console.print(Panel(Text(text, style=TEXT), title=Text("user", style=ACCENT),
                        title_align="left", border_style=BORDER,
                        style=f"on {USER_BG}", padding=(0, 1), expand=True), markup=False)


def _render_tool(data: dict) -> None:
    status = data.get("status", "success")
    fg = SUCCESS if status == "success" else ERROR
    bg = TOOL_OK_BG if status == "success" else TOOL_ERR_BG
    command = data.get("command", "")
    result = data.get("result", "")
    console.print(Panel(Text(result or "(no output)", style=TEXT),
                        title=Text(f"bash · {command}", style=fg),
                        title_align="left", border_style=fg,
                        style=f"on {bg}", padding=(0, 1), expand=True), markup=False)


def _help() -> None:
    console.print(Text("  Commands:", style=ACCENT))
    console.print(Text("    /exit, /quit     Leave the session", style=TEXT))
    console.print(Text("    /clear           Clear the screen", style=TEXT))
    console.print(Text("    /help            Show this help", style=TEXT))
    console.print(Text("    anything else    Send to the agent (may run bash)", style=TEXT))
    console.print()


# ---------------------------------------------------------------- streaming renderer
def make_emit():
    """Build the emit callback that renders streaming events with Rich."""
    def emit(event: str, payload: dict) -> None:
        if event == "reasoning":
            console.print(payload.get("text", ""), end="", style=f"italic {DIM}", markup=False)
        elif event == "reasoning_done":
            console.print()
        elif event == "text":
            console.print(payload.get("text", ""), end="", style=TEXT, markup=False)
        elif event == "text_done":
            console.print()
        elif event == "tool":
            console.print()          # keep the box on its own lines
            _render_tool(payload)
        elif event == "done":
            console.print()
        elif event == "error":
            console.print(Text(f"[error] {payload.get('message', 'unknown')}", style=ERROR))
    return emit


# ---------------------------------------------------------------- main loop
def _has_windows_console() -> bool:
    """True when running inside a real Windows console (or a non-Windows tty).

    Git Bash / mintty expose an xterm pty with no Windows console, so
    prompt_toolkit's win32 backend fails. winpty provides a real console.
    """
    if os.name != "nt":
        return True
    import ctypes
    kernel32 = ctypes.windll.kernel32
    handle = kernel32.GetStdHandle(-11)          # STD_OUTPUT_HANDLE
    mode = ctypes.c_uint()
    return bool(kernel32.GetConsoleMode(handle, ctypes.byref(mode)))


def run_tui() -> None:
    """Interactive prompt_toolkit + Rich front-end."""
    # Prefer UTF-8 so model output doesn't garble / crash on a GBK console.
    agent_mod._set_utf8_io()

    # Guard: prompt_toolkit needs a Windows console on Windows. Under Git Bash
    # (msys pty) it would crash with NoConsoleScreenBufferError, so hint first.
    if os.name == "nt" and "xterm" in os.environ.get("TERM", "") and not _has_windows_console():
        console.print(Text(
            "This TUI needs a native Windows console.\n"
            "  In Git Bash (msys), run with:  winpty uv run python agent.py\n"
            "  Or run the script/exe in Windows Terminal, cmd or PowerShell.",
            style=ERROR))
        sys.exit(1)

    history_path = os.path.join(os.path.expanduser("~"), ".deepseek_agent_history")
    session = PromptSession(
        history=FileHistory(history_path),
        auto_suggest=AutoSuggestFromHistory(),
        completer=WordCompleter(COMMANDS, ignore_case=True),
        complete_while_typing=True,
    )

    input_items = [agent_mod._system_item()]
    emit = make_emit()

    _banner()
    while True:
        # toolbar/model+effort at the bottom; escape only that line.
        try:
            raw = session.prompt(PROMPT, bottom_toolbar=lambda: HTML(
                f"<b><ansidefault>{agent_mod.MODEL} · effort={agent_mod.EFFORT}</ansidefault></b>"))
        except KeyboardInterrupt:
            console.print()
            continue
        except EOFError:
            console.print()
            break

        text = (raw or "").strip()
        if not text:
            continue
        low = text.lower()
        if low in ("/exit", "/quit"):
            break
        if low == "/help":
            _help()
            continue
        if low == "/clear":
            if os.name == "nt":
                os.system("cls")
            else:
                os.system("clear")
            _banner()
            continue

        _render_user(text)
        input_items.append(agent_mod._user_item(text))
        try:
            agent_mod.run_agent(input_items, emit=emit)
        except KeyboardInterrupt:
            console.print(Text("\n[aborted]", style=DIM))


def main() -> None:
    # Fail fast if the API key is missing (mirror agent.py's behaviour).
    try:
        agent_mod._key()
    except RuntimeError as e:
        print(f"[error] {e}", file=sys.stderr)
        sys.exit(1)
    run_tui()


if __name__ == "__main__":
    main()
