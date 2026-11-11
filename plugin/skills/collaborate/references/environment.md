# 环境、安装与服务

## 双平台布局

后端为 `$HOME/codex-deepseek/codex-deepseek-collab`（Windows 可位于其他工作区根）。当前实测 DSH 为 `0.2.2-rc.1`，Python 使用后端独立 3.12 `.venv`，git 为必备依赖。

| 项目 | macOS | Windows |
|---|---|---|
| Python | `.venv/bin/python` | `.venv/Scripts/python.exe` |
| 依赖 lock | `requirements-macos.lock` | `requirements.lock` |
| state | `runtime/macos/state` | `runtime/windows/state` |
| DSH App | `/Applications/DeepSeek Harness.app` | `%LOCALAPPDATA%/Programs/DeepSeek Harness` |
| worker shell | `tool-bash` | `tool-pwsh` |
| 进程清理 | POSIX process group | Windows Job Object |

配置只能在本机生成，不跨平台复制 `.venv`、lock、state、Cookie、token 或日志：

```bash
# macOS
.venv/bin/python tools/configure_macos.py
.venv/bin/python tools/configure_macos.py --check
```

```powershell
# Windows
.venv/Scripts/python.exe tools/configure_windows.py
.venv/Scripts/python.exe tools/configure_windows.py --check
```

通用实现为 `tools/configure_local.py`，两个平台入口是薄封装。Windows 是明确需求，配置、进程、路径、ACL、preset 和证据链保留兼容实现；Windows 实机验收仍未完成。

## 插件与 preset

DSH bundle 0.7.0 只有 `collab-worker`，通过官方 `plugin_manager` 的 `remove_bundle` / `install_bundle` 安装更新，再用 `agentPresets/read` 核对。macOS 启用一次性 Bash，Windows 启用一次性 PowerShell；不包含子 Agent、MCP、schedule 或 plugin management。不要手改 desktop profile 或 app.asar，实际能力以真实 Session 为准。

## 服务

macOS 示例：

```bash
STATE="$HOME/codex-deepseek/runtime/macos/state"
WORKSPACE="$HOME/学习"
.venv/bin/python -B -m collab --state-root "$STATE" --execution-workspace "$WORKSPACE" service start --native
.venv/bin/python -B -m collab --state-root "$STATE" --execution-workspace "$WORKSPACE" service status
.venv/bin/python -B -m collab --state-root "$STATE" --execution-workspace "$WORKSPACE" service stop
```

Windows 使用对应的 `.venv/Scripts/python.exe` 和 `windows/state`，命令参数相同。

当前安装把 `/Users/fanluxi/学习` 作为 execution workspace。DeepSeek task root 必须位于该目录内，`root` 使用相对它的路径；state 仍独立保存在 `runtime/macos/state`，不与业务文档混放。

stdio bridge 即使 Controller 未运行也暴露 6 个工具。带 `--execution-workspace` 时，业务工具遇到传输不可用会 native 自动启动一次并重试；`collab_status` 不自动启动。desktop 非 CONNECTED 时 status 返回修复步骤，submit 返回 `DESKTOP_NOT_READY`；不会自动重启 App 或配对。

每个 run 在 execution workspace 的 `.worktrees/<run-id>` 中执行，ACCEPTED 后才 apply 到主仓库。`.worktrees/` 自动加入所在 git 仓库的 `.git/info/exclude`。`/v1/health`、`/v1/stop` 仅用于私有生命周期，任务不经过自定义 `/v1/command`。
