# Profiles

This file has one card per template profile. Each card gives the layout, the
exceptions that profile carries, and the generation command. Generate from
the template root with `python3 tools/bootstrap.py`, or with
`cargo generate --define profile=PROFILE` as `SKILL.md` shows. Every command
here was run with Rust 1.98.1 and `python3 tools/check.py` exiting 0 on the
generated project. The only exception is Tauri on Linux and Windows. That
path is covered by CI configuration and was not run locally.

## Contents

- [Crate profile](#crate-profile)
- [Embedded profile](#embedded-profile)
- [WASM profile](#wasm-profile)
- [Tauri profile](#tauri-profile)

## Crate profile

**Definition.** One package: either at the root (`--shape root`) or in
`crates/NAME` of a workspace (`--shape workspace`). It is a library
(`--role lib`), a binary (`bin`), or both. With `--mode portable`, the library
is `no_std` by default and has `std` and `unsafe-code` features. It is listed
in `portable-packages` and compiled for `thumbv6m-none-eabi` as a smoke target.
This profile has no policy exceptions.

**Use when.**

- The request is a CLI, a library, a service binary, or a CLI plus a library.
- A `no_std` library must also work with `std` on the host (`--mode portable`).
- More crates will be added later (`--shape workspace`).

**Do not use when.**

- The code must run on a microcontroller. Portable binary-only output is
  rejected because it needs a runtime and linker script. Use the embedded
  profile.
- The output is a `.wasm` module or a desktop GUI. Use the WASM or Tauri
  profile.

**Example.**

```sh
python3 tools/bootstrap.py --name sensor-cli --author "Ada Lovelace" \
  --shape workspace --role both --output ../sensor-cli
```

**Cost removed.** Choosing shape, role, and mode by hand and then fixing lint,
feature, and layout failures one at a time. The generated project passes
`python3 tools/check.py` unchanged.

**Verify.**

1. `python3 tools/check.py` ends with `All configured checks passed.`
1. Portable mode adds `Cross-target packages compiled for thumbv6m-none-eabi.`

## Embedded profile

**Definition.** A workspace with the portable library `crates/NAME` and the
firmware binary `crates/NAME-firmware` for `thumbv6m-none-eabi`
(Cortex-M0/M0+). The firmware uses `cortex-m-rt` 0.7.7 and `panic-halt` 1.0.0.
`build.rs` puts `memory.x` on the linker path, and `.cargo/config.toml` passes
`-Tlink.x`. The reset handler lives in `src/firmware.rs`. `main.rs` only
declares modules. `default-members` leaves the firmware out of host builds.

It has exactly one exception: the firmware's `[lints]` table lowers
`rust_2024_compatibility` from `forbid` to `deny`. The reason is that
`#[cortex_m_rt::entry]` expands to `#[allow(static_mut_refs)]`, which a
`forbid` group rejects. The `cortex-m` crate is left out on purpose. It pulls
in duplicate `embedded-hal` and `syn` versions and the unmaintained
`bare-metal` crate (RUSTSEC-2026-0110), which `deny.toml` rejects.

**Use when.**

- The request names a microcontroller, firmware, bare metal, or Cortex-M.

**Do not use when.**

- The target is RISC-V or another architecture that `cortex-m-rt` does not
  support. Generate this profile, then replace the runtime crate and the
  target. The overlay is tied to `cortex-m-rt`, so expect to justify any
  different lint exception to the user.

**Example.**

```sh
python3 tools/bootstrap.py --name blinky --author "Ada Lovelace" \
  --profile embedded --output ../blinky
```

**Cost removed.** Writing a linker script setup, a panic handler, lint
exceptions, and host-lane exclusions from scratch. The gate compiles and
links the ELF on the first run.

**Verify.**

1. `python3 tools/check.py` prints
   `Cross-target packages compiled for thumbv6m-none-eabi.`
1. `file target/thumbv6m-none-eabi/no-std/blinky-firmware` reports an ARM
   ELF executable.
1. Hardware execution needs a probe, for example `probe-rs run --chip CHIP`.
   Report it separately. The gate never runs it.

## WASM profile

**Definition.** A workspace with the portable library `crates/NAME` and a
`cdylib` package `crates/NAME-wasm` for `wasm32v1-none`. The crate is
`no_std`, uses no `wasm-bindgen`, and exports C ABI functions with
`#[unsafe(no_mangle)]`. The crate root keeps `#![deny(unsafe_code)]`, and each
export carries a scoped `#[expect(unsafe_code, reason = ...)]` and a `SAFETY`
comment. Exports are private `const extern "C" fn`s. `pub` would trip
`unreachable_pub`, `pub(crate)` would trip `redundant_pub_crate`, and a
non-`const` body would trip `missing_const_for_fn`. The profile adds no lint or
dependency exceptions.

**Use when.**

- The request is a `.wasm` module for a browser, Bun, or another WebAssembly
  host with numeric exports.

**Do not use when.**

- The request needs JavaScript bindings for strings, objects, or DOM access.
  That needs `wasm-bindgen` and `wasm32-unknown-unknown`, which this profile
  does not ship. Tell the user, and extend the profile only if they agree.

**Example.**

```sh
python3 tools/bootstrap.py --name codec --author "Ada Lovelace" \
  --profile wasm --output ../codec
```

**Cost removed.** Choosing between `wasm32v1-none` and
`wasm32-unknown-unknown`, writing a panic handler, and meeting the unsafe
policy for exports. The generated module has 1 export plus `memory`.

**Verify.**

1. `python3 tools/check.py` prints
   `Cross-target packages compiled for wasm32v1-none.`
1. Run the export in Bun:

   ```sh
   bun -e 'const m = await WebAssembly.instantiate(await Bun.file(
     "target/wasm32v1-none/no-std/codec_wasm.wasm").arrayBuffer());
     console.log(m.instance.exports.project_name_len())'
   ```

   It prints the byte length of the package name (`5` for `codec`).

## Tauri profile

**Definition.** A Tauri 2.12 desktop app. The root holds a Vite 8 and
TypeScript 7 frontend that is managed by Bun 1.4.2+ only, with
`engines.bun = ">=1.4.2"`. The root workspace has the one member `src-tauri`.
`src-tauri/src/application.rs` builds the app, and
`application/commands.rs` holds `#[tauri::command]` functions. Two
`tsconfig` programs split app code (`vite/client` types) from
`vite.config.ts` (`bun` types), because both type packages declare
`ImportMeta`. The gate runs `bun install --frozen-lockfile`,
`bun run typecheck`, and `bun run build` before Cargo.

The fixed exceptions are these:

- `src-tauri` lowers the rust lint `unused` and the clippy lints `all`,
  `pedantic`, `exit`, and `large_stack_frames` to `deny`.
- `run()` carries `#[expect]`s for the lints that Tauri macros trigger.
- `deny.toml` allows MPL-2.0 and `Apache-2.0 WITH LLVM-exception`.
- `deny.toml` sets `multiple-versions = "allow"`.
- `deny.toml` ignores RUSTSEC-2024-0370 and RUSTSEC-2024-0429 from the
  GTK 0.18 stack, with reasons.

**Use when.**

- The request is a desktop app, a native window around a web UI, or Tauri.

**Do not use when.**

- The user wants Node, npm, or a different frontend framework build. This
  profile uses Bun only. Adding React or Svelte is fine as Bun dependencies
  plus a Vite plugin.
- The target is mobile. The profile generates no `gen/android` or
  `gen/apple` projects.

**Example.**

```sh
python3 tools/bootstrap.py --name notes --author "Ada Lovelace" \
  --profile tauri --output ../notes
cd ../notes && bun install && bun run tauri dev
```

**Cost removed.** Reconciling Tauri's macro expansions and dependency graph
with a forbid-everything policy. That takes many gate iterations, and the
fixes are narrow: no crate-wide `allow`.

**Verify.**

1. `python3 tools/check.py` ends with `All configured checks passed.`
1. `bun run tauri build` produces a bundle. It is not part of the gate. Linux
   needs the WebKitGTK packages listed in `.github/workflows/check.yml`.
