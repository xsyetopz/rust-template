---
name: scaffold-strict-rust-projects
description: >-
  Generates and extends Rust 1.98.1 projects from the xsyetopz strict-policy
  template: CLI, library, and binary crates, portable no_std libraries,
  Cortex-M embedded firmware, WebAssembly modules, and Tauri 2 desktop apps
  with a Bun, Vite, and TypeScript frontend. Use when asked to start a Rust
  project, pick a template profile, or retarget firmware, add a WASM export,
  or add a Tauri command inside a generated project while keeping
  tools/check.py passing. Not for optimizing existing Rust code.
compatibility: >-
  Requires Python 3.11+, rustup with Rust 1.98.1, and cargo-deny 0.20.2.
  The tauri profile also requires Bun 1.4.2+; on Linux it needs WebKitGTK 4.1.
---

# Scaffold strict Rust projects

Produce a Rust project whose policy gate passes on the first run, and keep it
passing while you extend it. The template renders four profiles: `crate`
(root or workspace, library, binary, or both, host or portable), `embedded`,
`wasm`, and `tauri`. Each profile has a fixed layout and a fixed, minimal list
of policy exceptions that `tools/check.py` enforces exactly. Pick the profile
from the request, generate, run the gate, and report what ran.

## Workflow

1. Find where you are.
   - A `tools/bootstrap.py` file means you are in the template: generate from it.
   - `[workspace.metadata.rust-policy]` in `Cargo.toml` without
     `tools/bootstrap.py` means you are in a generated project: extend it.
     Its `project-profile` key names the profile. No key means `crate`.
   - Neither means an empty or unrelated directory: generate with
     cargo-generate (step 3).
1. Choose the profile and options with the routing table below. When the
   request does not say, ask about the one thing that changes the layout.
   For example, ask "library, binary, or both?" or "which chip?".
1. Generate:
   - From the template root:

     ```sh
     python3 tools/check.py setup-rust
     python3 tools/bootstrap.py --name NAME --author "AUTHOR" --profile PROFILE \
       --output ../NAME
     ```

     Add `--shape`, `--role`, and `--mode` only with `--profile crate`. Every
     other profile rejects them. Use `--dry-run` to list the files first.
   - Without a local template copy:

     ```sh
     cargo generate --git https://github.com/xsyetopz/rust-template \
       --name NAME --allow-commands --define author="AUTHOR" \
       --define profile=PROFILE
     ```

     The post-generation hook runs `python3 tools/bootstrap.py`. Show the user
     the hook (`tools/cargo-generate.rhai`) before passing `--allow-commands`.
1. In the generated project, install the tools and run the full gate:

   ```sh
   python3 tools/check.py setup-rust
   rustup run 1.98.1 cargo install cargo-deny --locked --version 0.20.2
   python3 tools/check.py
   ```

   Use `python3 tools/check.py quick` during edits. It is not acceptance.
1. For changes inside a generated project, apply one card from
   [extending generated projects](references/extending.md), then run the full
   gate again.
1. Report using the completion evidence below.

## Route the request to a card

| Request or evidence | Card |
| --- | --- |
| CLI tool, library, binary, or both; no special target | [Crate profile](references/profiles.md#crate-profile) |
| `no_std` library that must also build on the host | [Crate profile, portable mode](references/profiles.md#crate-profile) |
| Microcontroller, firmware, Cortex-M, bare metal, `#[entry]` | [Embedded profile](references/profiles.md#embedded-profile) |
| WebAssembly, `.wasm`, browser or Bun host without wasm-bindgen | [WASM profile](references/profiles.md#wasm-profile) |
| Desktop app, web UI in a native window, Tauri | [Tauri profile](references/profiles.md#tauri-profile) |
| Different chip, core, memory map, or flashing | [Retarget firmware](references/extending.md#retarget-firmware) |
| New function callable from JavaScript or another WASM host | [Add a WASM export](references/extending.md#add-a-wasm-export) |
| New frontend-to-Rust call in a Tauri app | [Add a Tauri command](references/extending.md#add-a-tauri-command) |
| The gate rejects a dependency, license, advisory, or lint level | [Policy exceptions](references/extending.md#policy-exceptions) |

## Rules

- Never lower a lint, add `#[allow]`, or edit `deny.toml` to pass the gate.
  `tools/check.py` compares each profile package's `[lints]` table and the
  `deny.toml` exceptions with a fixed per-profile list and fails on any
  difference. Fix the code. If a framework macro forces a lint, use a scoped
  `#[expect(lint, reason = "...")]` on the item that expands it. Unlike
  `allow`, `expect` fails when the lint stops firing.
- Keep `lib.rs`, `main.rs`, and `mod.rs` to attributes, module declarations,
  re-exports, and entry-point composition. The gate scans for inline test
  modules. Unit tests go in `<module>/tests.rs`, and integration tests in the
  package's `tests/`.
- Target-only packages (`*-firmware`, `*-wasm`) cannot build or test on the
  host. The gate excludes them from host lanes and builds them per target in
  the embedded lane. Put testable logic in the portable library, not in them.
- Path dependencies inside the workspace need `default-features = false`,
  because `deny.toml` sets `workspace-default-features = "deny"`.
- In Tauri projects, use Bun and `bunx` only. `bun.lock` is frozen in the
  gate. Change it with `bun add` or `bun install` and review the diff. The
  frontend builds before Cargo because `tauri::generate_context!` embeds
  `dist/` at compile time, so `cargo` fails in a fresh clone until
  `bun run build` runs.
- A successful firmware or WASM build is compilation evidence only. Never
  report hardware or runtime execution that you did not run.

## References

- [Profiles](references/profiles.md): crate, embedded, WASM, and Tauri
  profiles, with each layout, the exceptions it carries, and the generation
  command.
- [Extending generated projects](references/extending.md): retargeting
  firmware, adding WASM exports, adding Tauri commands, and handling policy
  exceptions.

## Completion evidence

The report contains:

- The profile and options used.
- The generation command.
- The full-gate result line, which names cross-target compilation and states
  that no hardware execution occurred.
- Any check that could not run, with its reason. Examples: Bun missing,
  Linux WebKitGTK packages missing, or no probe connected.

State plainly that firmware was not flashed and that bundles were not built
unless you did both.
