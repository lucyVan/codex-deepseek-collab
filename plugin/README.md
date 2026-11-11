# Codex × DeepSeek 轻量协作 0.27.0

仅在用户明确指定 DeepSeek 时派发。DeepSeek 负责授权范围内的实现、执行、修复和复跑；Codex 独立检查实际差异与执行证据，最终接受或拒绝。

## 当前结构

- 单例常驻 MCP Controller，提供 `collab_status`、`collab_submit`、`collab_get`、`collab_wait`、`collab_cancel`、`collab_review`。
- stdio bridge 只转发 MCP；其他受信 client 可连接同一 MCP URL。
- 不同 task root 最多 2 个 driver 并发，同 root 由 lease 串行。
- 固定 `collab-worker`，没有 verifier/researcher、多阶段门禁或自动 repair/handoff。

## 任务与证据

```text
QUEUED → RUNNING → DELIVERED → ACCEPTED / REJECTED
                    ├→ FAILED
                    └→ CANCELLED
```

`root` 必须是 execution workspace 内的 git 仓库根，其他路径相对 task root。worker 从 HEAD worktree 执行；Codex 检查 summary、diff、运行证据和最终文件后才 review。

- `exclude_paths` 同时排除 manifest 和 patch，不会随 ACCEPTED 落盘。
- 交付后变化返回 `WORKTREE_CHANGED_AFTER_DELIVERY`，不得 apply。
- main HEAD 前进需要 rebase 后重新 review；冲突与证据缺失 fail closed。
- worktree 清理后 `after_matches_current=null`，`current_changes.available=false`。
- request_id 只允许 `[A-Za-z0-9_-]`；等待沿 run.version，取消 ACK 不是终态。

## macOS 与 Windows

macOS 使用 Bash、`.venv/bin/python` 和 `macos/state`；Windows 使用 PowerShell、`.venv/Scripts/python.exe` 和 `windows/state`。Windows Job、ACL、进程身份、路径校验和配置生成均保留兼容实现，但 Windows 实机验收尚未完成。

安装配置分别由 `tools/configure_macos.py`、`tools/configure_windows.py` 生成。DSH bundle 0.7.0 通过官方 `plugin_manager` 安装，不手改 profile。

详细操作见 `skills/collaborate/SKILL.md` 与 `references/`。
