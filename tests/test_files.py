from pathlib import Path
import os
import tempfile
import unittest

from collab import files
from collab.common import CollabError


class FilesTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.source = Path(self.temporary.name) / "source"
        self.source.mkdir()

    def put(self, rel):
        path = self.source / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("fixture", encoding="utf-8")
        return path

    def rejected(self, code, function, *args):
        with self.assertRaises(CollabError) as caught:
            function(*args)
        self.assertEqual(caught.exception.code, code)

    def test_relative_normalization(self):
        self.assertEqual(files.validate_rel("./src//模块.py"), "src/模块.py")
        self.assertEqual(files.validate_rel("a/./b/"), "a/b")
        self.assertEqual(files.validate_rel("console.txt"), "console.txt")

    def test_relative_rejects_windows_and_traversal_paths(self):
        invalid = ["", ".", "./", "..", "a/../b", "/etc/file", "C:/x", "a\\b", "a:b",
                   "a\x00b", "a\nb", "NUL", "a/con.txt", "COM1.py", "foo.", "a/*", None, 123]
        for value in invalid:
            with self.subTest(value=value):
                self.rejected("INVALID_PATH", files.validate_rel, value)

    def test_safe_path_missing_descendants_and_case_alias(self):
        self.put("Src/main.py")
        self.assertEqual(files.safe_path(self.source, "Src/new/a.py"), self.source / "Src/new/a.py")
        self.rejected("UNSAFE_PATH", files.safe_path, self.source, "src/main.py")
        self.rejected("UNSAFE_PATH", files.safe_path, self.source, "Src/MAIN.py")
        self.rejected("UNSAFE_PATH", files.safe_path, self.source, "Src/main.py/x")

    def test_safe_path_rejects_case_collisions_when_supported(self):
        self.put("A.txt")
        self.put("a.txt")
        if len(list(self.source.iterdir())) != 2:
            self.skipTest("Filesystem is case insensitive")
        self.rejected("UNSAFE_PATH", files.safe_path, self.source, "A.txt")

    def test_hardlink_is_rejected(self):
        original = self.put("original.txt")
        os.link(original, self.source / "alias.txt")
        self.rejected("UNSAFE_PATH", files.safe_path, self.source, "alias.txt")

    def test_symlink_and_broken_link_are_rejected_when_supported(self):
        self.put("real/file.txt")
        link = self.source / "link"
        try:
            link.symlink_to(self.source / "real", target_is_directory=True)
        except OSError as exc:
            self.skipTest(f"Symlinks unavailable: {exc}")
        self.rejected("UNSAFE_PATH", files.safe_path, self.source, "link/file.txt")
        self.rejected("UNSAFE_PATH", files.safe_path, self.source, "link/missing/x")


if __name__ == "__main__":
    unittest.main()
