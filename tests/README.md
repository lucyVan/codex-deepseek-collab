# 关键测试约定

只使用标准库 `unittest`，不引入覆盖率、测试框架或网络/模型依赖。测试保护公开契约与高风险反例，不追求数量或行覆盖。

| 文件 | 关键职责 |
|---|---|
| `test_git_evidence.py` | 真实临时 git 仓库；无 Git 自动初始化与安全基线、父仓库子目录任务、exclude/patch/apply 一致、交付后变化、rebase、二进制、清理与失败关闭 |
| `test_lightweight_controller.py` | submit/get(默认 summary)/wait/cancel/review、重叠子目录互斥、HEAD 输入预检、越界落盘门禁、每次任务工作区、恢复、apply 后崩溃重试、并发 root lease、续跑参数规范化与重放 |
| `test_lightweight_store.py` | request 幂等、version、状态历史、事务内 root 排他与终态释放 |
| `test_lightweight_scheduler.py` | 每次任务工作区、不同 root 并发、取消、启动与异常兜底、PAUSED 后同 worktree 续跑 |
| `test_lightweight_mcp.py` | 真实本地 MCP 服务、stdio bridge、多 client 与认证 |
| `test_desktop_*.py` | 私有 RPC、配对、Session 终态与人工输入边界，使用本地 fake Host；覆盖连续两次同 Session 续跑、新人工输入、旧终态和缺失序号 |
| `test_configuration.py` | macOS/Windows 配置布局与当前生成产物一致 |
| `test_windows_contract.py` | Windows FILETIME 与 Job 挂起创建/入组/恢复顺序；不是实机验收 |
| `test_dsh_bundle.py` | 单 worker、平台 shell 和无 delegation 契约 |
| `test_files.py` | 路径、链接、devices 与大小写安全 |

变体优先 `subTest`，相同正例不重复。集成测试保持少量；真实 DSH 验收只在独立 fixture 中执行，不放入默认 unittest，避免模型调用和外部副作用。

```bash
.venv/bin/python -B -m unittest discover -s tests -p 'test_*.py'
```

测试通过不替代最终文件、patch、run 状态、进程与 worktree 清理核对。Windows 实机仍是独立验收项。
