# Codex × DeepSeek DSH Agent preset

`bundle/` 是唯一事实源，当前版本 0.7.0，只声明 `collab-worker`：文件编辑、jobs、todo、web，以及按平台启用的一次性 Bash（macOS）或 PowerShell（Windows）；不使用子 Agent、MCP、schedule 或 plugin management。

更新通过官方 `plugin_manager` 执行 `remove_bundle` → `install_bundle`，再用 `agentPresets/read` 核对 ID、平台 shell、工具权限与 persona。不得手改 desktop profile 或 app.asar。

Windows shell 声明和配置兼容已保留，但 Windows 实机验收尚未完成；`agentPresets/read` 也只证明注册，不证明真实执行。
