import json
from pathlib import Path
import unittest

from tools import configure_local


ROOT = Path(__file__).resolve().parents[1]


class ConfigurationTests(unittest.TestCase):
    def test_platform_layouts_and_current_mac_configuration(self):
        workspace = ROOT.parent
        for platform, python_suffix, state_name in (
            ("darwin", ".venv/bin/python", "macos"),
            ("win32", ".venv/Scripts/python.exe", "windows"),
        ):
            with self.subTest(platform=platform):
                mcp, _ = configure_local.configuration(workspace, platform)
                connection = mcp["mcpServers"]["codex_deepseek_collab"]
                self.assertTrue(connection["command"].endswith(python_suffix))
                state = connection["args"][connection["args"].index("--state-root") + 1]
                self.assertIn(state_name, state)
                self.assertEqual(len(connection["enabled_tools"]), 6)
        actual = json.loads((ROOT / "plugin/.mcp.json").read_text(encoding="utf-8"))
        actual_connection = actual["mcpServers"]["codex_deepseek_collab"]
        expected, _ = configure_local.configuration(workspace, "darwin", Path(actual_connection["cwd"]))
        self.assertEqual(actual, expected)
        custom, _ = configure_local.configuration(workspace, "darwin", workspace / "current-workspace")
        connection = custom["mcpServers"]["codex_deepseek_collab"]
        self.assertEqual(connection["cwd"], str((workspace / "current-workspace").resolve()))
        self.assertEqual(connection["args"][-1], str((workspace / "current-workspace").resolve()))


if __name__ == "__main__":
    unittest.main()
