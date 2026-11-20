"""Public source preparation cannot inherit research files or Git history."""
from pathlib import Path
import tempfile
import unittest

from scripts import prepare_pages_repository as publishing


class PublicRepositoryTests(unittest.TestCase):
    def fixture(self, root: Path) -> None:
        for name in publishing.PUBLIC_SOURCE_FILES:
            path = root / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(f"reviewed source: {name}\n")
        for name in (".git/config", "archive/private.ipynb", "research.csv",
                     ".env", ".cache/water.json", "web/not-reviewed.txt"):
            path = root / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text("private sentinel")

    def test_only_allowlisted_source_enters_a_fresh_checkout(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "source"
            self.fixture(root)
            output = Path(temporary) / "public"
            copied = publishing.prepare(output, source_root=root)
            expected = set(publishing.PUBLIC_SOURCE_FILES) | {".gitignore", ".github/workflows/pages.yml"}
            self.assertEqual(set(copied), expected)
            self.assertEqual({p.relative_to(output).as_posix() for p in output.rglob("*") if p.is_file()}, expected)
            self.assertFalse((output / ".git").exists())
            self.assertFalse((output / "archive").exists())
            self.assertEqual((output / ".github/workflows/pages.yml").read_bytes(),
                             (root / "deploy/github-pages/pages.yml").read_bytes())

    def test_nonempty_destination_is_not_overwritten(self):
        with tempfile.TemporaryDirectory() as temporary:
            output = Path(temporary)
            sentinel = output / "keep.txt"
            sentinel.write_text("keep")
            with self.assertRaisesRegex(ValueError, "empty"):
                publishing.prepare(output)
            self.assertEqual(sentinel.read_text(), "keep")

    def test_allowlisted_symlink_cannot_publish_private_contents(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "source"
            self.fixture(root)
            path = root / "web/app.js"
            path.unlink()
            path.symlink_to(root / ".env")
            output = Path(temporary) / "public"
            with self.assertRaisesRegex(ValueError, "symlink"):
                publishing.prepare(output, source_root=root)
            self.assertFalse(output.exists())


if __name__ == "__main__":
    unittest.main()
