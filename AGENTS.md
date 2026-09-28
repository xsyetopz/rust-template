# template-rust Instructions

These rules apply throughout the repository. Explicit task instructions control scope.
Read `README.md`, affected manifests, and subtree instructions before editing.
Preserve unrelated files, lockfiles, hidden configuration, and file properties.
`CLAUDE.md` and `GEMINI.md` are symlinks to this file. Edit `AGENTS.md` only.

## Commands

Run from the repository root with Python 3.11+ and rustup installed.

- Compiler setup: `python3 tools/check.py setup-rust`.
- Audit tool: `cargo +1.98.1 install cargo-deny --locked --version 0.20.2`.
- Template tests require: `cargo +1.98.1 install cargo-generate --locked --version 0.24.0`,
  `rustup target add --toolchain 1.98.1 wasm32v1-none`, Bun 1.4.2+, and on Linux
  the WebKitGTK packages in `.github/workflows/check.yml`.
- During edits: `python3 tools/check.py quick`.
- Before acceptance: `python3 tools/check.py` plus existing project-specific checks.

`quick` is not acceptance. Missing tools or required coverage are failures.
The full gate covers host tests, feature modes, embedded compilation, dependencies,
and template generation. Report hardware execution separately from compilation.

## Source boundaries

Change generated output through `tools/bootstrap.py` or its source configuration.
Keep shared scanning and checks in `tools/check.py` so generated projects remain self-contained.
Keep each profile's exceptions in `PROFILES` in `tools/check.py`. Changing them is a policy change.
Keep `.agents/skills` in sync with the profiles. `tools/bootstrap.py` copies it into generated projects.
Keep `tools/adopt.py` read-only. Use [README.md](README.md#adopt-in-an-existing-repository) for policy merges.
Keep `lib.rs`, `main.rs`, and `mod.rs` limited to attributes, module declarations,
re-exports, and entry-point composition. Put implementation in ordinary modules.
Put unit tests in adjacent `<module>/tests.rs` files or explicit external modules.
Put integration tests under each package's `tests/` directory.

## Rust contracts

Keep portable defaults empty. Use `core`, deliberate `alloc`, and feature-gated `std`.
Register portable libraries in `workspace.metadata.rust-policy.portable-packages`.
Inherit workspace lints, edition, and minimum Rust version in each package.
Preserve public contracts, evaluation order, errors, timing, and feature behavior.
Return recoverable failures explicitly. Define integer overflow and conversion semantics.
Do not add allocations, copies, dependencies, or wrappers merely to satisfy a lint.
Unsafe changes require authorization, explicit operations, local `SAFETY` explanations,
API safety contracts, and invariant tests. Keep the safe default. Permission does not prove soundness.
Require measurements before adding a faster backend.

## Enforcement and evidence

Do not hide failures with lint suppressions, formatting skips, caps, dummy uses, ignored results,
disabled tests, or configuration gates. Test exceptions belong in `clippy.toml`.
Policy changes require explicit authorization and a reason. Do not weaken checks or CI.
Preserve target, linker, runner, and compiler settings. Report conflicts and unsupported settings.
Add regression tests for changed behavior. Compare the final diff and packaged files with task scope.
Report executed checks, measurements, and remaining failures. Never claim an unavailable check passed.
