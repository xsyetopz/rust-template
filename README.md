# Rust template

A dependency-free Rust workspace with enforced formatting, lint, feature,
portability, and dependency checks. Requires Python 3.11+ and rustup.
The template pins Rust to **1.98.1**. Editing rules are in [AGENTS.md](AGENTS.md).

## Generate a project

Run from this template's root:

```sh
python3 tools/check.py setup-rust
python3 tools/bootstrap.py --name sensor-core --author "Ada Lovelace"
```

| Option | Values |
| --- | --- |
| `--profile` | `crate` (default), `embedded`, `wasm`, `tauri` |
| `--shape` | `root` (default), `workspace`. Only with `--profile crate`. |
| `--role` | `lib` (default), `bin`, `both`. Only with `--profile crate`. |
| `--mode` | `host` (default), `portable`. Only with `--profile crate`. |

The profiles:

| Profile | Layout | Policy exceptions |
| --- | --- | --- |
| `crate` | One library, binary, or both, at the root or in a workspace. Optionally a portable `no_std` library. | None |
| `embedded` | Portable library plus a `cortex-m-rt` firmware binary for `thumbv6m-none-eabi`, with `memory.x` and a linker config. | Firmware lowers `rust_2024_compatibility` to `deny`. |
| `wasm` | Portable library plus a `no_std` `cdylib` with C ABI exports for `wasm32v1-none`. | None |
| `tauri` | Tauri 2 app in `src-tauri`. The Vite and TypeScript frontend at the root uses Bun 1.4.2+ only. | Lowered lints, licenses, duplicate versions, and advisories that Tauri needs |

`tools/check.py` holds each profile's exceptions in `PROFILES`. It rejects any
lint table or `deny.toml` exception that differs from that list. Profiles record
themselves in `workspace.metadata.rust-policy` as `project-profile` and
`profile-packages`. The target-only firmware and WebAssembly packages are left out
of host lanes. They are built and linked for each toolchain target instead. The
Tauri profile runs `bun install --frozen-lockfile`, `bun run typecheck`, and
`bun run build` before Cargo, and it needs WebKitGTK on Linux.

`--year` defaults to the current year. `--dry-run` previews files without writing.
Author names support Unicode, quotes, and backslashes. The renderer rejects ASCII
controls and trims surrounding whitespace. Portable binary-only output requires
project-specific runtime, linker, and board configuration, so the renderer rejects it.

Optional cargo-generate commands use the same renderer:

```sh
cargo +1.98.1 install cargo-generate --locked --version 0.24.0
cargo generate --path . --allow-commands --define profile=wasm
```

Review the hook before granting `--allow-commands`. Generated projects exclude
template-only tools and tests. They remain `publish = false` until configured for
publication. This template uses the [MIT license](LICENSE). Adoption does not
relicense existing work.

## Agent skill

`.agents/skills/scaffold-strict-rust-projects` teaches coding agents to pick a profile,
generate a project, and extend it without breaking the gate. Codex and Gemini CLI read
`.agents/skills` directly. Other agents, Claude Code among them, can install it with:

```sh
bunx skills add xsyetopz/rust-template
```

Generated projects ship a copy of the skill.

## Check a project

Run from the generated project or template root:

```sh
python3 tools/check.py setup-rust
cargo +1.98.1 install cargo-deny --locked --version 0.20.2
python3 tools/check.py
```

The template's full gate also requires cargo-generate **0.24.0**, installed above. It
also requires `rustup target add --toolchain 1.98.1 wasm32v1-none`, Bun **1.4.2+**,
and, on Linux, the WebKitGTK packages listed in [the workflow](.github/workflows/check.yml),
because its tests run the full gate on generated projects of every profile.
Use `python3 tools/check.py quick` during edits. It is not the acceptance gate.
The full gate includes host tests and doctests, Clippy, dependency audits, and
embedded library builds where configured. Template tests also check generated projects.
CI runs the full gate with Python 3.11 and pinned actions.

Portable libraries use empty defaults and separate `std` and `unsafe-code`
features. Register them in `workspace.metadata.rust-policy.portable-packages`.
An empty list supports host-only projects. Each package inherits workspace lints,
edition, and minimum Rust version. Portable checks isolate packages and exercise
both unsafe-permission settings. Permission does not establish soundness.

The `std` and `no-std` profiles share release settings. Profiles control code
generation. Features control capabilities. Replace the `thumbv6m-none-eabi` smoke
target in [rust-toolchain.toml](rust-toolchain.toml) with supported targets.
Portable packages require explicit targets. Compilation provides no hardware evidence.
Keep application-specific feature checks, platform builds, and hardware tests.

[Cargo.toml](Cargo.toml), [clippy.toml](clippy.toml), [rustfmt.toml](rustfmt.toml),
and [deny.toml](deny.toml) define policy and thresholds. Passing checks does not
prove soundness, bounded resource use, panic freedom, or performance.

## Adopt in an existing repository

Inspect the repository without changing it:

```sh
python3 tools/adopt.py --dry-run /path/to/repository
```

For portable libraries, add repeatable `--portable-package NAME` and `--target TARGET` options.
The analyzer reports policy gaps, source layout, configuration, and licensing.
Member discovery applies exclusions and reports unmatched declarations. It does
not resolve the complete Cargo dependency graph.

1. Inventory crates, features, native checks, platforms, and external members.
2. Select portable libraries and supported targets. Check dependency features with `cargo tree -e features`.
3. Keep wiring files (`lib.rs`, `main.rs`, `mod.rs`) limited to attributes, modules, re-exports, and entry-point composition.
   Move implementation into ordinary modules. Keep unit tests in external modules and integration tests in each package's `tests/` directory.
   Preserve visibility, API paths, setup order, and assertions when moving tests.
4. Merge policy and CI commands. Preserve package identity, dependencies, features, membership, lockfiles, and licenses.
   Preserve assets, build scripts, hidden configuration, and existing instructions.
   Retain target, linker, and runner settings. Resolve conflicts explicitly.
5. Use the pinned Cargo version for manifest changes. Review the dependency diff. Run the full gate and every existing acceptance check.
