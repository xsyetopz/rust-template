# Extending generated projects

This file has cards for the changes that agents most often make inside a
generated project, and that break its gate when made the obvious way. Each
card ends with the full gate, `python3 tools/check.py`, because `quick` skips
tests, target builds, and dependency audits.

## Contents

- [Retarget firmware](#retarget-firmware)
- [Add a WASM export](#add-a-wasm-export)
- [Add a Tauri command](#add-a-tauri-command)
- [Policy exceptions](#policy-exceptions)

## Retarget firmware

**Definition.** Move the embedded profile from the `thumbv6m-none-eabi`
placeholder to the user's chip. The target triple appears in three files,
and they must agree:

- `rust-toolchain.toml` `targets`
- the `[target.TRIPLE]` table in `.cargo/config.toml`
- `targets:` in `.github/workflows/check.yml`

`memory.x` must match the chip's datasheet.

**Use when.**

- The user names a chip or board, for example an STM32F4 (Cortex-M4F,
  `thumbv7em-none-eabihf`) or an RP2040 (Cortex-M0+, `thumbv6m-none-eabi`).

**Do not use when.**

- The chip is not Cortex-M. `cortex-m-rt` only supports Cortex-M. Tell the
  user that the runtime crate and the profile's lint exception must change.

**Example.** Retarget to a Cortex-M4F with 512K flash and 128K RAM:

```toml
# rust-toolchain.toml
targets = ["thumbv7em-none-eabihf"]

# .cargo/config.toml
[target.thumbv7em-none-eabihf]
rustflags = ["-C", "link-arg=-Tlink.x"]
runner = "probe-rs run --chip STM32F411CEUx"
```

```text
/* crates/NAME-firmware/memory.x */
MEMORY
{
  FLASH : ORIGIN = 0x08000000, LENGTH = 512K
  RAM : ORIGIN = 0x20000000, LENGTH = 128K
}
```

Then run `python3 tools/check.py setup-rust` to install the new target.
Take the chip values from the datasheet. The ones above illustrate the
format only.

**Cost removed.** A half-retargeted project. If `.cargo/config.toml` still
names the old triple, the new target links without `-Tlink.x`. The build then
fails, or it produces an image with no vector table.

**Verify.**

1. `python3 tools/check.py` prints
   `Cross-target packages compiled for thumbv7em-none-eabihf.`
1. Adding a HAL or PAC crate can pull in `cortex-m`, which brings in the
   unmaintained `bare-metal` crate. `cargo deny` then fails. Report that to
   the user rather than adding an advisory ignore. See
   [Policy exceptions](#policy-exceptions).

## Add a WASM export

**Definition.** A new function that the WebAssembly host calls by name. Put
the logic in the portable library, where host tests run. Add a thin export
in `crates/NAME-wasm/src/exports.rs` with the same attribute set as the
existing export.

**Use when.**

- A host needs a new numeric entry point.

**Do not use when.**

- The function needs strings, slices, or objects across the boundary. That
  needs an explicit memory protocol (pointer and length plus an allocator
  export) or `wasm-bindgen`. Design it with the user first.

**Example.**

```rust
/// Returns the checksum of the built-in sample.
// SAFETY: No other symbol in the module exports this name.
#[expect(unsafe_code, reason = "exports a C ABI symbol to the WebAssembly host")]
#[unsafe(no_mangle)]
const extern "C" fn sample_checksum() -> u32 {
    codec::sample_checksum()
}
```

Here `codec::sample_checksum` is a `pub const fn` in the portable library,
with its test in `<module>/tests.rs`. If the library function cannot be
`const`, drop `const` from the export too. The lint
`missing_const_for_fn` only fires when `const` is possible.

**Cost removed.** Gate failures from `pub` exports (`unreachable_pub`),
crate-wide `allow(unsafe_code)`, and logic in a package that host tests never
run.

**Verify.**

1. `python3 tools/check.py` passes. The library's new test runs in the
   host lanes.
1. Instantiate the module in Bun and call the export, as in the WASM profile
   card.

## Add a Tauri command

**Definition.** A Rust function that the frontend calls with
`invoke("name")`. Define it in `src-tauri/src/application/commands.rs` as a
`#[tauri::command] pub(super) fn`, and add it to the
`tauri::generate_handler![...]` list in `application.rs`. The existing
`#[expect(clippy::wildcard_imports)]` on `run` covers every command in the
list.

**Use when.**

- The frontend needs data or an action from Rust.

**Do not use when.**

- An official plugin already provides the capability (file system, dialogs,
  shell). Add it with `bunx tauri add PLUGIN` and grant only the permissions
  that you need in `src-tauri/capabilities/default.json`. Then run the gate.
  Plugins can add licenses or advisories outside the profile list. See
  [Policy exceptions](#policy-exceptions).

**Example.**

```rust
/// Returns the greeting shown in the main window.
#[tauri::command]
#[must_use]
pub(super) fn greeting(name: &str) -> String {
    format!("Hello, {name}")
}
```

```rust
.invoke_handler(tauri::generate_handler![
    commands::project_name,
    commands::greeting
])
```

```ts
const text = await invoke<string>("greeting", { name: "Ada" });
```

Add a unit test for `greeting` in `application/commands/tests.rs`.

**Cost removed.** Commands that compile but fail at runtime because they were
never registered. Also `pub` commands that trip `unreachable_pub`, and new
crate-wide `allow`s.

**Verify.**

1. `python3 tools/check.py` passes, including `bun run typecheck` for the
   new `invoke` call.
1. `bun run tauri dev` shows the result. This is a manual check, so report
   whether you ran it.

## Policy exceptions

**Definition.** `tools/check.py` keeps a fixed `PROFILES` table: which lints
each profile package may lower to `deny`, and which licenses, duplicate
versions, and advisories `deny.toml` may admit. The gate fails on any
difference in either direction.

**Use when.**

- The gate reports a `[lints]` mismatch, an unapproved license or advisory,
  or a `multiple-versions` value.

**Do not use when.**

- The failure can be fixed in code, or by choosing a different dependency or
  feature set. Do that first. For example, disable a crate's default features
  that pull in the offending graph.

**Example.** A new dependency brings in a GPL license. Stop and tell the user
which crate brings it in: `cargo tree -i CRATE`. Then describe the options,
such as a different crate or an explicit exception. Only with the user's
explicit authorization, add the license to that profile's `licenses` in
`tools/check.py`, next to a comment giving the reason, and to `deny.toml`.

**Cost removed.** Silent policy erosion. An exception added to pass one build
stays after the dependency that needed it is gone. `deny.toml` also sets
`unused-ignored-advisory = "deny"` so that stale advisory ignores fail.

**Verify.**

1. `python3 tools/check.py` passes with the exception.
1. The report names the exception, its reason, and who authorized it.
