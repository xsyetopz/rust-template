"""Test command coverage and fail-closed behavior without simulating Rust results."""

import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

import check as CHECK


def package(root, name="portable-core", *, portable=True):
    return {
        "id": name,
        "name": name,
        "manifest_path": str(root / "crates" / name / "Cargo.toml"),
        "features": {"default": [], "std": [], "unsafe-code": []} if portable else {},
        "targets": [{"kind": ["lib"]}],
    }


class SettingsTests(unittest.TestCase):
    def test_exact_versions_and_smoke_target(self):
        config = CHECK.settings()
        self.assertEqual(config.rust, "1.98.1")
        self.assertEqual(config.deny, "0.20.2")
        self.assertEqual(config.targets, ("thumbv6m-none-eabi",))

    def test_unknown_metadata_is_an_error(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            content = (CHECK.ROOT / "Cargo.toml").read_text()
            content = content.replace(
                "[workspace.metadata.rust-policy]",
                "[workspace.metadata.rust-policy]\nunrecognized = true",
            )
            (root / "Cargo.toml").write_text(content)
            shutil.copyfile(
                CHECK.ROOT / "rust-toolchain.toml", root / "rust-toolchain.toml"
            )
            with self.assertRaises(CHECK.PolicyError):
                CHECK.settings(root)

    def test_duplicate_values_are_an_error(self):
        with self.assertRaises(CHECK.PolicyError):
            CHECK.string_list(["a", "a"], "names")

    def test_version_mismatch_is_an_error(self):
        with self.assertRaises(CHECK.PolicyError):
            CHECK.require_version("rustc 1.97.0 (hash)", "rustc", "1.98.1")

    def test_exact_version_is_accepted(self):
        CHECK.require_version("rustc 1.98.1 (hash date)", "rustc", "1.98.1")


class FlagTests(unittest.TestCase):
    def test_lint_overrides_are_rejected(self):
        for flag in (
            "-Awarnings",
            "-A",
            "--allow=unused",
            "--expect=unused",
            "--cap-lints",
            "--cap-lints=allow",
            "--force-warn=unused",
        ):
            with self.subTest(flag=flag), self.assertRaises(CHECK.PolicyError):
                CHECK.check_flags([flag], "test")

    def test_encoded_lint_override_is_rejected(self):
        with self.assertRaises(CHECK.PolicyError):
            CHECK.check_environment({"CARGO_ENCODED_RUSTFLAGS": "--cap-lints\x1fallow"})

    def test_native_codegen_flags_are_preserved(self):
        CHECK.check_environment({"RUSTFLAGS": "-C target-cpu=cortex-m0 -D warnings"})

    def test_external_clippy_configuration_is_rejected(self):
        with (
            tempfile.TemporaryDirectory() as directory,
            self.assertRaises(CHECK.PolicyError),
        ):
            CHECK.check_environment({"CLIPPY_CONF_DIR": directory})


class SourceStructureTests(unittest.TestCase):
    def source(self, text: str) -> list[int]:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "module.rs"
            path.write_text(text)
            return CHECK.inline_test_modules(path)

    def test_rejects_same_line_inline_test_module(self):
        self.assertEqual(self.source("#[cfg(test)] mod tests {}\n"), [1])

    def test_rejects_multiline_inline_test_module(self):
        source = '#[cfg(\n    all(feature = "std", test)\n)]\nmod checks\n{\n}\n'
        self.assertEqual(self.source(source), [1])

    def test_rejects_nonstandard_name_and_nested_cfg(self):
        source = (
            '#[cfg(any(feature = "host", all(test, unix)))]\nmod verification { }\n'
        )
        self.assertEqual(self.source(source), [1])

    def test_accepts_external_and_explicit_path_modules(self):
        source = (
            "#[cfg(test)]\nmod tests;\n\n#[cfg(test)]\n"
            '#[path = "other_tests.rs"]\nmod other;\n'
        )
        self.assertEqual(self.source(source), [])

    def test_ignores_test_modules_in_comments_and_literals(self):
        source = r"""// #[cfg(test)] mod line {}
/* #[cfg(all(test, unix))] mod block {} */
const NORMAL: &str = "#[cfg(test)] mod string {}";
const RAW: &str = r###"#[cfg(test)] mod raw {}"###;
const BYTE: &[u8] = br##"#[cfg(test)] mod bytes {}"##;
const CHARACTER: char = '}';
"""
        self.assertEqual(self.source(source), [])

    def test_attributes_belong_only_to_the_following_item(self):
        source = (
            "#[cfg(unix)] mod production {}\n"
            "#[cfg(test)] fn helper() {}\n"
            "#[cfg(unix)] mod unrelated {}\n"
            "#[cfg(test)] mod external;\n"
            "#[cfg(unix)] mod also_unrelated {}\n"
        )
        self.assertEqual(self.source(source), [])

    def test_balanced_attributes_and_raw_identifiers(self):
        source = (
            '\n#[cfg(all(test, any(unix, target_os = "none")))]\n'
            '#[doc = "brackets ] and mod ignored {}"]\n'
            '#[cfg_attr(feature = "extra", allow(dead_code))]\n'
            "pub(crate) mod r#type { }\n"
            '#[cfg_attr(feature = "extra", cfg(any(test, unix)))]\n'
            '#[path = "ignored.rs"]\nmod r#match {}\n'
            '#[cfg_attr(unix, cfg_attr(feature = "extra", cfg(test)))]\n'
            "mod nested {}\n"
        )
        self.assertEqual(self.source(source), [2, 6, 9])

    def test_cfg_attr_without_test_gating_and_literal_test_are_ignored(self):
        source = (
            "#[cfg_attr(test, allow(dead_code))] mod ordinary {}\n"
            '#[cfg(feature = "test")] mod feature {}\n'
            '#[cfg(test = "value")] mod key_value {}\n'
            '#[doc = "test"] #[cfg(unix)] mod documented {}\n'
            "#![cfg(test)] mod following_inner_attribute {}\n"
        )
        self.assertEqual(self.source(source), [])

    def test_nested_modules_and_comments_preserve_attribute_lines(self):
        source = (
            "/* outer /* #[cfg(test)] mod fake {} */ */\n"
            "mod parent {\n"
            "    #[cfg(test)] // control\n"
            "    /* gap */ #[allow(dead_code)]\n"
            "    mod child {}\n"
            "}\n"
        )
        self.assertEqual(self.source(source), [3])


class PlanTests(unittest.TestCase):
    def setUp(self):
        self.config = CHECK.settings()
        self.packages = [
            package(CHECK.ROOT),
            package(CHECK.ROOT, "host-app", portable=False),
        ]

    def test_embedded_scope_and_both_safety_modes(self):
        commands = CHECK.command_plan(self.config, self.packages, "embedded")
        self.assertEqual(len(commands), 4)
        for command in commands:
            self.assertIn("--lib", command)
            self.assertIn("no-std", command)
            self.assertIn("thumbv6m-none-eabi", command)
            self.assertNotIn("--workspace", command)
            self.assertNotIn("host-app", command)
            self.assertFalse(any("portable-core/std" in arg for arg in command))
        self.assertEqual(sum("portable-core/unsafe-code" in c for c in commands), 2)
        self.assertEqual(sum(c[1] == "build" for c in commands), 2)

    def test_host_tests_include_doctests_and_use_native_test_profile(self):
        commands = CHECK.command_plan(self.config, self.packages, "host")
        tests = [c for c in commands if c[1] == "test"]
        self.assertEqual(len(tests), 4)
        for command in tests:
            self.assertNotIn("--all-targets", command)
            self.assertNotIn("--profile", command)

    def test_portable_host_checks_are_isolated(self):
        commands = CHECK.command_plan(self.config, self.packages, "host")
        for command in commands[:4]:
            self.assertIn("--package", command)
            self.assertNotIn("--workspace", command)
            self.assertFalse(any("portable-core/std" in arg for arg in command))

    def test_package_qualified_features_do_not_require_host_features(self):
        args = CHECK.feature_args(self.packages, std=True, unsafe=True)
        self.assertEqual(
            args,
            [
                "--no-default-features",
                "--features",
                "portable-core/std,portable-core/unsafe-code",
            ],
        )

    def test_all_commands_use_lockfile_and_no_all_features_shortcut(self):
        for command in CHECK.command_plan(self.config, self.packages, "all"):
            self.assertNotIn("--all-features", command)
            if command[1] != "fmt":
                self.assertIn("--locked", command)

    def test_dependency_graph_is_not_pruned(self):
        commands = CHECK.command_plan(self.config, self.packages, "dependencies")
        self.assertEqual(len(commands), 4)
        for command in commands:
            self.assertIn("--workspace", command)
            self.assertNotIn("--target", command)
            self.assertNotIn("--exclude", command)
            self.assertEqual(command[-3:], ["check", "--deny", "warnings"])

    def test_missing_embedded_coverage_is_an_error(self):
        for config in (
            replace(self.config, portable=()),
            replace(self.config, targets=()),
        ):
            with self.subTest(config=config), self.assertRaises(CHECK.PolicyError):
                CHECK.command_plan(config, self.packages, "embedded")

    def test_multiple_portable_packages_are_checked_separately(self):
        config = replace(self.config, portable=("portable-core", "second-core"))
        commands = CHECK.command_plan(
            config, self.packages + [package(CHECK.ROOT, "second-core")], "embedded"
        )
        self.assertEqual(len(commands), 8)
        for command in commands:
            self.assertEqual(command.count("--package"), 1)

    def test_quick_is_an_explicit_subset(self):
        commands = CHECK.command_plan(self.config, self.packages, "quick")
        self.assertEqual([command[1] for command in commands], ["fmt", "clippy"])


class ProfilePlanTests(unittest.TestCase):
    def setUp(self):
        self.base = CHECK.settings()
        self.packages = [
            package(CHECK.ROOT),
            package(CHECK.ROOT, "device", portable=False),
        ]

    def plan(self, profile, lane):
        config = replace(self.base, profile=profile, profile_packages=("device",))
        return CHECK.command_plan(config, self.packages, lane)

    def test_target_only_packages_leave_every_host_lane(self):
        for profile in ("embedded", "wasm"):
            for lane in ("host", "quick"):
                with self.subTest(profile=profile, lane=lane):
                    for command in self.plan(profile, lane):
                        if "--workspace" in command:
                            index = command.index("--exclude")
                            self.assertEqual(command[index + 1], "device")

    def test_target_only_packages_build_and_link_each_target(self):
        commands = [c for c in self.plan("embedded", "embedded") if "device" in c]
        self.assertEqual([c[1] for c in commands], ["clippy", "build"])
        for command in commands:
            self.assertNotIn("--lib", command)
            self.assertIn("thumbv6m-none-eabi", command)
            self.assertIn("no-std", command)

    def test_target_only_packages_alone_enable_the_embedded_lane(self):
        config = replace(
            self.base, portable=(), profile="wasm", profile_packages=("device",)
        )
        commands = CHECK.command_plan(config, self.packages, "embedded")
        self.assertEqual(len(commands), 2)

    def test_dependency_audit_keeps_profile_packages(self):
        for command in self.plan("embedded", "dependencies"):
            self.assertIn("--workspace", command)
            self.assertNotIn("--exclude", command)

    def test_frontend_builds_before_cargo(self):
        for lane in ("all", "host", "quick"):
            with self.subTest(lane=lane):
                commands = self.plan("tauri", lane)
                self.assertEqual(
                    commands[:3],
                    [
                        ["bun", "install", "--frozen-lockfile"],
                        ["bun", "run", "typecheck"],
                        ["bun", "run", "build"],
                    ],
                )
                self.assertFalse(any("--exclude" in c for c in commands))
        self.assertFalse(any(c[0] == "bun" for c in self.plan("tauri", "embedded")))
        self.assertFalse(any(c[0] == "bun" for c in self.plan("wasm", "all")))

    def test_bun_minimum_version(self):
        CHECK.require_minimum_version("1.4.2\n", (1, 4, 2))
        CHECK.require_minimum_version("1.10.0\n", (1, 4, 2))
        for output in ("1.4.1", "1.3.99", "canary", ""):
            with self.subTest(output=output), self.assertRaises(CHECK.PolicyError):
                CHECK.require_minimum_version(output, (1, 4, 2))

    def test_bun_minimum_requires_an_explicit_engine_range(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for engines, expected in (
                ({"bun": ">=1.4.2"}, (1, 4, 2)),
                ({"bun": "^1.4.2"}, None),
                ({}, None),
            ):
                (root / "package.json").write_text(json.dumps({"engines": engines}))
                with self.subTest(engines=engines):
                    if expected is None:
                        with self.assertRaises(CHECK.PolicyError):
                            CHECK.bun_minimum(root)
                    else:
                        self.assertEqual(CHECK.bun_minimum(root), expected)


class PolicyTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name) / "repository"
        shutil.copytree(
            CHECK.ROOT,
            self.root,
            ignore=shutil.ignore_patterns("__pycache__", "target", ".git"),
        )
        self.config = CHECK.settings(self.root)
        self.packages = [package(self.root)]

    def test_template_manifest_policy(self):
        CHECK.validate_policy(self.root, self.config, self.packages)

    def test_missing_member_lint_inheritance_is_an_error(self):
        path = self.root / "crates" / "portable-core" / "Cargo.toml"
        path.write_text(path.read_text().replace("[lints]\nworkspace = true", ""))
        with self.assertRaises(CHECK.PolicyError):
            CHECK.validate_policy(self.root, self.config, self.packages)

    def test_lint_weakening_is_an_error(self):
        path = self.root / "Cargo.toml"
        path.write_text(
            path.read_text().replace('unwrap_used = "forbid"', 'unwrap_used = "allow"')
        )
        with self.assertRaises(CHECK.PolicyError):
            CHECK.validate_policy(self.root, self.config, self.packages)

    def test_profile_drift_is_an_error(self):
        path = self.root / "Cargo.toml"
        path.write_text(path.read_text() + "\nopt-level = 0\n")
        with self.assertRaises(CHECK.PolicyError):
            CHECK.validate_policy(self.root, self.config, self.packages)

    def test_test_clippy_configuration_drift_is_an_error(self):
        path = self.root / ".clippy-test" / "clippy.toml"
        path.write_text(
            path.read_text().replace(
                "array-size-threshold = 512", "array-size-threshold = 1024"
            )
        )
        with self.assertRaises(CHECK.PolicyError):
            CHECK.validate_policy(self.root, self.config, self.packages)

    def test_missing_lockfile_is_an_error(self):
        (self.root / "Cargo.lock").unlink()
        with self.assertRaises(CHECK.PolicyError):
            CHECK.validate_policy(self.root, self.config, self.packages)

    def test_native_cargo_config_lint_caps_are_rejected(self):
        directory = self.root / ".cargo"
        directory.mkdir()
        (directory / "config.toml").write_text(
            '[build]\nrustflags = ["--cap-lints=allow"]\n'
        )
        with self.assertRaises(CHECK.PolicyError):
            CHECK.validate_policy(self.root, self.config, self.packages)

    def use_profile(self, profile, member_lints):
        manifest = self.root / "Cargo.toml"
        manifest.write_text(
            manifest.read_text().replace(
                'cargo-deny-version = "0.20.2"',
                f'cargo-deny-version = "0.20.2"\nproject-profile = "{profile}"\n'
                'profile-packages = ["device"]',
            )
        )
        device = self.root / "crates" / "device"
        device.mkdir()
        (device / "Cargo.toml").write_text(
            '[package]\nname = "device"\nedition.workspace = true\n'
            f"rust-version.workspace = true\n\n{member_lints}"
        )
        self.packages.append(package(self.root, "device", portable=False))
        self.config = CHECK.settings(self.root)

    def overlay_lints(self, *extra):
        workspace = (self.root / "Cargo.toml").read_text()
        block = workspace[
            workspace.index("[workspace.lints.rust]") : workspace.index(
                "[profile.release]"
            )
        ].replace("[workspace.lints.", "[lints.")
        for name in ("rust_2024_compatibility", *extra):
            block = block.replace(
                f'{name} = {{ level = "forbid"', f'{name} = {{ level = "deny"'
            ).replace(f'{name} = "forbid"', f'{name} = "deny"')
        return block

    def test_profile_lint_overlay_is_accepted(self):
        self.use_profile("embedded", self.overlay_lints())
        CHECK.validate_policy(self.root, self.config, self.packages)

    def test_profile_lint_overlay_beyond_the_profile_is_an_error(self):
        self.use_profile("embedded", self.overlay_lints("unwrap_used"))
        with self.assertRaises(CHECK.PolicyError):
            CHECK.validate_policy(self.root, self.config, self.packages)

    def test_profile_without_lowered_lints_requires_inheritance(self):
        self.use_profile("wasm", self.overlay_lints())
        with self.assertRaises(CHECK.PolicyError):
            CHECK.validate_policy(self.root, self.config, self.packages)
        path = self.root / "crates" / "device" / "Cargo.toml"
        text = path.read_text()
        path.write_text(
            text[: text.index("[lints.rust]")] + "[lints]\nworkspace = true\n"
        )
        CHECK.validate_policy(self.root, self.config, self.packages)

    def test_unknown_or_incomplete_profiles_are_errors(self):
        manifest = self.root / "Cargo.toml"
        original = manifest.read_text()
        for addition in (
            'project-profile = "desktop"\nprofile-packages = ["portable-core"]',
            'project-profile = "wasm"\nprofile-packages = []',
            'project-profile = "wasm"\nprofile-packages = ["portable-core"]',
            'project-profile = "wasm"',
        ):
            manifest.write_text(
                original.replace(
                    'cargo-deny-version = "0.20.2"',
                    f'cargo-deny-version = "0.20.2"\n{addition}',
                )
            )
            with self.subTest(addition=addition), self.assertRaises(CHECK.PolicyError):
                CHECK.settings(self.root)

    def test_dependency_exceptions_require_their_profile(self):
        tauri = CHECK.PROFILES["tauri"]
        base = {
            "licenses": {"allow": ["MIT"]},
            "bans": {"multiple-versions": "deny"},
            "advisories": {"ignore": []},
        }
        CHECK.validate_dependency_policy(base, CHECK.NO_PROFILE)
        exceptions = {
            "licenses": {"allow": ["MIT", "MPL-2.0"]},
            "bans": {"multiple-versions": "allow"},
            "advisories": {
                "ignore": [{"id": "RUSTSEC-2024-0429", "reason": "no upgrade"}]
            },
        }
        CHECK.validate_dependency_policy(exceptions, tauri)
        for key, value in exceptions.items():
            with (
                self.subTest(key=key),
                self.assertRaises(CHECK.PolicyError),
            ):
                CHECK.validate_dependency_policy({**base, key: value}, CHECK.NO_PROFILE)
        for key, value in (
            ("licenses", {"allow": ["GPL-3.0"]}),
            ("advisories", {"ignore": ["RUSTSEC-2020-0001"]}),
        ):
            with self.subTest(key=key), self.assertRaises(CHECK.PolicyError):
                CHECK.validate_dependency_policy({**exceptions, key: value}, tauri)

    def test_missing_workspace_member_is_an_error(self):
        with self.assertRaises(CHECK.PolicyError):
            CHECK.owned_packages({"workspace_members": ["missing"], "packages": []})

    def test_dependency_packages_are_not_workspace_members(self):
        own, external = package(self.root), package(self.root, "external")
        selected = CHECK.owned_packages(
            {"workspace_members": [own["id"]], "packages": [external, own]}
        )
        self.assertEqual(selected, [own])


class ProcessTests(unittest.TestCase):
    def test_first_failure_stops_the_plan(self):
        failure = subprocess.CalledProcessError(17, ["native-tool"])
        with patch.object(CHECK, "run", side_effect=failure) as runner:
            with self.assertRaises(subprocess.CalledProcessError):
                CHECK.execute_plan([["first"], ["second"]])
            self.assertEqual(runner.call_count, 1)

    def test_missing_tool_is_not_success(self):
        with patch.object(CHECK, "run", side_effect=FileNotFoundError("missing tool")):
            self.assertEqual(CHECK.main(["quick"]), 1)

    def test_native_exit_code_is_preserved(self):
        with patch.object(
            CHECK, "run", side_effect=subprocess.CalledProcessError(17, ["rustc"])
        ):
            self.assertEqual(CHECK.main(["quick"]), 17)

    def test_real_failed_subprocess_is_not_success(self):
        with (
            patch.dict(os.environ, {}, clear=True),
            self.assertRaises(subprocess.CalledProcessError) as error,
        ):
            CHECK.run([sys.executable, "-c", "raise SystemExit(19)"])
        self.assertEqual(error.exception.returncode, 19)

    def test_real_successful_subprocess_can_be_captured(self):
        with patch.dict(os.environ, {}, clear=True):
            self.assertEqual(
                CHECK.run([sys.executable, "-c", "print('captured')"], capture=True),
                "captured\n",
            )


if __name__ == "__main__":
    unittest.main()
