"""Check the installed six-tool plugin via official MCP stdio, without model dispatch."""
from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
from pathlib import Path

from mcp import ClientSession
from mcp.client.stdio import StdioServerParameters, stdio_client


EXPECTED = {"collab_status", "collab_submit", "collab_get", "collab_wait", "collab_cancel", "collab_review"}


async def verify(root: Path):
    manifest = json.loads((root / ".codex-plugin/plugin.json").read_text(encoding="utf-8"))
    connection = json.loads((root / manifest["mcpServers"]).read_text(encoding="utf-8"))["mcpServers"]["codex_deepseek_collab"]
    assert (root / manifest["onboardingSkill"]).is_file()
    for key in ("composerIcon", "logo"):
        assert (root / manifest["interface"][key]).is_file(), key
    assert set(connection["enabled_tools"]) == EXPECTED
    params = StdioServerParameters(command=connection["command"], args=connection["args"], cwd=connection["cwd"])

    async def call(session, name, arguments):
        reply = (await session.call_tool(name, arguments)).model_dump(by_alias=True)
        assert not reply.get("isError"), name
        if reply.get("structuredContent"):
            value = reply["structuredContent"]
        else:
            value = json.loads(next(x["text"] for x in reply["content"] if x["type"] == "text"))
        assert value["ok"], name
        return value["result"]

    async with stdio_client(params) as (read, write):
        async with ClientSession(read, write) as session:
            initialization = await session.initialize()
            names = {tool.name for tool in (await session.list_tools()).tools}
            assert names == EXPECTED
            status = await call(session, "collab_status", {})
            runs = await call(session, "collab_get", {"limit": 5})
    return {
        "status": "INSTALLED_PLUGIN_MCP_READ_ONLY_SMOKE_PASSED",
        "origin": "OFFICIAL_SDK_FROM_INSTALLED_PLUGIN_NOT_DESKTOP_CHAT",
        "plugin_root": str(root), "plugin_version": manifest["version"],
        "mcp_server": initialization.model_dump(by_alias=True)["serverInfo"],
        "tools": sorted(names), "run_count_on_page": len(runs["items"]),
        "service_state": status["service"]["state"],
        "desktop_state": status["desktop"]["state"],
        "model_execution_enabled": status["service"]["model_execution_enabled"],
        "model_requests": 0,
        "files": [{"path": str(path.relative_to(root)), "sha256": hashlib.sha256(path.read_bytes()).hexdigest()}
                  for path in sorted(root.rglob("*")) if path.is_file()],
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--plugin-root", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()
    result = asyncio.run(verify(args.plugin_root.resolve()))
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({key: result[key] for key in ("status", "tools", "service_state", "desktop_state", "model_requests")},
                     ensure_ascii=False))


if __name__ == "__main__":
    main()
