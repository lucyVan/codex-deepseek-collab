from pathlib import Path
import asyncio
import tempfile
import unittest
import sys

import httpx2
from mcp import ClientSession
from mcp.client.stdio import StdioServerParameters, stdio_client
from mcp.client.streamable_http import streamable_http_client

from collab import service
from collab.service_state import load_registration


PROJECT = Path(__file__).resolve().parents[1]
EXPECTED = {"collab_status", "collab_submit", "collab_get", "collab_wait", "collab_cancel", "collab_review"}


class LightweightMCPTests(unittest.TestCase):
    async def direct(self, root):
        registration, token = load_registration(root)
        headers = {"Authorization": "Bearer " + token, "X-Collab-Instance": registration["instance_id"]}
        async with httpx2.AsyncClient(headers=headers, trust_env=False, timeout=httpx2.Timeout(20)) as client:
            async with streamable_http_client(registration["url"], http_client=client) as (read, write):
                async with ClientSession(read, write) as session:
                    await session.initialize()
                    tools = await session.list_tools()
                    status = await session.call_tool("collab_status", {})
                    self.assertFalse(status.is_error)
                    return {tool.name for tool in tools.tools}

    async def bridge(self, root):
        parameters = StdioServerParameters(
            command=sys.executable,
            args=["-I", "-B", str(PROJECT / "tools/mcp_collab.py"), "--state-root", str(root)],
        )
        async with stdio_client(parameters) as (read, write):
            async with ClientSession(read, write) as session:
                await session.initialize()
                tools = await session.list_tools()
                status = await session.call_tool("collab_status", {})
                self.assertFalse(status.is_error)
                return {tool.name for tool in tools.tools}

    async def exercise(self, root):
        first, second = await asyncio.gather(self.direct(root), self.direct(root))
        third = await self.bridge(root)
        self.assertEqual(first, EXPECTED)
        self.assertEqual(second, EXPECTED)
        self.assertEqual(third, EXPECTED)
        registration, token = load_registration(root)
        async with httpx2.AsyncClient(trust_env=False, timeout=httpx2.Timeout(10)) as anonymous:
            response = await anonymous.post(registration["url"], json={})
            self.assertEqual(response.status_code, 401)
        headers = {"Authorization": "Bearer " + token, "X-Collab-Instance": registration["instance_id"]}
        async with httpx2.AsyncClient(headers=headers, trust_env=False, timeout=httpx2.Timeout(10)) as client:
            response = await client.post(registration["admin_url"] + "/v1/command", json={})
            self.assertEqual(response.status_code, 404)

    async def bridge_before_and_after_service(self, root, workspace):
        parameters = StdioServerParameters(
            command=sys.executable,
            args=["-I", "-B", str(PROJECT / "tools/mcp_collab.py"), "--state-root", str(root)],
        )
        async with stdio_client(parameters) as (read, write):
            async with ClientSession(read, write) as session:
                await session.initialize()
                tools = {tool.name for tool in (await session.list_tools()).tools}
                unavailable = await session.call_tool("collab_status", {})
                self.assertTrue(unavailable.is_error)
                service.start(root, workspace)
                try:
                    status = await session.call_tool("collab_status", {})
                    self.assertFalse(status.is_error)
                finally:
                    service.stop(root)
                return tools

    def test_bridge_lists_tools_without_service_and_recovers(self):
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            root, workspace = base / "state", base / "workspace"
            workspace.mkdir()
            tools = asyncio.run(self.bridge_before_and_after_service(root, workspace))
            self.assertEqual(tools, EXPECTED)

    def test_two_http_clients_and_stdio_bridge_share_one_service(self):
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            root, workspace = base / "state", base / "workspace"
            workspace.mkdir()
            service.start(root, workspace)
            try:
                asyncio.run(self.exercise(root))
            finally:
                result = service.stop(root)
                self.assertTrue(result["process_exit_confirmed"])


if __name__ == "__main__":
    unittest.main()
