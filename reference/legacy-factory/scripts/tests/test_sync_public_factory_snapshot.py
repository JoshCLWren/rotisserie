"""Tests for the allowlisted public factory snapshot synchronizer."""

import tempfile
import unittest
from importlib.util import module_from_spec, spec_from_file_location
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "scripts" / "sync_public_factory_snapshot.py"
SPEC = spec_from_file_location("sync_public_factory_snapshot", SCRIPT)
if SPEC is None or SPEC.loader is None:
    raise RuntimeError(f"Unable to load {SCRIPT}")
SYNC = module_from_spec(SPEC)
SPEC.loader.exec_module(SYNC)


class SyncPublicFactorySnapshotTests(unittest.TestCase):
    """Verify allowlisted, deterministic synchronization behavior."""

    def test_sync_copies_only_allowlisted_files_and_detects_drift(self) -> None:
        """Copy mapped files while preserving unrelated target content."""
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "source"
            target = root / "target"
            for index, (source_relative, target_relative) in enumerate(SYNC.FILE_MAP.items()):
                source_path = source / source_relative
                source_path.parent.mkdir(parents=True, exist_ok=True)
                source_path.write_text(f"canonical-{index}\n", encoding="utf-8")
                target_path = target / target_relative
                target_path.parent.mkdir(parents=True, exist_ok=True)
                target_path.write_text("stale\n", encoding="utf-8")

            unrelated = target / "README.md"
            unrelated.write_text("preserve me\n", encoding="utf-8")

            drift = SYNC.sync_snapshot(source, target, check=True)
            self.assertEqual(set(drift), set(SYNC.FILE_MAP.values()))

            changed = SYNC.sync_snapshot(source, target)
            self.assertEqual(set(changed), set(SYNC.FILE_MAP.values()))
            self.assertEqual(SYNC.sync_snapshot(source, target, check=True), [])
            self.assertEqual(unrelated.read_text(encoding="utf-8"), "preserve me\n")

    def test_sync_rejects_source_symlinks(self) -> None:
        """Prevent an allowlisted path from redirecting to unintended private data."""
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "source"
            target = root / "target"
            first_source = next(iter(SYNC.FILE_MAP))
            source_path = source / first_source
            source_path.parent.mkdir(parents=True, exist_ok=True)
            private = root / "private"
            private.write_text("secret\n", encoding="utf-8")
            source_path.symlink_to(private)

            with self.assertRaisesRegex(ValueError, "Unsafe or missing source"):
                SYNC.sync_snapshot(source, target)


if __name__ == "__main__":
    unittest.main()
