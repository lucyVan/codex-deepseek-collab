import json
from pathlib import Path
import sys
import unittest

from tools import configure_local


ROOT = Path(__file__).resolve().parents[1]


class ConfigurationTests(unittest.TestCase):
    def test_platform_layouts_and_current_host_configuration(self):
        workspace = ROOT.parent
        for platform, python_parts, state_name in (
            ("darwin", (".venv", "bin", "python"), "macos"),
            ("win32", (".venv", "Scripts", "python.exe"), "windows"),
        ):
            with self.subTest(platform=platform):
                mcp, _ = configure_local.configuration(workspace, platform)
                connection = mcp["mcpServers"]["codex_deepseek_collab"]
                self.assertEqual(Path(connection["command"]), ROOT.joinpath(*python_parts))
                state = connection["args"][connection["args"].index("--state-root") + 1]
                self.assertEqual(Path(state), workspace / "runtime" / state_name / "state")
                self.assertEqual(len(connection["enabled_tools"]), 7)
        actual = json.loads((ROOT / "plugin/.mcp.json").read_text(encoding="utf-8"))
        actual_connection = actual["mcpServers"]["codex_deepseek_collab"]
        expected, _ = configure_local.configuration(workspace, sys.platform, Path(actual_connection["cwd"]))
        self.assertEqual(actual, expected)
        custom, _ = configure_local.configuration(workspace, sys.platform, workspace / "current-workspace")
        connection = custom["mcpServers"]["codex_deepseek_collab"]
        self.assertEqual(connection["cwd"], str((workspace / "current-workspace").resolve()))
        self.assertEqual(connection["args"][-1], str((workspace / "current-workspace").resolve()))


if __name__ == "__main__":
    unittest.main()
