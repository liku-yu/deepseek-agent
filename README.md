# DeepSeek Agent

极简命令行 agent:DeepSeek **Responses API**(`POST /v1/responses`)流式,自带 `bash` 工具在 Git Bash 中执行真实命令。`uv` 管理,可打包成单个 exe。

- 模型 `deepseek-v4-flash-vision-exp`(实验性)、推理 `high`
- 交互界面:prompt_toolkit + Rich 的 TUI(可用 `--plain` 退化为纯文本)

## 依赖

- Python 3.11–3.13(uv 自动管理)
- [Git for Windows](https://git-scm.com)(`bash` 工具需要)
- `DEEPSEEK_API_KEY`:环境变量,或 exe/脚本旁的 `.env`(见 `.env.example`)

## 使用

```bash
uv sync                          # 安装依赖(网络不通见下方镜像)
uv run python agent.py           # TUI(默认)
winpty uv run python agent.py    # Git Bash 下需 winpty
uv run python agent.py --plain   # 纯文本 REPL
uv run python agent.py --one "用 bash 看看当前目录"   # 单次
echo "..." | uv run python agent.py --one            # 管道输入
```

TUI 内:`/help` 帮助、`/clear` 清屏、`/exit` 退出;运行中 **Ctrl+C 可中止**(会杀掉整棵子进程树)。

> **TUI 终端要求**:需原生 Windows 控制台 → 用 Windows Terminal / cmd / PowerShell,或 Git Bash 加 `winpty`。
> **中文/UTF-8**:已统一 UTF-8 I/O,中文输入输出不会乱码。

## 配置(均可用环境变量覆盖)

| 变量 | 默认 | 说明 |
|---|---|---|
| `DEEPSEEK_MODEL` | `deepseek-v4-flash-vision-exp` | 模型(账号未开通时改这里) |
| `DEEPSEEK_EFFORT` | `high` | 推理强度 |
| `DEEPSEEK_CMD_TIMEOUT` | `120` | 单条命令超时(秒) |
| `DEEPSEEK_MAX_RETRIES` | `3` | 瞬时错误重试次数 |
| `DEEPSEEK_MAX_CONTEXT_CHARS` | `200000` | 超限时裁掉最旧整轮对话 |
| `DEEPSEEK_ALLOW_DANGEROUS` | 空 | 设 `1` 跳过危险命令审批 |
| `DEEPSEEK_API_BASE` | `https://api.deepseek.com/v1` | 接口地址 |

> **安全**:危险命令(`rm -rf` 等)、敏感文件(`.env`/`.ssh` 等)、网络命令(`curl` 等)默认需审批,非交互(`--one`)直接拦截。**这是启发式防护、不是沙箱**,请自行评估风险。

## 原理

循环请求 `/v1/responses`(流式渲染 `reasoning`/`text`);出现 `function_call` 就用 `bash` 执行并回填 `function_call_output`,直到模型给出最终回答(**不限轮数**)。

## 构建 / 发布

```bash
./build.sh              # 构建 exe -> C:/develop/bin/deepseek-agent.exe(OUT= 可改目录)
./ship.sh "commit msg"  # 构建 + 提交并推送到 GitHub / cnb.cool
```

- `build.sh`:先 `uv sync` 装依赖,再 `pyinstaller` 打成单文件 exe;`OUT=dist ./build.sh` 可改输出目录。
- `ship.sh`:先提交并推送代码,再重建 exe;推送失败会自动重试(GitHub 链路偶发不稳)。
- 等价的直接命令:`uv run pyinstaller --noconfirm --clean --distpath C:/develop/bin deepseek-agent.spec`。

## 开发

```bash
# pypi.org 不通时用国内镜像
UV_DEFAULT_INDEX=https://mirrors.aliyun.com/pypi/simple/ uv sync
```

## 文件

`agent.py` 核心逻辑 · `tui.py` 界面 · `build.sh`/`ship.sh` 构建发布 · `deepseek-agent.spec` 打包配置 · `pyproject.toml`/`uv.lock` 依赖 · `.env.example` key 模板
