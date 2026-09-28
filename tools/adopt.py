"""Inspect an existing Rust repository and propose a manual policy migration."""

from __future__ import annotations

import argparse
import glob
import re
import sys
from pathlib import Path
from typing import Any

import check
import tomllib


class AdoptionError(Exception):
    """The repository cannot be inspected safely."""


def read_manifest(path: Path) -> dict[str, Any]:
    try:
        with path.open("rb") as source:
            return tomllib.load(source)
    except (OSError, tomllib.TOMLDecodeError) as error:
        raise AdoptionError(f"cannot read {path}: {error}") from error


def workspace_members(
    root: Path, manifest: dict[str, Any]
) -> tuple[list[Path], list[Path], list[str]]:
    root = root.resolve()
    workspace = manifest.get("workspace", {})
    if not isinstance(workspace, dict):
        raise AdoptionError("workspace must be a table")
    for key in ("members", "exclude"):
        value = workspace.get(key, [])
        if not isinstance(value, list) or any(
            not isinstance(item, str) for item in value
        ):
            raise AdoptionError(f"workspace.{key} must be a list of paths")
    excluded = {
        Path(item).resolve()
        for pattern in workspace.get("exclude", [])
        for item in glob.glob(str(root / pattern))
    }
    discovered: set[Path] = set()
    unmatched: list[str] = []
    for pattern in workspace.get("members", []):
        matches = {Path(item).resolve() for item in glob.glob(str(root / pattern))}
        if not matches:
            unmatched.append(pattern)
        discovered.update(matches - excluded)
    if "package" in manifest:
        discovered.add(root)
    members = sorted(path for path in discovered if path.is_relative_to(root))
    external = sorted(path for path in discovered if not path.is_relative_to(root))
    return members, external, unmatched


def policy_findings(
    manifest: dict[str, Any], portable: tuple[str, ...], targets: tuple[str, ...]
) -> list[str]:
    messages: list[str] = []
    workspace = manifest.get("workspace")
    if not isinstance(workspace, dict):
        messages.append("missing [workspace]. Add it without changing package identity")
        workspace = {}
    package = workspace.get("package", {})
    for key, wanted in (("edition", "2024"), ("rust-version", "1.98")):
        actual = package.get(key) if isinstance(package, dict) else None
        if actual is None:
            messages.append(f"missing workspace.package.{key} = {wanted!r}")
        elif actual != wanted:
            messages.append(
                f"conflict: workspace.package.{key} is {actual!r}. Template policy uses {wanted!r}"
            )
    lints = workspace.get("lints", {})
    for namespace in ("rust", "clippy"):
        if not isinstance(lints, dict) or not lints.get(namespace):
            messages.append(
                f"missing workspace.lints.{namespace}. Merge the template lint table"
            )
    metadata = workspace.get("metadata", {})
    current = metadata.get("rust-policy") if isinstance(metadata, dict) else None
    if current is None:
        messages.append("missing workspace.metadata.rust-policy")
    elif not isinstance(current, dict):
        messages.append("conflict: workspace.metadata.rust-policy is not a table")
    if portable and not targets:
        messages.append(
            "portable packages were selected but no explicit --target was supplied"
        )
    if targets and not portable:
        messages.append("targets were supplied without any --portable-package")
    if portable:
        messages.append(f"proposed portable-packages = {list(portable)!r}")
        messages.append(f"proposed smoke targets = {list(targets)!r}")
    return messages


def wiring_implementation(path: Path) -> list[int]:
    masked = check._mask_rust_literals_and_comments(path.read_text(encoding="utf-8"))
    findings: list[int] = []
    pattern = re.compile(
        r"^[ \t]*(?:pub(?:\s*\([^)]*\))?\s+)?(?:async\s+|unsafe\s+|const\s+|extern\s+)*"
        r"(fn|struct|enum|union|trait|impl|static|const|type|macro_rules!)\b",
        re.MULTILINE,
    )
    for match in pattern.finditer(masked):
        line = masked.count("\n", 0, match.start()) + 1
        if path.name == "main.rs" and match.group(1) == "fn":
            declaration = masked[match.start() : masked.find("{", match.start())]
            if re.search(r"\bfn\s+main\s*\(", declaration):
                continue
        findings.append(line)
    return findings


def analyze(
    root: Path, portable: tuple[str, ...], targets: tuple[str, ...]
) -> dict[str, list[str]]:
    manifest_path = root / "Cargo.toml"
    if not manifest_path.is_file():
        raise AdoptionError(f"repository has no Cargo.toml: {root}")
    manifest = read_manifest(manifest_path)
    members, external, unmatched = workspace_members(root, manifest)
    if "package" in manifest:
        shape = (
            "root package with workspace policy"
            if "workspace" in manifest
            else "standalone root package"
        )
    elif "workspace" in manifest:
        shape = "virtual workspace"
    else:
        raise AdoptionError("Cargo.toml has neither [package] nor [workspace]")
    findings = {
        "Repository": [
            f"shape: {shape}",
            f"internal declared-member roots discovered: {len(members)}",
        ],
        "Policy": policy_findings(manifest, portable, targets),
        "Workspace": [
            *(f"unmatched member declaration: {pattern}" for pattern in unmatched),
            "Declared-member discovery does not resolve the complete Cargo dependency graph.",
            *(
                [
                    f"external member requires explicit integration: {path}"
                    for path in external
                ]
                or ["no external declared-member roots discovered"]
            ),
        ],
        "Source structure": [],
    }
    for path in sorted(root.rglob("*.rs")):
        if any(part in {"target", ".git"} for part in path.parts):
            continue
        for line in check.inline_test_modules(path):
            findings["Source structure"].append(
                f"{path.relative_to(root)}:{line}: inline cfg(test) module"
            )
        if path.name in {"lib.rs", "main.rs", "mod.rs"}:
            for line in wiring_implementation(path):
                findings["Source structure"].append(
                    f"{path.relative_to(root)}:{line}: implementation in wiring file"
                )
    collisions = [
        name
        for name in (
            "clippy.toml",
            "rustfmt.toml",
            "deny.toml",
            "rust-toolchain.toml",
            "AGENTS.md",
        )
        if (root / name).exists()
    ]
    workflows = list((root / ".github" / "workflows").glob("*"))
    cargo_configs = [
        f".cargo/{name}"
        for name in ("config", "config.toml")
        if (root / ".cargo" / name).exists()
    ]
    findings["CI and configuration"] = [
        "existing policy files to merge: " + (", ".join(collisions) or "none"),
        f"existing CI workflows to preserve: {len(workflows)}",
        "Cargo target/linker configs to preserve: "
        + (", ".join(cargo_configs) or "none"),
    ]
    licenses = sorted(
        path.name
        for path in root.iterdir()
        if path.is_file() and path.name.upper().startswith(("LICENSE", "COPYING"))
    )
    package = manifest.get("package")
    package_license = package.get("license") if isinstance(package, dict) else None
    findings["Licensing"] = [
        f"license files: {licenses or 'none'}. Root package license: {package_license or 'unspecified'}"
    ]
    findings["Manual adoption"] = [
        "merge workspace lint tables and release policy",
        "add tools/check.py without replacing native checks",
        "add or merge clippy.toml, rustfmt.toml, deny.toml, and rust-toolchain.toml",
        "move inline unit tests adjacent to their implementation modules",
        "extract implementation from lib.rs, main.rs, and mod.rs",
    ]
    return findings


def report(findings: dict[str, list[str]]) -> str:
    output = ["Rust policy adoption dry run", "No files were modified."]
    for section, messages in findings.items():
        if messages:
            output.extend(("", f"## {section}"))
            output.extend(f"- {message}" for message in messages)
    return "\n".join(output) + "\n"


def main(arguments: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dry-run", action="store_true", required=True)
    parser.add_argument("--portable-package", action="append", default=[])
    parser.add_argument("--target", action="append", default=[])
    parser.add_argument("repository", type=Path)
    args = parser.parse_args(arguments)
    try:
        root = args.repository.resolve(strict=True)
        if not root.is_dir():
            raise AdoptionError(f"not a directory: {root}")
        print(
            report(analyze(root, tuple(args.portable_package), tuple(args.target))),
            end="",
        )
        return 0
    except (AdoptionError, OSError, UnicodeError) as error:
        print(f"FAILED: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
