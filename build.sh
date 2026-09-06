#!/usr/bin/env bash
# Build a single-file .exe for the DeepSeek CLI agent (with the prompt_toolkit+Rich TUI).
#       ./build.sh                    -> C:/develop/bin/deepseek-agent.exe
#       OUT=dist ./build.sh           -> dist/deepseek-agent.exe (custom output)
set -euo pipefail
cd "$(dirname "$0")"

OUT="${OUT:-C:/develop/bin}"
mkdir -p "$OUT"

# The committed spec pins onefile + optimize=2 + excludes, and bundles tui/rich/prompt_toolkit.
uv run pyinstaller --noconfirm --clean --distpath "$OUT" deepseek-agent.spec

echo ""
echo "Built: $(cygpath -w "$OUT" 2>/dev/null || echo "$OUT")/deepseek-agent.exe"
