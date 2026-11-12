# 故障处理

| 现象 | 处理 |
|---|---|
| `SERVICE_UNAVAILABLE` | Controller 未运行或注册失效；先 start 服务，必要时看 logs |
| `INVALID_INPUT` | 看错误里的 `field`/`message`：常见是 `request_id` 含点号、enum 非法、字段超长 |
| `GIT_REQUIRED` | 未安装 git；安装后重试 |
| `GIT_EVIDENCE_REQUIRED` | task root 位于已有仓库的子目录；选择仓库根，不自动创建嵌套仓库 |
| `GIT_BASELINE_TOO_LARGE` | 单文件超过 100 MiB；缩小范围或排除大文件，不在线初始化基线 |
| `GIT_BASELINE_FAILED` | 自动初始化或基线提交失败；检查 hooks、权限、已有 `.git` 和磁盘状态 |
| `ROOT_BUSY` | 同 task root 还有 QUEUED/RUNNING/PAUSED/DELIVERED run；先 review/cancel 再提交 |
| `DESKTOP_NOT_READY` | DSH Desktop 未连接；看 `collab_status` 的 `desktop.repair.command`，重启 App 后重新 pair |
| `REVIEW_BASE_CHANGED` | 主仓库已前进；worktree 已 rebase，未 apply；重新读 diff 后再 review |
| `WORKTREE_CONFLICT` | worktree rebase 到新 HEAD 有冲突；不自动解决，worktree 保留，Codex 人工处理或重做 |
| `WORKTREE_MAIN_DIRTY_CONFLICT` | 主仓库未提交改动与任务路径重叠；先处理/提交用户改动，或重做任务 |
| `WORKTREE_APPLY_FAILED` / `WORKTREE_REBASE_FAILED` | 预检或 apply 失败；主仓库不变，worktree/patch 保留 |
| `WORKTREE_IGNORE_FAILED` | 无法把 `.worktrees/` 加入 workspace 仓库的 `.git/info/exclude`；检查 workspace 是否可写 |
| `NATIVE_DISABLED` | 纯校验用 dry_run；实际执行前以 --native 启动单例服务 |
| `DESKTOP_NOT_PAIRED` / 配对失效 | 检查 DSH App 和官方配对；不要改模型、权限或 profile 绕过 |
| `AGENT_PRESET_UNAVAILABLE` / `DESKTOP_EXECUTION_MISMATCH` | 核对 preset 注册以及实际 Session 的 preset、cwd 和权限；配置声明不等于真实能力，必要时运行小 fixture |
| `IDEMPOTENCY_CONFLICT` | 同一 request_id 输入不同；查原 run，使用新 key 表示真正的新任务 |
| `REQUEST_OUTCOME_UNKNOWN` | 先按 request_id/run 查询，不自动重发 |
| `VERSION_CONFLICT` | 重新读取 run.version，确认正在操作同一代结果 |
| `PAUSED` / `MAX_OUTPUT_TOKENS` | 保留 Session/worktree；先检查部分结果，再用 collab_resume 同 Session 续跑，或直接拒绝/取消。不要接受部分 patch |
| `CONTINUATION_LIMIT` | 已达到最多 2 次续跑；检查剩余工作并改用小任务，不继续盲目续跑 |
| `RESUME_UNAVAILABLE` | 原 Session、cwd、权限、worktree 或上一次 max-tokens 终态不可确认；不要创建同名新 Session 冒充恢复 |
| `MISSING_INPUT` | 检查 root 与 read_inputs，不先提交再补输入 |
| `RUN_BUSY` | 同一 task root 还有 RUNNING run；等它到终态再 review |
| `LIMIT` | 缩小 task root、文件范围或 diff 分页 |
| wait 60 秒超时 | 只表示 version 未变化，继续 after_version 等待 |
| cancel 后仍 RUNNING | ACK 不是终态；继续等待 driver 和 DSH 确认取消 |
| Controller 重启后仍 FAILED/outcome_unknown | 重启时会尝试按 `session_id` 恢复 Run 状态；仍未知说明 Session 不可观测。不自动重发 prompt，核对实际工作区后新建任务 |
| `WORKTREE_CHANGED_AFTER_DELIVERY` | worktree 已与冻结 patch 不同；主仓库未 apply。检查变化来源，REJECTED 后新建任务，不接受旧交付 |
| `REVIEW_BASE_CHANGED` | main HEAD 已前进且 worktree 已 rebase；重读新 diff 后再 review |
| worktree 清理后 current 不可用 | 这是正常终态；after_matches_current=null 不表示当前 main 与交付匹配，需要时直接检查 main |

没有独立 candidate/formal、自动修复、handoff 或后台验证队列。Codex 复跑失败或补写代码时，应分别说明 DeepSeek 原始交付与 Codex 后续修改，不混写为 DeepSeek 独立验收。
