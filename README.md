# DeepSeek Agent (极简,单文件 exe)

一个使用 DeepSeek **Responses API**(`POST /v1/responses`)的极简 agent。
模型 `deepseek-v4-flash-vision-exp`,推理强度 `high`,**流式输出**。
自带 `bash` 工具,可在 Git Bash 中执行真实命令。用 `uv` 管理,并打包为单个 `.exe`。

> 运行环境需已安装 [Git for Windows](https://git-scm.com) 以便 `bash` 工具执行命令。

## 使用

先准备 API key:复制 `.env.example` 为 `.env` 并填入 key,或用环境变量:
```bash
export DEEPSEEK_API_KEY=sk-xxxx
```

```bash
# 交互式对话——prompt_toolkit + Rich 的 TUI(默认)
uv run python agent.py
# 或在 Git Bash 里(winpty 提供 Windows 控制台)
winpty uv run python agent.py
# 或旧版纯文本 REPL
uv run python agent.py --plain

# 单次问答(流式输出到 stdout)
uv run python agent.py --one "用 bash 看看当前目录有哪些文件"
echo "列出当前目录文件" | uv run python agent.py --one
```

TUI 用 `prompt_toolkit`(带历史/命令补全/底部状态栏)+ `Rich`(流式思考、正文、箱式的 user 消息与 bash 工具结果)渲染。命令:输入后回车发送;`/help` 查看帮助;`/clear` 清屏;`/exit`/`/quit` 退出(Ctrl+D 亦可)。

> **终端要求**:prompt_toolkit 在 Windows 上需要一个原生控制台。请用 Windows Terminal / cmd / PowerShell 运行,或在 Git Bash(msys)里加 `winpty` 前缀;否则会提示改用 `winpty`。

## 开发运行(uv)

```bash
uv sync                       # 安装依赖(Python 3.12 + httpx + rich + prompt-toolkit)
uv run python agent.py --one "..."   # 直接从源码运行
```

## 构建 exe

```bash
./build.sh                    # 需要先用 uv 装好依赖(含 pyinstaller)
# 默认输出到 C:/develop/bin,也可: OUT=dist ./build.sh
```
产物:`C:/develop/bin/deepseek-agent.exe`(约 14MB,含 Python + httpx + rich + prompt_toolkit,已把 TUI 打进去)。
运行 exe 时在 exe 旁边放 `.env` 或设置环境变量即可换 key。exe 在 Git Bash 里同样用 `winpty deepseek-agent.exe` 启动 TUI。

## 原理

1. 组装 `input`(系统提示 + 历史),请求 `/v1/responses`,推理 `high`。
2. 响应为 SSE,按 `response.reasoning_text.delta` / `response.output_text.delta`
   实时流式渲染;用 `response.output_item.done` 收集完整条目。
3. 若出现 `function_call`,用 `bash` 执行并把 `function_call_output` 回填,
   继续下一轮(不限轮数,直到模型给出最终回答)。
4. 每轮把 `reasoning` / `message` / `function_call` 追加回上下文。

## 配置(agent.py 顶部,均可用环境变量覆盖)

| 变量 | 默认值 | 说明 |
|---|---|---|
| `MODEL` / `DEEPSEEK_MODEL` | `deepseek-v4-flash-vision-exp` | 模型 |
| `EFFORT` / `DEEPSEEK_EFFORT` | `high` | 推理强度 |
| `CMD_TIMEOUT` / `DEEPSEEK_CMD_TIMEOUT` | `120` | 单条命令超时(秒) |

> 注意:`deepseek-v4-flash-vision-exp` 是实验性模型,若你的账号未开通,
> 请通过 `DEEPSEEK_MODEL` 改成你有权限的模型。

## 文件

```
agent.py             # agent 逻辑(uv/Python,流式;含事件回调供 TUI 复用)
tui.py               # prompt_toolkit + Rich 的 TUI
build.sh             # 构建 exe
deepseek-agent.spec  # PyInstaller 规范(onefile + optimize,含 rich/prompt_toolkit)
pyproject.toml       # uv 项目定义(httpx + rich + prompt-toolkit)
.env.example         # API key 模板(真正的 key 在 .env,不入库)
.venv/               # uv 虚拟环境
```

> TUI 依赖 `rich`/`prompt_toolkit`(随 `uv sync` 一并安装);`--one`/`--plain` 不依赖它们。
