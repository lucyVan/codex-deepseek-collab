from pathlib import Path
import json
import unittest


ROOT = Path(__file__).resolve().parents[1]
BUNDLE = ROOT / "dsh-presets" / "bundle"
EXPECTED_IDS = ["collab-worker"]


class DshBundleTests(unittest.TestCase):
    def test_bundle_declares_worker_preset_without_shared_markers(self):
        manifest = json.loads((BUNDLE / "package.json").read_text(encoding="utf-8"))
        self.assertEqual(manifest["name"], "@collab-local/codex-deepseek-presets")
        self.assertEqual(manifest["version"], "0.7.0")
        self.assertEqual(manifest["dsh"]["bundle"]["patch"], "./cordis.patch.yml")
        patch = (BUNDLE / "cordis.patch.yml").read_text(encoding="utf-8")
        self.assertTrue(patch.startswith("- insert:\n"))
        self.assertNotIn("# @shared", patch)
        row_ids = [line.strip() for line in patch.splitlines()
                   if line.startswith("    - id: preset-")]
        self.assertEqual(row_ids, [f"- id: preset-{preset_id}" for preset_id in EXPECTED_IDS])
        preset_ids = [line.split("id: ", 1)[1] for line in patch.splitlines()
                      if line.startswith("        id: ")]
        self.assertEqual(preset_ids, EXPECTED_IDS)
        self.assertEqual(len(preset_ids), len(set(preset_ids)))

    def test_worker_has_full_tools_and_no_delegation(self):
        patch = (BUNDLE / "cordis.patch.yml").read_text(encoding="utf-8")
        self.assertEqual(patch.count("          - id: tool-bash\n"), 1)
        self.assertEqual(patch.count("process.platform !== 'darwin'"), 1)
        self.assertEqual(patch.count("          - id: tool-pwsh\n"), 1)
        self.assertEqual(patch.count("name: '@deepseek-ai/dsh-tool-pwsh'"), 1)
        self.assertEqual(patch.count("process.platform !== 'win32'"), 1)
        self.assertEqual(patch.count("          - id: tool-web\n"), 1)
        self.assertNotIn("- id: delegation", patch)


if __name__ == "__main__":
    unittest.main()
