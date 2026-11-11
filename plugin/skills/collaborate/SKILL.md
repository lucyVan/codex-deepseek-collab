---
name: collaborate
description: 仅在用户明确指定 DeepSeek 时派发本机任务；DeepSeek 在共享目录实现、执行、修复并复跑，Codex 独立检查实际差异和运行证据后最终接受或拒绝。
---

# Codex × DeepSeek 轻量协作

使用 codex_deepseek_collab 的 6 个 MCP 工具。只有用户明确指定交给 DeepSeek 才能实际 submit；选择插件、读取技能、查看状态或 Codex 判断适合协作都不构成授权。沿用本轮已有明确授权，不重复询问。

## 当前职责

- DeepSeek：在授权 read/write 范围内实现、运行、修复、复跑并返回实际证据。
- Codex：拆解任务、限定范围和预算、检查差异、按需独立复跑、最终接受或拒绝。
- Controller：单例 MCP 服务，负责 request 幂等、受控并发调度（max 2，不同 root）、root lease、run.version、差异清单和进程清理。
- stdio bridge：只转发 MCP，不包含任务逻辑。

Codex 自己的工作直接在主会话或原生子 Agent 中执行，不需要创建 collab run。

## 6 个工具

| 工具 | 何时使用 |
|---|---|
| `collab_status` | 检查当前服务与 DSH 配对 |
| `collab_submit` | 校验并提交；`dry_run=true` 只校验 |
| `collab_get` | 查 run：`view="summary"` 轻量概览（先看这个）、`result` 全量、分页 diff、history；结果顶层有 `outcome_unknown` |
| `collab_wait` | 按 `id + after_version` 最长等待 60 秒；返回轻量 run head（id/version/state/summary） |
| `collab_cancel` | 取消 queued/running 任务并继续确认终态 |
| `collab_review` | Codex 对 DELIVERED 结果接受或拒绝 |

没有独立的 preflight、collect、verify、handoff 或 feedback 工具。提交内部完成基本检查；diff 在 get 中；Codex 复验在 Codex 会话或新的只读任务中完成；重做通过新任务完成，不关联旧 run 状态。

## 日常流程

1. 调用 `collab_status`。native 未启动时先按 environment.md 启动服务；纯检查不提交任务。
2. 明确 goal、task root（必须是 git 仓库根）、read_inputs、write_paths、constraints、acceptance 和 timeout。**Codex 默认从当前会话环境读取当前工作区的绝对路径，并自动填入 `execution_workspace`，不要求用户手工配置，也不使用源码目录或历史工作区代替。** 只有无法确认当前目录或用户显式指定其他目录时，才省略该字段并回退到服务默认值。`root` 相对本次 execution workspace，其他读写路径相对 task root。`exclude_paths`（相对 task root，最多 100 个）同时排除可见 manifest 和最终 patch，被排除的变化不会在 ACCEPTED 时落盘。git 是必备依赖；执行在 HEAD 的隔离 worktree 中。
3. 需要时先 `collab_submit(dry_run=true)`。实际提交使用稳定 request_id；`request_id` 只能包含字母、数字、`_`、`-`（不能有点号），相同 request_id 只能用于相同输入。同一 task root 还有 QUEUED/RUNNING/DELIVERED run 时提交返回 `ROOT_BUSY`：先 review/cancel 前一个 run。
4. 保存 run ID 和当前 version，用 `collab_wait(after_version=...)` 等待。它只返回轻量 run head（id/version/state/summary）；需要细节时再 get，别让每次等待都携带完整 manifest。没有独立工作时持续等待，不提前结束回合。
5. 到 `DELIVERED` 后先 `collab_get(view="summary")` 看状态与 manifest 计数；需要 spec/delivery/完整 manifest 时才用 `view="result"`；用 `view=diff` 分页查看差异，必要时 `current=true` 核对交付后变化；`view=diff` 与 `current=true` 不能同时使用。`after_matches_current=false` 表示交付后变化，此时 ACCEPTED 会返回 `WORKTREE_CHANGED_AFTER_DELIVERY` 且主仓库不变；worktree 清理后该值为 null、reason 为 `WORKTREE_CLEANED_UP`，不是匹配证明。DeepSeek 自报完成、ACK 和进程退出都不是验收。
6. Codex 检查实际输出、越界路径、`main_changed` 警告和必要运行证据，再调用 `collab_review`，传当前 expected_version、ACCEPTED/REJECTED、summary 和具体 checks。若主仓库在任务期间前进，ACCEPTED 会返回 `REVIEW_BASE_CHANGED`：Controller 已把 worktree rebase 到新 HEAD，但**不会 apply**；Codex 必须重新 `collab_get(view=diff)`、重新审查后再 review。同一 task root 仍有 RUNNING run 时 review 返回 `RUN_BUSY`，应先等它到终态。

## 状态与失败

```text
QUEUED → RUNNING → DELIVERED → ACCEPTED / REJECTED
                    ├→ FAILED
                    └→ CANCELLED
```

- `DELIVERED` 不是最终验收。
- 拒绝后需要 DeepSeek 重做时，创建新任务；没有自动 repair/handoff 状态机，也不继承旧 run 的执行状态。
- running 取消只发送请求，必须继续 wait 到 CANCELLED/FAILED 等真实终态。
- `REQUEST_OUTCOME_UNKNOWN` 或断连后先按 request_id/run ID 查询，不换 key 重派。
- Controller 重启会先按已知 session_id 恢复 Run 状态：已完成则补 manifest/结果，活跃则 best-effort cancel 并记 `OUTCOME_UNKNOWN`；不自动重发 prompt。

## 差异与范围

Controller 不复制完整项目树。git 是必备依赖；task root 必须是仓库根。执行在 execution workspace 下的 `.worktrees/<run-id>` 中（如 `~/codex-deepseek/.worktrees/<run-id>`），主仓库在 review 前不变。交付报告 worktree 内新增/删除/修改与 write_paths 外变化，并生成 `worktree.patch`；ACCEPTED 后 apply 到主仓库，REJECTED 删除 worktree。

worktree 交付带 `worktree.patch`；review ACCEPTED 时如果主仓库 HEAD/工作区已变化，先在 worktree 内 rebase/三方合并到新 HEAD：无冲突则 apply，冲突则返回 `WORKTREE_CONFLICT` 并保留 worktree，不做自动冲突解决；主仓库有未提交改动且与任务路径重叠时返回 `WORKTREE_MAIN_DIRTY_CONFLICT`。REJECTED 时删除 worktree。主仓库原有的未提交改动不会进入 worktree；worker 只看 HEAD。git diff/manifest 不能替代 Codex 审查。共享目录仍要求同一任务范围单写者；交付后再次变化会记录在 current_changes 中，由 Codex 决定是否重新检查。

## 执行角色

固定使用 `collab-worker`：文件读写、jobs、todo、web，以及 macOS Bash 或 Windows PowerShell；不使用子 Agent/MCP/schedule。改动通过 worktree 隔离，由 Codex review 后决定是否 apply。不再提供 verifier/researcher preset。Windows 是明确兼容目标，但当前只有 macOS 实机验收，不能把配置或单元测试写成 Windows 已验收。

## 长任务与等待

`timeout_seconds` 默认 1800，允许 60–86400 秒，选择能覆盖实际工作的最小值。单个 run 使用一个 DSH Session；不能可靠拆分或超过 24 小时的工作应拆成多个可独立检查的任务。没有独立 Codex 工作时，持续用 `collab_wait(max_seconds=60)` 和递增的 run.version 等待，不结束回合、不高频轮询、不重复读取完整 diff。MCP client 断开不取消 run；恢复后用同一 run ID 和最新 version 继续。

具体服务、安装与排障见 references/environment.md 和 references/troubleshooting.md。
