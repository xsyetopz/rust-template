"""Behavior tests for the read-only adoption analyzer."""

from __future__ import annotations

import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
ADOPT = ROOT / "tools" / "adopt.py"


def snapshot(root: Path) -> dict[str, bytes]:
    return {
        str(path.relative_to(root)): path.read_bytes()
        for path in root.rglob("*")
        if path.is_file()
    }


def run(repository: Path, *arguments: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(ADOPT), "--dry-run", *arguments, str(repository)],
        cwd=ROOT,
        text=True,
        capture_output=True,
        check=False,
    )


class AdoptionTests(unittest.TestCase):
    def test_root_package_report_is_actionable_and_nonmutating(self):
        with tempfile.TemporaryDirectory() as temporary:
            repository = Path(temporary) / "root-package"
            (repository / "src").mkdir(parents=True)
            (repository / "Cargo.toml").write_text(
                '[package]\nname = "existing-app"\nversion = "0.1.0"\nedition = "2021"\nlicense = "Apache-2.0"\n'
            )
            (repository / "src" / "lib.rs").write_text(
                "pub fn implementation() {}\n#[cfg(any(test, unix))] mod custom { #[test] fn works() {} }\n"
            )
            (repository / "LICENSE-APACHE").write_text("existing license\n")
            before = snapshot(repository)
            result = run(
                repository,
                "--portable-package",
                "existing-app",
                "--target",
                "thumbv7em-none-eabi",
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(snapshot(repository), before)
            for expected in (
                "standalone root package",
                "missing [workspace]",
                "inline cfg(test) module",
                "implementation in wiring file",
                "LICENSE-APACHE",
                "proposed portable-packages",
                "No files were modified",
            ):
                self.assertIn(expected, result.stdout)

    def test_virtual_workspace_reports_members_collisions_and_external_paths(self):
        with tempfile.TemporaryDirectory() as temporary:
            parent = Path(temporary)
            repository = parent / "workspace"
            member = repository / "crates" / "owned"
            external = parent / "shared"
            (member / "src").mkdir(parents=True)
            external.mkdir()
            (repository / ".github" / "workflows").mkdir(parents=True)
            (repository / ".cargo").mkdir()
            (repository / "Cargo.toml").write_text(
                '[workspace]\nresolver = "3"\nmembers = ["crates/owned", "../shared"]\n'
                '[workspace.package]\nedition = "2021"\nrust-version = "1.85"\n'
            )
            (member / "Cargo.toml").write_text(
                '[package]\nname = "owned"\nversion = "0.1.0"\n'
            )
            (member / "src" / "lib.rs").write_text("mod engine;\n")
            (external / "Cargo.toml").write_text(
                '[package]\nname = "shared"\nversion = "0.1.0"\n'
            )
            (repository / "clippy.toml").write_text("check-private-items = true\n")
            (repository / ".github" / "workflows" / "ci.yml").write_text(
                "name: existing\n"
            )
            (repository / ".cargo" / "config.toml").write_text(
                '[target.thumbv7em-none-eabi]\nrunner = "probe-rs run"\n'
            )
            before = snapshot(parent)
            result = run(repository)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(snapshot(parent), before)
            for expected in (
                "virtual workspace",
                "external member requires explicit integration",
                "workspace.package.edition",
                "clippy.toml",
                "existing CI workflows to preserve: 1",
                ".cargo/config.toml",
            ):
                self.assertIn(expected, result.stdout)

    def test_member_exclusions_unmatched_declarations_and_resolved_paths(self):
        import adopt

        with tempfile.TemporaryDirectory() as temporary:
            parent = Path(temporary).resolve()
            root = parent / "workspace"
            for name in ("kept", "excluded-one", "excluded-two"):
                (root / "crates" / name).mkdir(parents=True)
            external = parent / "shared"
            external.mkdir()
            (root / "crates" / "linked").symlink_to(external, target_is_directory=True)
            manifest = {
                "workspace": {
                    "members": ["crates/*", "../shared", "missing/*", "absent"],
                    "exclude": ["crates/excluded-*"],
                }
            }
            before = snapshot(parent)
            members, outside, unmatched = adopt.workspace_members(
                root / "crates" / "..", manifest
            )
            self.assertEqual(members, [root / "crates" / "kept"])
            self.assertEqual(outside, [external])
            self.assertEqual(unmatched, ["missing/*", "absent"])
            self.assertEqual(snapshot(parent), before)

    def test_unmatched_declarations_are_reported_without_inflating_count(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "Cargo.toml").write_text(
                '[workspace]\nmembers = ["missing/*", "absent"]\n'
            )
            result = run(root)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn("internal declared-member roots discovered: 0", result.stdout)
            self.assertIn("unmatched member declaration: missing/*", result.stdout)
            self.assertIn("unmatched member declaration: absent", result.stdout)
            self.assertIn(
                "does not resolve the complete Cargo dependency graph", result.stdout
            )

    def test_wiring_diagnostics_start_at_the_declaration(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "Cargo.toml").write_text('[package]\nname = "existing"\n')
            (root / "lib.rs").write_text(
                "\n\n// comment\n#[derive(Debug)]\n\n  pub struct Item;\n"
                "\n\nconst VALUE: u8 = 1;\n"
            )
            result = run(root)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn("lib.rs:6: implementation in wiring file", result.stdout)
            self.assertIn("lib.rs:9: implementation in wiring file", result.stdout)

    def test_missing_manifest_fails_without_modification(self):
        with tempfile.TemporaryDirectory() as temporary:
            repository = Path(temporary)
            sentinel = repository / "sentinel"
            sentinel.write_bytes(b"unchanged")
            before = snapshot(repository)
            result = run(repository)
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("no Cargo.toml", result.stderr)
            self.assertEqual(snapshot(repository), before)


if __name__ == "__main__":
    unittest.main()
