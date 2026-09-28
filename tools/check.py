#!/usr/bin/env python3
"""Run the repository's native Rust checks. Requires Python 3.11+ and rustup."""

from __future__ import annotations

import argparse
import json
import os
import re
import shlex
import subprocess
import sys
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import tomllib

ROOT = Path(__file__).resolve().parents[1]
TOOLCHAIN_BIN: Path | None = None


class PolicyError(Exception):
    """A required setting is missing, unsupported, or weakens enforcement."""


def read_toml(path: Path) -> dict[str, Any]:
    with path.open("rb") as source:
        return tomllib.load(source)


def string_list(value: Any, name: str) -> tuple[str, ...]:
    if not isinstance(value, list) or any(
        not isinstance(v, str) or not v for v in value
    ):
        raise PolicyError(f"{name} must be a list of nonempty strings")
    if len(value) != len(set(value)):
        raise PolicyError(f"{name} contains duplicates")
    return tuple(value)


@dataclass(frozen=True)
class ProjectProfile:
    """Fixed exceptions that one project profile needs from the shared policy."""

    # Profile packages compile only for rust-toolchain.toml targets, never for the host.
    target_only: bool
    # Workspace lints that profile packages lower from forbid to deny, by namespace.
    lowered_lints: Mapping[str, frozenset[str]]
    licenses: frozenset[str] = frozenset()
    duplicate_versions: bool = False
    advisories: frozenset[str] = frozenset()
    # Bun installs, type-checks, and builds the frontend before Cargo runs.
    frontend: bool = False


# Each exception is the minimum that the pinned framework needs to compile and audit.
PROFILES: Mapping[str, ProjectProfile] = {
    # cortex-m-rt's #[entry] emits #[allow(static_mut_refs)], which forbid rejects.
    "embedded": ProjectProfile(
        target_only=True,
        lowered_lints={"rust": frozenset({"rust_2024_compatibility"})},
    ),
    "wasm": ProjectProfile(target_only=True, lowered_lints={}),
    # Tauri's macros emit allow attributes for unused and clippy::all lints, call
    # process::exit, build large context frames, and generate wildcard imports.
    # Its GTK, Windows, and CSS dependency graph contains duplicate crate versions,
    # MPL-2.0 and LLVM-exception licenses, and two advisories without upgrades.
    "tauri": ProjectProfile(
        target_only=False,
        lowered_lints={
            "rust": frozenset({"unused"}),
            "clippy": frozenset({"all", "pedantic", "exit", "large_stack_frames"}),
        },
        licenses=frozenset({"MPL-2.0", "Apache-2.0 WITH LLVM-exception"}),
        duplicate_versions=True,
        advisories=frozenset({"RUSTSEC-2024-0370", "RUSTSEC-2024-0429"}),
        frontend=True,
    ),
}
LICENSES = frozenset(
    {
        "Apache-2.0",
        "BSD-2-Clause",
        "BSD-3-Clause",
        "ISC",
        "MIT",
        "Unicode-3.0",
        "Zlib",
    }
)
NO_PROFILE = ProjectProfile(target_only=False, lowered_lints={})


@dataclass(frozen=True)
class Settings:
    rust: str
    deny: str
    portable: tuple[str, ...]
    targets: tuple[str, ...]
    components: tuple[str, ...]
    install_profile: str
    profile: str | None = None
    profile_packages: tuple[str, ...] = ()

    @property
    def overlay(self) -> ProjectProfile:
        return NO_PROFILE if self.profile is None else PROFILES[self.profile]


def settings(root: Path = ROOT) -> Settings:
    manifest = read_toml(root / "Cargo.toml")
    policy = manifest["workspace"]["metadata"]["rust-policy"]
    expected = {"portable-packages", "cargo-deny-version"}
    profile_keys = {"project-profile", "profile-packages"}
    if set(policy) not in (expected, expected | profile_keys):
        raise PolicyError(
            f"rust-policy must contain exactly {sorted(expected)}, "
            f"optionally with {sorted(profile_keys)}"
        )
    toolchain = read_toml(root / "rust-toolchain.toml")["toolchain"]
    rust, deny = toolchain["channel"], policy["cargo-deny-version"]
    if any(
        not isinstance(v, str) or not re.fullmatch(r"\d+\.\d+\.\d+", v)
        for v in (rust, deny)
    ):
        raise PolicyError("Rust and cargo-deny require exact stable x.y.z version pins")
    portable = string_list(policy["portable-packages"], "portable-packages")
    profile = policy.get("project-profile")
    if profile is not None and profile not in PROFILES:
        raise PolicyError(f"project-profile must be one of {sorted(PROFILES)}")
    profile_packages = string_list(
        policy.get("profile-packages", []), "profile-packages"
    )
    if profile is not None and not profile_packages:
        raise PolicyError("project-profile requires at least one profile package")
    if any(
        not re.fullmatch(r"[A-Za-z0-9_-]+", name)
        for name in (*portable, *profile_packages)
    ):
        raise PolicyError(
            "portable-packages and profile-packages must contain Cargo package names"
        )
    if set(portable) & set(profile_packages):
        raise PolicyError("a package cannot be both portable and a profile package")
    targets = string_list(toolchain.get("targets", []), "toolchain.targets")
    components = string_list(toolchain.get("components", []), "toolchain.components")
    if not {"clippy", "rustfmt"}.issubset(components):
        raise PolicyError("rust-toolchain.toml must include clippy and rustfmt")
    return Settings(
        rust,
        deny,
        portable,
        targets,
        components,
        toolchain.get("profile", "minimal"),
        profile,
        profile_packages,
    )


def check_flags(tokens: Sequence[str], origin: str) -> None:
    for token in tokens:
        if token.startswith(
            ("-A", "--allow=", "--expect=", "--cap-lints=", "--force-warn=")
        ) or token in {"--allow", "--expect", "--cap-lints", "--force-warn"}:
            raise PolicyError(f"{origin}: lint override is prohibited: {token}")


def check_environment(environment: Mapping[str, str], root: Path = ROOT) -> None:
    for name, value in environment.items():
        if name.endswith(("RUSTFLAGS", "RUSTDOCFLAGS")) or name == "CLIPPY_ARGS":
            tokens = value.split("\x1f") if "ENCODED" in name else shlex.split(value)
            check_flags(tokens, name)
    directory = environment.get("CLIPPY_CONF_DIR")
    if directory and Path(directory).resolve() != root.resolve():
        raise PolicyError("CLIPPY_CONF_DIR must select this repository's configuration")


def select_toolchain(version: str) -> Path:
    """Resolve the pinned rustup toolchain without trusting PATH's Cargo shim."""
    result = subprocess.run(
        ["rustup", "which", "--toolchain", version, "cargo"],
        check=True,
        text=True,
        stdout=subprocess.PIPE,
    )
    path = Path(result.stdout.strip()).resolve()
    if not path.is_file():
        raise PolicyError(f"rustup returned a missing Cargo executable: {path}")
    return path.parent


def run(args: Sequence[str], root: Path = ROOT, *, capture: bool = False) -> str:
    environment = os.environ.copy()
    check_environment(environment, root)
    if "clippy" in args:
        test_clippy = "--all-targets" in args
        environment["CLIPPY_CONF_DIR"] = str(
            root / ".clippy-test" if test_clippy else root
        )
    else:
        environment.pop("CLIPPY_CONF_DIR", None)
    if TOOLCHAIN_BIN is not None:
        environment["PATH"] = (
            f"{TOOLCHAIN_BIN}{os.pathsep}{environment.get('PATH', '')}"
        )
    print("+ " + shlex.join(args), file=sys.stderr, flush=True)
    result = subprocess.run(
        list(args),
        cwd=root,
        env=environment,
        check=True,
        text=True,
        stdout=subprocess.PIPE if capture else None,
    )
    return result.stdout if capture else ""


def owned_packages(metadata: Mapping[str, Any]) -> list[dict[str, Any]]:
    members = set(metadata["workspace_members"])
    packages = [p for p in metadata["packages"] if p["id"] in members]
    if not packages or len(packages) != len(members):
        raise PolicyError("cargo metadata did not resolve every workspace member")
    return sorted(packages, key=lambda package: package["name"])


def validate_policy(
    root: Path, config: Settings, packages: Sequence[Mapping[str, Any]]
) -> None:
    manifest = read_toml(root / "Cargo.toml")
    production_clippy = read_toml(root / "clippy.toml")
    test_clippy = read_toml(root / ".clippy-test" / "clippy.toml")
    expected_test_clippy = dict(production_clippy)
    expected_test_clippy["check-private-items"] = False
    if test_clippy != expected_test_clippy:
        raise PolicyError(
            "test Clippy configuration must match clippy.toml except check-private-items = false"
        )
    for namespace in ("rust", "clippy"):
        lints = manifest["workspace"]["lints"][namespace]
        if not lints:
            raise PolicyError(f"workspace.lints.{namespace} must not be empty")
        for name, value in lints.items():
            level = value.get("level") if isinstance(value, dict) else value
            if level != "forbid":
                raise PolicyError(f"{namespace}::{name} must remain forbid")
    if not {"all", "pedantic"}.issubset(manifest["workspace"]["lints"]["clippy"]):
        raise PolicyError("clippy::all and clippy::pedantic are required")
    if config.portable:
        if not config.targets:
            raise PolicyError("portable packages require explicit toolchain targets")
        for profile in ("no-std", "std"):
            if manifest.get("profile", {}).get(profile) != {"inherits": "release"}:
                raise PolicyError(
                    f"profile.{profile} must inherit the shared release policy unchanged"
                )
    names = {p["name"] for p in packages}
    if not set(config.portable).issubset(names):
        raise PolicyError("portable-packages references a missing workspace member")
    if not set(config.profile_packages).issubset(names):
        raise PolicyError("profile-packages references a missing workspace member")
    if config.overlay.target_only and not config.targets:
        raise PolicyError(
            f"the {config.profile} profile requires explicit toolchain targets"
        )
    overlay_lints = profile_lints(manifest["workspace"]["lints"], config.overlay)
    for package in packages:
        path = Path(package["manifest_path"]).resolve()
        if not path.is_relative_to(root.resolve()):
            raise PolicyError(f"workspace member is outside the repository: {path}")
        member = read_toml(path)
        if package["name"] in config.profile_packages and overlay_lints is not None:
            if member.get("lints") != overlay_lints:
                raise PolicyError(
                    f"{path}: [lints] must equal the workspace lints with only the "
                    f"{config.profile} profile's lints lowered to deny"
                )
        elif member.get("lints") != {"workspace": True}:
            raise PolicyError(f"{path}: [lints] workspace = true is required")
        for key in ("edition", "rust-version"):
            if member["package"].get(key) != {"workspace": True}:
                raise PolicyError(f"{path}: package.{key}.workspace = true is required")
        if package["name"] in config.portable:
            features = member.get("features", {})
            if features.get("default") != [] or not {"std", "unsafe-code"}.issubset(
                features
            ):
                raise PolicyError(
                    f"{path}: portable crates require empty defaults, std, and unsafe-code"
                )
            if not any("lib" in target["kind"] for target in package["targets"]):
                raise PolicyError(
                    f"{path}: portable-packages must identify library packages"
                )

    def inspect_flags(value: Any, origin: str) -> None:
        if not isinstance(value, dict):
            return
        for key, child in value.items():
            label = f"{origin}.{key}"
            if key in {"rustflags", "rustdocflags"}:
                tokens = (
                    shlex.split(child)
                    if isinstance(child, str)
                    else string_list(child, label)
                )
                check_flags(tokens, label)
            else:
                inspect_flags(child, label)

    for filename in ("config", "config.toml"):
        path = root / ".cargo" / filename
        if path.exists():
            inspect_flags(read_toml(path), str(path))
    if not (root / "Cargo.lock").is_file():
        raise PolicyError(
            "Cargo.lock is required. Generate it. Review it before running the gate."
        )
    validate_dependency_policy(read_toml(root / "deny.toml"), config.overlay)
    scan_inline_test_modules(root)


def profile_lints(
    workspace_lints: Mapping[str, Mapping[str, Any]], profile: ProjectProfile
) -> dict[str, Any] | None:
    """Return the exact [lints] table for profile packages, or None to inherit."""
    if not profile.lowered_lints:
        return None
    lints = {namespace: dict(table) for namespace, table in workspace_lints.items()}
    for namespace, names in profile.lowered_lints.items():
        for name in names:
            value = lints[namespace][name]
            lints[namespace][name] = (
                {**value, "level": "deny"} if isinstance(value, dict) else "deny"
            )
    return lints


def validate_dependency_policy(
    deny: Mapping[str, Any], profile: ProjectProfile
) -> None:
    """Reject cargo-deny exceptions that the project profile does not require."""
    allowed = set(deny.get("licenses", {}).get("allow", []))
    extra = allowed - LICENSES - profile.licenses
    if extra:
        raise PolicyError(f"deny.toml allows unapproved licenses: {sorted(extra)}")
    duplicates = deny.get("bans", {}).get("multiple-versions")
    expected = "allow" if profile.duplicate_versions else "deny"
    if duplicates != expected:
        raise PolicyError(f'deny.toml bans.multiple-versions must be "{expected}"')
    ignored = {
        entry["id"] if isinstance(entry, dict) else entry
        for entry in deny.get("advisories", {}).get("ignore", [])
    }
    if not ignored.issubset(profile.advisories):
        raise PolicyError(
            f"deny.toml ignores unapproved advisories: {sorted(ignored - profile.advisories)}"
        )


def _mask_rust_literals_and_comments(source: str) -> str:
    """Replace Rust comments and literals with spaces while retaining newlines."""
    output = list(source)
    index = 0
    length = len(source)

    def blank(start: int, end: int) -> None:
        for position in range(start, end):
            if output[position] != "\n":
                output[position] = " "

    while index < length:
        if source.startswith("//", index):
            end = source.find("\n", index)
            end = length if end < 0 else end
            blank(index, end)
            index = end
        elif source.startswith("/*", index):
            start = index
            depth = 1
            index += 2
            while index < length and depth:
                if source.startswith("/*", index):
                    depth += 1
                    index += 2
                elif source.startswith("*/", index):
                    depth -= 1
                    index += 2
                else:
                    index += 1
            blank(start, index)
        elif source[index] == "r":
            match = re.match(r'r(#{0,255})"', source[index:])
            if match:
                start = index
                terminator = '"' + match.group(1)
                index += match.end()
                end = source.find(terminator, index)
                index = length if end < 0 else end + len(terminator)
                blank(start, index)
            else:
                index += 1
        elif source[index] == '"':
            start = index
            index += 1
            while index < length:
                if source[index] == "\\":
                    index += 2
                elif source[index] == '"':
                    index += 1
                    break
                else:
                    index += 1
            blank(start, min(index, length))
        elif source[index] == "'" and index + 2 < length:
            match = re.match(r"'(?:\\.|[^\\'\n])'", source[index:])
            if match:
                end = index + match.end()
                blank(index, end)
                index = end
            else:
                index += 1
        else:
            index += 1
    return "".join(output)


def _balanced_end(source: str, start: int) -> int:
    """Return the offset after a balanced delimiter group in masked Rust source."""
    pairs = {"(": ")", "[": "]", "{": "}"}
    stack = [pairs[source[start]]]
    for index in range(start + 1, len(source)):
        character = source[index]
        if character in pairs:
            stack.append(pairs[character])
        elif character == stack[-1]:
            stack.pop()
            if not stack:
                return index + 1
    return len(source)


def _test_condition(attribute: str) -> bool:
    """Recognize test predicates in cfg and conditional cfg attributes."""
    match = re.match(r"\s*(cfg|cfg_attr)\s*\(", attribute)
    if not match:
        return False
    opening = match.end() - 1
    body = attribute[opening + 1 : _balanced_end(attribute, opening) - 1]
    if match.group(1) == "cfg":
        return re.search(r"(?<![\w#])(?:r#)?test\b(?!\s*=)", body) is not None
    # The first argument controls whether the remaining attributes apply.
    index = 0
    while index < len(body):
        if body[index] in "([{":
            index = _balanced_end(body, index)
        elif body[index] == ",":
            if _test_condition(body[index + 1 :]):
                return True
            index += 1
        else:
            index += 1
    return False


def inline_test_modules(path: Path) -> list[int]:
    masked = _mask_rust_literals_and_comments(path.read_text(encoding="utf-8"))
    attribute = re.compile(r"#\s*\[")
    module = re.compile(
        r"(?:pub(?:\s*\([^)]*\))?\s+)?(?:unsafe\s+)?"
        r"mod\s+(?:r#)?[^\W\d]\w*\s*\{"
    )
    findings: list[int] = []
    index = 0
    while index < len(masked):
        match = attribute.search(masked, index)
        if match is None:
            break
        controlling = None
        while match is not None:
            end = _balanced_end(masked, match.end() - 1)
            if controlling is None and _test_condition(masked[match.end() : end - 1]):
                controlling = match.start()
            index = end
            while index < len(masked) and masked[index].isspace():
                index += 1
            match = attribute.match(masked, index)
        if controlling is not None and module.match(masked, index):
            findings.append(masked.count("\n", 0, controlling) + 1)
    return findings


def scan_inline_test_modules(root: Path) -> None:
    failures = [
        f"{path.relative_to(root)}:{line}: inline cfg(test) module. Use an external module"
        for path in sorted(root.rglob("*.rs"))
        if "target" not in path.parts and "node_modules" not in path.parts
        for line in inline_test_modules(path)
    ]
    if failures:
        raise PolicyError("\n".join(failures))


def feature_args(
    packages: Sequence[Mapping[str, Any]], *, std: bool, unsafe: bool
) -> list[str]:
    requested = []
    for package in packages:
        for feature, enabled in (("std", std), ("unsafe-code", unsafe)):
            if enabled and feature in package["features"]:
                requested.append(f"{package['name']}/{feature}")
    args = ["--no-default-features"]
    if requested:
        args += ["--features", ",".join(sorted(requested))]
    return args


def command_plan(
    config: Settings,
    packages: Sequence[Mapping[str, Any]],
    lane: str,
    root: Path = ROOT,
) -> list[list[str]]:
    portable = [p for p in packages if p["name"] in config.portable]
    target_only = [
        p
        for p in packages
        if config.overlay.target_only and p["name"] in config.profile_packages
    ]
    host = [p for p in packages if p not in target_only]
    # Target-only packages cannot link for the host, so host lanes exclude them.
    workspace = ["--workspace"]
    for package in target_only:
        workspace += ["--exclude", package["name"]]
    commands = []
    if lane == "embedded" and (not (portable or target_only) or not config.targets):
        raise PolicyError(
            "embedded checks require portable or target packages and explicit targets. None are skipped"
        )
    if config.overlay.frontend and lane in {"all", "host", "quick"}:
        # Tauri embeds the built frontend at compile time, so Bun runs before Cargo.
        commands += [
            ["bun", "install", "--frozen-lockfile"],
            ["bun", "run", "typecheck"],
            ["bun", "run", "build"],
        ]
    if lane in {"all", "quick"}:
        commands.append(
            [
                "cargo",
                "fmt",
                "--all",
                "--",
                "--check",
                "--config-path",
                str(root / "rustfmt.toml"),
            ]
        )
    if lane in {"all", "host"}:
        # Isolate portable roots so unrelated host members cannot unify std into them.
        for package in portable:
            for unsafe in (False, True):
                args = [
                    "--locked",
                    "--package",
                    package["name"],
                    *feature_args([package], std=False, unsafe=unsafe),
                ]
                commands += [
                    ["cargo", "clippy", "--lib", *args],
                    ["cargo", "clippy", "--all-targets", *args],
                    ["cargo", "test", *args],
                ]
        for unsafe in (False, True):
            args = [
                "--locked",
                *workspace,
                *feature_args(host, std=True, unsafe=unsafe),
            ]
            commands += [
                ["cargo", "clippy", *args],
                ["cargo", "clippy", "--all-targets", *args],
                ["cargo", "test", *args],
                ["cargo", "doc", "--no-deps", *args],
                [
                    "cargo",
                    "build",
                    "--profile",
                    "std" if portable else "release",
                    *args,
                ],
            ]
    elif lane == "quick":
        args = [
            "--locked",
            *workspace,
            *feature_args(host, std=True, unsafe=False),
        ]
        commands.append(["cargo", "clippy", *args])
    if lane == "embedded" or (lane == "all" and (portable or target_only)):
        for target in config.targets:
            for package in portable:
                for unsafe in (False, True):
                    args = [
                        "--locked",
                        "--package",
                        package["name"],
                        "--lib",
                        "--profile",
                        "no-std",
                        "--target",
                        target,
                        *feature_args([package], std=False, unsafe=unsafe),
                    ]
                    commands += [
                        ["cargo", "clippy", *args],
                        ["cargo", "build", *args],
                    ]
            # Firmware and WebAssembly packages build and link every target they own.
            for package in target_only:
                args = [
                    "--locked",
                    "--package",
                    package["name"],
                    "--profile",
                    "no-std",
                    "--target",
                    target,
                    *feature_args([package], std=False, unsafe=False),
                ]
                commands += [
                    ["cargo", "clippy", *args],
                    ["cargo", "build", *args],
                ]
    if lane in {"all", "dependencies"}:
        for std in (False, True):
            for unsafe in (False, True):
                commands.append(
                    [
                        "cargo",
                        "deny",
                        "--locked",
                        "--workspace",
                        "--config",
                        str(root / "deny.toml"),
                        *feature_args(packages, std=std, unsafe=unsafe),
                        "check",
                        "--deny",
                        "warnings",
                    ]
                )
    unique: list[list[str]] = []
    for command in commands:
        if command not in unique:
            unique.append(command)
    return unique


def require_version(output: str, name: str, expected: str) -> None:
    if output.split()[:2] != [name, expected]:
        raise PolicyError(f"required {name} {expected}. Received {output.strip()!r}")


def bun_minimum(root: Path = ROOT) -> tuple[int, ...]:
    """Read the minimum Bun version from package.json engines.bun (">=x.y.z")."""
    engines = json.loads((root / "package.json").read_text(encoding="utf-8")).get(
        "engines", {}
    )
    match = re.fullmatch(r">=(\d+)\.(\d+)\.(\d+)", str(engines.get("bun", "")))
    if match is None:
        raise PolicyError('package.json requires engines.bun = ">=x.y.z"')
    return tuple(int(part) for part in match.groups())


def require_minimum_version(output: str, minimum: tuple[int, ...]) -> None:
    match = re.fullmatch(r"(\d+)\.(\d+)\.(\d+)", output.strip())
    if match is None or tuple(int(part) for part in match.groups()) < minimum:
        expected = ".".join(map(str, minimum))
        raise PolicyError(f"required bun >= {expected}. Received {output.strip()!r}")


def execute_plan(commands: Sequence[Sequence[str]], root: Path = ROOT) -> None:
    for command in commands:
        run(command, root)


def main(argv: Sequence[str] | None = None) -> int:
    global TOOLCHAIN_BIN
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "lane",
        nargs="?",
        default="all",
        choices=(
            "all",
            "quick",
            "host",
            "embedded",
            "dependencies",
            "policy",
            "versions",
            "setup-rust",
        ),
    )
    args = parser.parse_args(argv)
    try:
        config = settings()
        if args.lane == "versions":
            print(f"rust={config.rust}\ndeny={config.deny}")
            return 0
        check_environment(os.environ)
        if args.lane == "setup-rust":
            command = [
                "rustup",
                "toolchain",
                "install",
                config.rust,
                "--profile",
                config.install_profile,
            ]
            for component in config.components:
                command += ["--component", component]
            for target in config.targets:
                command += ["--target", target]
            run(command)
            return 0
        TOOLCHAIN_BIN = select_toolchain(config.rust)
        require_version(run(["rustc", "--version"], capture=True), "rustc", config.rust)
        require_version(run(["cargo", "--version"], capture=True), "cargo", config.rust)
        metadata = json.loads(
            run(
                [
                    "cargo",
                    "metadata",
                    "--locked",
                    "--no-deps",
                    "--format-version",
                    "1",
                    "--no-default-features",
                ],
                capture=True,
            )
        )
        packages = owned_packages(metadata)
        validate_policy(ROOT, config, packages)
        if args.lane == "policy":
            print(
                "Manifest policy checks passed. No Rust lint, build, test, or dependency checks executed."
            )
            return 0
        commands = command_plan(config, packages, args.lane)
        if any(command[0] == "bun" for command in commands):
            require_minimum_version(
                run(["bun", "--version"], capture=True), bun_minimum()
            )
        if args.lane in {"all", "dependencies"}:
            require_version(
                run(["cargo", "deny", "--version"], capture=True),
                "cargo-deny",
                config.deny,
            )
        if args.lane == "all" and any((ROOT / "tools").glob("test_*.py")):
            run(
                [
                    sys.executable,
                    "-m",
                    "unittest",
                    "discover",
                    "-s",
                    "tools",
                    "-p",
                    "test_*.py",
                    "-v",
                ]
            )
        lock_before = (ROOT / "Cargo.lock").read_bytes()
        execute_plan(commands)
        if (ROOT / "Cargo.lock").read_bytes() != lock_before:
            raise PolicyError("checks modified Cargo.lock")
        if args.lane == "all":
            targets = sorted(
                {
                    command[command.index("--target") + 1]
                    for command in commands
                    if command[1] == "build" and "--target" in command
                }
            )
            coverage = (
                f"Cross-target packages compiled for {', '.join(targets)}."
                if targets
                else "Host-only configuration. No embedded compilation occurred."
            )
            print(
                f"All configured checks passed. {coverage} No hardware execution occurred."
            )
        else:
            print(f"{args.lane} checks passed. This is not the full acceptance gate.")
        return 0
    except subprocess.CalledProcessError as error:
        print(f"FAILED: command exited {error.returncode}", file=sys.stderr)
        return error.returncode if error.returncode > 0 else 1
    except (PolicyError, OSError, ValueError, KeyError, TypeError) as error:
        print(f"FAILED: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
