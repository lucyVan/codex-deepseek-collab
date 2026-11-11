# Codex × DeepSeek 轻量协作（codex-deepseek-collab）

当前源版本为 **0.27.0**，插件 ID 为 `codex-deepseek-collab@collab-local`。DeepSeek 在授权范围内实现、执行、修复和复跑；Codex 独立检查实际差异与执行证据，最终接受或拒绝。

## 架构与公开契约

```text
Codex / 其他受信 MCP client
  │ stdio（bridge 无业务逻辑）或 Streamable HTTP
  ▼
单例常驻 MCP Controller
  ├── 6 个工具、参数校验、request 幂等
  ├── run.version、精简 SQLite、同 root lease
  ├── 不同 root 最多 2 个 driver 并发
  └── HEAD worktree → manifest/patch → Codex review → apply
                          │
                          ▼
                DeepSeek Harness App
```

工具只有：`collab_status`、`collab_submit`、`collab_get`、`collab_wait`、`collab_cancel`、`collab_review`。

```text
QUEUED → RUNNING → DELIVERED → ACCEPTED / REJECTED
                    ├→ FAILED
                    └→ CANCELLED
```

`DELIVERED` 不是验收。没有 candidate/formal、三门禁、自动 repair/handoff 或独立 verifier/researcher preset。Controller、幂等、取消、进程组清理和最终 review 是必要能力，不为减法删除。

## worktree 证据与 apply

- task root 必须是 git 仓库根；worker 从 HEAD 的隔离 worktree 执行，主仓库在 review 前不变。
- `write_paths` 标出授权范围，越界变化单列；worktree 是隔离与证据机制，不是 OS 安全沙箱。
- `exclude_paths` 同时从可见 manifest 和最终 patch 排除；这些变化不会在 ACCEPTED 时应用到主仓库。
- patch 使用原始字节保存，支持二进制和非 UTF-8 内容。
- 交付后 worktree 内容若与 patch 不一致，ACCEPTED 返回 `WORKTREE_CHANGED_AFTER_DELIVERY`，主仓库不变。
- main HEAD 前进时先在 worktree rebase，返回 `REVIEW_BASE_CHANGED`；Codex 必须重读新 diff 再 review。冲突、dirty overlap 和证据缺失均 fail closed。
- ACCEPTED/REJECTED 清理 worktree 后，`after_matches_current=null`、`reason=WORKTREE_CLEANED_UP`，不把无法观察伪报为匹配。

## 平台支持

Windows 是明确支持目标；当前只完成 macOS 实机验证，**Windows 代码兼容不等于已实机验收**。

| 环节 | macOS | Windows |
|---|---|---|
| Python/依赖 | `.venv/bin/python`、`requirements-macos.lock` | `.venv/Scripts/python.exe`、`requirements.lock` |
| 默认 state | `runtime/macos/state` | `runtime/windows/state` |
| worker shell | 官方一次性 `tool-bash` | 官方一次性 `tool-pwsh` |
| driver 进程 | POSIX session/process group | Windows Job Object，挂起创建后入 Job 再恢复 |
| 进程身份 | `proc_pidinfo` | `GetProcessTimes` FILETIME |
| 私有目录 | owner chmod | 当前用户 SID + SYSTEM ACL |
| Desktop | `/Applications/DeepSeek Harness.app` | `%LOCALAPPDATA%/Programs/DeepSeek Harness` |

配置生成器只在本机写本机配置，不跨平台复制 `.venv`、lock、state、Cookie、token 或日志：

```bash
# macOS
.venv/bin/python tools/configure_macos.py --check

# Windows PowerShell（在 Windows 主机执行）
.venv/Scripts/python.exe tools/configure_windows.py --check
```

通用入口为 `tools/configure_local.py`；两个平台入口只是薄封装。插件 `.mcp.json` 是生成产物。

## DSH preset

`dsh-presets/bundle/` 版本 **0.7.0**，只声明 `collab-worker`：文件读写、jobs、todo、web，以及按平台启用的 Bash 或 PowerShell；不使用子 Agent、MCP、schedule 或 plugin management。通过官方 `plugin_manager` 安装/更新并读回核对，不手改 desktop profile 或 app.asar。实际能力以 Session 执行证据为准。

## 测试与验收

```bash
.venv/bin/python -B -m unittest discover -s tests -p 'test_*.py'
.venv/bin/python tools/configure_macos.py --check
.venv/bin/python tools/verify_plugin.py \
  --plugin-root "$HOME/.codex/plugins/cache/collab-local/codex-deepseek-collab/0.27.0" \
  --output "$HOME/codex-deepseek/runtime/acceptance/plugin-sdk.json"
```

测试分为：

1. 标准库 `unittest` 的关键契约和反例，不追求覆盖率或测试数量；
2. 无模型的真实 MCP/服务/bridge 集成；
3. 限定新建 git fixture 的真实 DSH worker 端到端，核对 run、manifest/patch、最终主仓库与清理。

单元测试、MCP smoke、fake driver、插件安装和 worker 自报均不能单独代表完整验收。Windows 实机、长时间生产任务与业务项目回归仍须单独验证。
