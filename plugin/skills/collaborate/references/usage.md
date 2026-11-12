# 提交、等待与验收

## 提交

`collab_submit` 常用字段：

```json
{
  "request_id": "stable-task-key",
  "execution_workspace": "/absolute/path/to/current/codex/workspace",
  "root": "codex-deepseek-collab",
  "goal": "实现并运行一个明确的小任务",
  "read_inputs": ["collab/controller.py"],
  "write_paths": ["output/"],
  "exclude_paths": ["runtime"],
  "constraints": ["只修改授权路径"],
  "acceptance": ["交付内容和实际运行结果满足目标"],
  "timeout_seconds": 1800
}
```

Codex 默认自动把当前工作区绝对路径填入 `execution_workspace`；未提供时才回退到服务默认值。`root` 相对本次 execution workspace，read/write/exclude 路径相对 task root；**root 可以是现有 git 仓库根或无 Git 目录**。无 Git 时 dry-run 只返回 `git_init_required`，实际 submit 自动初始化并创建安全基线提交；已有仓库子目录不会被嵌套初始化。执行在 execution workspace 的 `.worktrees/<run-id>` 隔离副本中进行，交付标准 git diff/patch；review ACCEPTED 时若主仓库已前进，先 rebase，返回 `REVIEW_BASE_CHANGED`，Codex 重读新 diff 后再 review；冲突返回 `WORKTREE_CONFLICT`，dirty overlap 返回 `WORKTREE_MAIN_DIRTY_CONFLICT`。`exclude_paths` 同时从证据清单和 patch 排除，被排除变化不会应用到主仓库。交付后 worktree 与冻结 patch 不一致时返回 `WORKTREE_CHANGED_AFTER_DELIVERY`，主仓库不变。`dry_run=true` 只校验，不创建 run；非 native 服务只支持 dry-run。

`collab_submit`/`collab_review` 成功时只返回轻量 run head，不返回完整 spec/result；需要时用 `collab_get`。`request_id` 是持久幂等键，只允许 `[A-Za-z0-9_-]`（不能含点号）；相同输入重试返回原结果，不同输入返回 `IDEMPOTENCY_CONFLICT`；未知结果先查询，不盲目重试。参数校验失败会返回 `INVALID_INPUT`，并在 `field`/`message` 中给出字段级原因。

## 等待

`collab_wait` 只返回轻量 run head（`id/version/state/summary`），不重复携带 spec/result/manifest；需要细节时用 `collab_get`。每次 wait 传当前已看到的 version：

```text
collab_wait(id=run_id, after_version=seen_version, max_seconds=60)
```

version 变大就返回轻量 run head；否则 60 秒后 `timed_out=true`。客户端断开只关闭 MCP transport，不取消 run。没有独立 Codex 工作时，继续等待到 DELIVERED、FAILED、CANCELLED 等可推进状态。

## 结果与差异

- `collab_get()`：小页列出 run，after_row 继续。
- `collab_get(id=..., view="summary")`：轻量概览（状态、spec 摘要、manifest 计数、review 结论），优先使用。
- `collab_get(id=..., view="result")`：读取完整 spec、delivery、manifest 和 review。
- `collab_get(id=..., view="diff", offset=0, limit=10)`：分页读取文本 diff；`out_of_scope` 最多返回 20 条并带 `out_of_scope_total/truncated`；`after_matches_current=false` 表示交付后变化，ACCEPTED 会拒绝 apply。worktree 清理后为 null，reason 为 `WORKTREE_CLEANED_UP`。
- `collab_get(id=..., view="history")`：读取按 version 记录的状态历史。
- `collab_get(id=..., current=true)`：比较交付清单与当前工作区。

write_paths 外变化单独列出；manifest 会带 `root` 与 `excluded`。`collab_get(id=...)` 顶层 `outcome_unknown=true` 表示结果未知而非确定失败。文本 diff 截断时不能当作完整内容；二进制 patch 按原始字节保存和 apply。`view="diff"` 不能与 `current=true` 同时使用。

## 最终 review

只有 DELIVERED 可以 review。传当前 run.version 作为 expected_version，结论为 ACCEPTED 或 REJECTED，并填写 summary 与 checks。同一 task root 仍有 RUNNING run 时返回 `RUN_BUSY`。Codex 应核对实际文件、执行结果和验收条件；需要独立复跑时在 Codex 会话中执行，或另行派发只读 DeepSeek 任务。

拒绝后如需重做，创建新任务，不自动修复或恢复旧 DSH Session。

## max-tokens 暂停与续跑

DSH 因 max output tokens 暂停时，run 进入 `PAUSED`，保留 Session、worktree 和 root lease；这不是 DELIVERED，不能 ACCEPTED/apply。先用 `collab_get(view="summary")` 和必要的 diff 检查部分结果，再调用：

```json
{
  "request_id": "resume-long-task-001",
  "id": "原 run ID",
  "expected_version": 3,
  "instructions": "继续完成剩余实现，并重新运行尚未通过的测试"
}
```

续跑回到 `QUEUED`，再由 Scheduler claim 为 `RUNNING`：使用原 `session_id`、新 prompt request ID 和旧 cursor，不新建 Session、不重复完整历史。原 goal、write_paths、acceptance、execution_workspace 和绝对 deadline 保持不变，最多续跑 2 次。旧终态不会被当成本次完成。若现场不可信、超过续跑上限或预算耗尽，应 REJECTED 或 cancel 并清理。
