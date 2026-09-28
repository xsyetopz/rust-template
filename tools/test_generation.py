"""End-to-end validation for every supported generated-project shape."""

from __future__ import annotations

import itertools
import json
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

import bootstrap
import tomllib

ROOT = Path(__file__).resolve().parents[1]
BOOTSTRAP = ROOT / "tools" / "bootstrap.py"
AUTHOR = "Ada Lovelace"
YEAR = "1843"
FORBIDDEN = ("portable-core", "portable_core", "template-rust")


def cargo() -> str:
    return subprocess.run(
        ["rustup", "which", "--toolchain", "1.98.1", "cargo"],
        check=True,
        text=True,
        stdout=subprocess.PIPE,
    ).stdout.strip()


def generate(
    destination: Path,
    *,
    shape: str | None = None,
    role: str | None = None,
    mode: str | None = None,
    profile: str = "crate",
    name: str = "sample-project",
) -> None:
    layout = []
    for flag, value in (("--shape", shape), ("--role", role), ("--mode", mode)):
        if value is not None:
            layout += [flag, value]
    subprocess.run(
        [
            sys.executable,
            str(BOOTSTRAP),
            "--name",
            name,
            "--author",
            AUTHOR,
            "--year",
            YEAR,
            "--profile",
            profile,
            *layout,
            "--output",
            str(destination),
        ],
        cwd=ROOT,
        check=True,
    )


def metadata(project: Path) -> dict:
    return json.loads(
        subprocess.run(
            [cargo(), "metadata", "--locked", "--no-deps", "--format-version", "1"],
            cwd=project,
            check=True,
            text=True,
            stdout=subprocess.PIPE,
        ).stdout
    )


def full_gate(project: Path) -> str:
    return subprocess.run(
        [sys.executable, "tools/check.py"],
        cwd=project,
        check=True,
        text=True,
        stdout=subprocess.PIPE,
    ).stdout


def files(root: Path) -> dict[str, bytes]:
    return {
        str(path.relative_to(root)): path.read_bytes()
        for path in sorted(root.rglob("*"))
        if path.is_file() and ".git" not in path.parts
    }


class RendererInputTests(unittest.TestCase):
    def test_author_round_trips_through_toml(self):
        with tempfile.TemporaryDirectory() as temporary:
            values = Path(temporary) / "values"
            for author in (
                "  Zoë 李  ",
                'Ada "Countess"',
                "Ada\\Lovelace",
                "Ada\u2028Lovelace",
            ):
                values.write_text(f"sample\n{author}\n1843\ncrate\nroot\nlib\nhost\n")
                for arguments in (
                    ["--name", "sample", "--author", author],
                    ["--cargo-generate-values", str(values)],
                ):
                    with self.subTest(author=author, arguments=arguments):
                        options, *_ = bootstrap.parse_options(arguments)
                        manifest = tomllib.loads(
                            bootstrap.render(options)["Cargo.toml"].decode()
                        )
                        self.assertEqual(
                            manifest["package"]["authors"], [author.strip()]
                        )

    def test_all_ascii_controls_are_rejected_by_both_input_paths(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            values = root / "values"
            for code in (*range(32), 127):
                author = f"Ada{chr(code)}Lovelace"
                values.write_text(f"sample\n{author}\n1843\ncrate\nroot\nlib\nhost\n")
                for arguments in (
                    ["--name", "sample", "--author", author],
                    ["--cargo-generate-values", str(values)],
                ):
                    with (
                        self.subTest(code=code, arguments=arguments),
                        self.assertRaises(bootstrap.BootstrapError),
                    ):
                        bootstrap.parse_options(arguments)

    def test_rejected_author_preserves_destination_including_dry_run(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            destination = root / "existing"
            destination.mkdir()
            (destination / "sentinel").write_bytes(b"unchanged")
            before = files(destination)
            values = root / "values"
            values.write_text("sample\nAda\tLovelace\n1843\ncrate\nroot\nlib\nhost\n")
            for arguments in (
                ["--name", "sample", "--author", "Ada\tLovelace"],
                ["--cargo-generate-values", str(values)],
            ):
                for extra in ([], ["--dry-run"], ["--replace-cargo-generate-template"]):
                    result = subprocess.run(
                        [
                            sys.executable,
                            str(BOOTSTRAP),
                            *arguments,
                            "--output",
                            str(destination),
                            *extra,
                        ],
                        capture_output=True,
                        text=True,
                        check=False,
                    )
                    self.assertEqual(result.returncode, 1)
                    self.assertIn("Remove them and retry", result.stderr)
                    self.assertEqual(files(destination), before)

    def test_crate_layout_flags_are_rejected_for_other_profiles(self):
        for profile in ("tauri", "wasm", "embedded"):
            for flag, value in (
                ("--shape", "root"),
                ("--role", "lib"),
                ("--mode", "host"),
            ):
                with (
                    self.subTest(profile=profile, flag=flag),
                    self.assertRaises(bootstrap.BootstrapError),
                ):
                    bootstrap.parse_options(
                        [
                            "--name",
                            "s",
                            "--author",
                            "A",
                            "--profile",
                            profile,
                            flag,
                            value,
                        ]
                    )

    def test_cargo_generate_values_leave_crate_layout_unset_for_other_profiles(self):
        with tempfile.TemporaryDirectory() as temporary:
            values = Path(temporary) / "values"
            for profile in ("tauri", "wasm", "embedded"):
                with self.subTest(profile=profile):
                    values.write_text(f"sample\nAda\n1843\n{profile}\n\n\n\n")
                    options, *_ = bootstrap.parse_options(
                        ["--cargo-generate-values", str(values)]
                    )
                    self.assertEqual(options.profile, profile)
                    values.write_text(f"sample\nAda\n1843\n{profile}\nroot\n\n\n")
                    with self.assertRaises(bootstrap.BootstrapError):
                        bootstrap.parse_options(
                            ["--cargo-generate-values", str(values)]
                        )

    def test_profile_exceptions_match_the_gate(self):
        for profile in ("tauri", "wasm", "embedded"):
            with self.subTest(profile=profile):
                options, *_ = bootstrap.parse_options(
                    ["--name", "sample", "--author", "A", "--profile", profile]
                )
                rendered = bootstrap.render(options)
                root = tomllib.loads(rendered["Cargo.toml"].decode())
                package = options.profile_package
                member = next(
                    tomllib.loads(content.decode())
                    for name, content in rendered.items()
                    if name.endswith("Cargo.toml")
                    and tomllib.loads(content.decode()).get("package", {}).get("name")
                    == package
                )
                overlay = bootstrap.check.PROFILES[profile]
                expected = bootstrap.check.profile_lints(
                    root["workspace"]["lints"], overlay
                )
                self.assertEqual(
                    member["lints"],
                    {"workspace": True} if expected is None else expected,
                )
                bootstrap.check.validate_dependency_policy(
                    tomllib.loads(rendered["deny.toml"].decode()), overlay
                )

    def test_renderer_import_and_execution_use_the_gate_interpreter(self):
        subprocess.run(
            [
                sys.executable,
                "-c",
                "import bootstrap; assert bootstrap.main(['--name', 'sample', '--author', 'Ada', '--dry-run']) == 0",
            ],
            cwd=ROOT / "tools",
            check=True,
            capture_output=True,
        )


class GenerationTests(unittest.TestCase):
    def test_every_supported_combination_has_valid_metadata_and_lockfile(self):
        for shape, role, mode in itertools.product(
            ("root", "workspace"), ("lib", "bin", "both"), ("host", "portable")
        ):
            if role == "bin" and mode == "portable":
                continue
            with (
                self.subTest(shape=shape, role=role, mode=mode),
                tempfile.TemporaryDirectory() as temporary,
            ):
                project = Path(temporary) / "project"
                generate(project, shape=shape, role=role, mode=mode)
                metadata = json.loads(
                    subprocess.run(
                        [
                            cargo(),
                            "metadata",
                            "--locked",
                            "--no-deps",
                            "--format-version",
                            "1",
                        ],
                        cwd=project,
                        check=True,
                        text=True,
                        stdout=subprocess.PIPE,
                    ).stdout
                )
                self.assertEqual(
                    [package["name"] for package in metadata["packages"]],
                    ["sample-project"],
                )
                self.assertIn(
                    'name = "sample-project"', (project / "Cargo.lock").read_text()
                )
                package = (
                    project
                    if shape == "root"
                    else project / "crates" / "sample-project"
                )
                self.assertEqual(
                    (package / "src/lib.rs").exists(), role in {"lib", "both"}
                )
                self.assertEqual(
                    (package / "src/main.rs").exists(), role in {"bin", "both"}
                )
                manifest = (project / "Cargo.toml").read_text()
                toolchain = (project / "rust-toolchain.toml").read_text()
                if mode == "host":
                    self.assertNotIn("unsafe-code", manifest)
                    self.assertNotIn("profile.no-std", manifest)
                    self.assertNotIn("targets =", toolchain)
                else:
                    self.assertIn("unsafe-code", (package / "Cargo.toml").read_text())
                    self.assertIn("profile.no-std", manifest)
                    self.assertIn("thumbv6m-none-eabi", toolchain)

    def test_every_profile_has_valid_metadata_and_lockfiles(self):
        expected = {
            "embedded": (
                ["sample-project", "sample-project-firmware"],
                "thumbv6m-none-eabi",
            ),
            "wasm": (["sample-project", "sample-project-wasm"], "wasm32v1-none"),
            "tauri": (["sample-project"], None),
        }
        for profile, (packages, target) in expected.items():
            with (
                self.subTest(profile=profile),
                tempfile.TemporaryDirectory() as temporary,
            ):
                project = Path(temporary) / "project"
                generate(project, profile=profile)
                self.assertEqual(
                    sorted(p["name"] for p in metadata(project)["packages"]), packages
                )
                manifest = tomllib.loads((project / "Cargo.toml").read_text())
                policy = manifest["workspace"]["metadata"]["rust-policy"]
                self.assertEqual(policy["project-profile"], profile)
                self.assertEqual(policy["profile-packages"], [packages[-1]])
                toolchain = tomllib.loads((project / "rust-toolchain.toml").read_text())
                workflow = (project / ".github/workflows/check.yml").read_text()
                if target is None:
                    self.assertNotIn("targets", toolchain["toolchain"])
                    self.assertNotIn("targets:", workflow)
                else:
                    self.assertEqual(toolchain["toolchain"]["targets"], [target])
                    self.assertIn(f"targets: {target}\n", workflow)
                    self.assertEqual(
                        manifest["workspace"]["default-members"],
                        ["crates/sample-project"],
                    )
                self.assertEqual((project / "bun.lock").exists(), profile == "tauri")
                self.assertEqual("setup-bun" in workflow, profile == "tauri")
                self.assertEqual("webkit2gtk" in workflow, profile == "tauri")
                self.assertTrue(
                    (project / ".agents/skills").is_dir(),
                    "generated projects must ship the agent skills",
                )

    def test_portable_binary_only_is_rejected_for_both_shapes(self):
        for shape in ("root", "workspace"):
            with self.subTest(shape=shape), tempfile.TemporaryDirectory() as temporary:
                result = subprocess.run(
                    [
                        sys.executable,
                        str(BOOTSTRAP),
                        "--name",
                        "sample-project",
                        "--author",
                        AUTHOR,
                        "--shape",
                        shape,
                        "--role",
                        "bin",
                        "--mode",
                        "portable",
                        "--output",
                        str(Path(temporary) / "project"),
                    ],
                    check=False,
                    cwd=ROOT,
                    text=True,
                    capture_output=True,
                )
                self.assertNotEqual(result.returncode, 0)
                self.assertIn(
                    "runtime, linker script, and board configuration", result.stderr
                )

    def test_dry_run_changes_no_bytes(self):
        with tempfile.TemporaryDirectory() as temporary:
            destination = Path(temporary) / "existing"
            destination.mkdir()
            (destination / "sentinel").write_bytes(b"unchanged\x00content")
            before = files(destination)
            subprocess.run(
                [
                    sys.executable,
                    str(BOOTSTRAP),
                    "--name",
                    "sample-project",
                    "--author",
                    AUTHOR,
                    "--dry-run",
                    "--output",
                    str(destination),
                ],
                cwd=ROOT,
                check=True,
            )
            self.assertEqual(files(destination), before)

    def test_generated_tree_has_no_template_artifacts_or_leaks(self):
        with tempfile.TemporaryDirectory() as temporary:
            project = Path(temporary) / "project"
            generate(
                project,
                shape="workspace",
                role="both",
                mode="portable",
                name="clean-room",
            )
            generated = files(project)
            self.assertFalse(
                any(
                    name.endswith(
                        ("bootstrap.py", "adopt.py", "cargo-generate.toml", ".rhai")
                    )
                    for name in generated
                )
            )
            for name, content in generated.items():
                if name == "LICENSE" or b"\0" in content:
                    continue
                text = content.decode()
                for forbidden in FORBIDDEN:
                    self.assertNotIn(
                        forbidden, text, f"{forbidden!r} leaked into {name}"
                    )

    def test_library_package_listing_excludes_generator_files(self):
        with tempfile.TemporaryDirectory() as temporary:
            project = Path(temporary) / "project"
            generate(project, shape="root", role="lib", mode="host")
            listing = subprocess.run(
                [cargo(), "package", "--locked", "--list"],
                cwd=project,
                check=True,
                text=True,
                stdout=subprocess.PIPE,
            ).stdout.splitlines()
            excluded = (
                "bootstrap.py",
                "adopt.py",
                "test_generation.py",
                "cargo-generate.toml",
                ".rhai",
            )
            self.assertFalse(
                any(any(fragment in path for fragment in excluded) for path in listing)
            )

    def test_cargo_generate_uses_the_canonical_renderer(self):
        executable = shutil.which("cargo-generate")
        if executable is None:
            self.fail("cargo-generate 0.24.0 is required")
        version = subprocess.run(
            [executable, "--version"], check=True, text=True, stdout=subprocess.PIPE
        ).stdout
        self.assertIn("0.24.0", version)
        cases = (
            {"shape": "workspace", "role": "both", "mode": "portable"},
            {"profile": "wasm"},
        )
        for layout in cases:
            with (
                self.subTest(**layout),
                tempfile.TemporaryDirectory() as temporary,
            ):
                parent = Path(temporary)
                direct = parent / "direct"
                generate(direct, name="equivalent-project", **layout)
                template = parent / "template"
                shutil.copytree(
                    ROOT,
                    template,
                    ignore=shutil.ignore_patterns(
                        ".git", "target", "__pycache__", "*.pyc"
                    ),
                )
                cargo_generate_destination = parent / "cargo-generate"
                cargo_generate_destination.mkdir()
                defines = []
                for key, value in {"profile": "crate", **layout}.items():
                    defines += ["--define", f"{key}={value}"]
                subprocess.run(
                    [
                        executable,
                        "generate",
                        "--path",
                        str(template),
                        "--name",
                        "equivalent-project",
                        "--destination",
                        str(cargo_generate_destination),
                        "--vcs",
                        "none",
                        "--silent",
                        "--allow-commands",
                        "--define",
                        f"author={AUTHOR}",
                        "--define",
                        f"year={YEAR}",
                        *defines,
                    ],
                    cwd=parent,
                    check=True,
                )
                rendered = cargo_generate_destination / "equivalent-project"
                self.assertEqual(files(rendered), files(direct))

    def test_representative_generated_projects_pass_full_gate(self):
        cases = (
            ("root", "bin", "host"),
            ("workspace", "lib", "host"),
            ("root", "both", "portable"),
        )
        for shape, role, mode in cases:
            with (
                self.subTest(shape=shape, role=role, mode=mode),
                tempfile.TemporaryDirectory() as temporary,
            ):
                project = Path(temporary) / "project"
                generate(project, shape=shape, role=role, mode=mode)
                output = full_gate(project)
                if mode == "host":
                    self.assertIn(
                        "Host-only configuration. No embedded compilation occurred.",
                        output,
                    )
                    self.assertNotIn("Cross-target packages compiled", output)
                else:
                    self.assertIn("Cross-target packages compiled for ", output)
                    self.assertNotIn("Host-only configuration.", output)
                self.assertIn("No hardware execution occurred.", output)

    def test_every_profile_passes_full_gate(self):
        coverage = {
            "embedded": "Cross-target packages compiled for thumbv6m-none-eabi.",
            "wasm": "Cross-target packages compiled for wasm32v1-none.",
            "tauri": "Host-only configuration. No embedded compilation occurred.",
        }
        for profile, message in coverage.items():
            with (
                self.subTest(profile=profile),
                tempfile.TemporaryDirectory() as temporary,
            ):
                project = Path(temporary) / "project"
                generate(project, profile=profile)
                output = full_gate(project)
                self.assertIn(message, output)
                self.assertIn("No hardware execution occurred.", output)


if __name__ == "__main__":
    unittest.main()
