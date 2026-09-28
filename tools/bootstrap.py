"""Render a clean Rust project from this policy template."""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import struct
import subprocess
import sys
import tempfile
import zlib
from dataclasses import dataclass
from pathlib import Path
from time import localtime

import check

ROOT = Path(__file__).resolve().parents[1]
NAME = re.compile(r"[a-z][a-z0-9]*(?:-[a-z0-9]+)*\Z")
PROFILE_NAMES = ("crate", "tauri", "wasm", "embedded")
TARGETS = {"embedded": "thumbv6m-none-eabi", "wasm": "wasm32v1-none"}
# Each advisory has no fixed release reachable from the pinned Tauri 2 Linux stack.
ADVISORY_REASONS = {
    "RUSTSEC-2024-0370": "proc-macro-error is unmaintained; glib-macros in Tauri's GTK 0.18 stack still depends on it",
    "RUSTSEC-2024-0429": "glib 0.18 VariantStrIter is unsound; Tauri's GTK 0.18 stack has no release on a fixed glib",
}


class BootstrapError(Exception):
    """The requested project cannot be rendered safely."""


@dataclass(frozen=True)
class Options:
    name: str
    author: str
    year: int
    shape: str
    role: str
    mode: str
    profile: str = "crate"

    @property
    def crate(self) -> str:
        return self.name.replace("-", "_")

    @property
    def package_dir(self) -> str:
        return "." if self.shape == "root" else f"crates/{self.name}"

    @property
    def profile_package(self) -> str | None:
        return {
            "embedded": f"{self.name}-firmware",
            "wasm": f"{self.name}-wasm",
            "tauri": self.name,
        }.get(self.profile)

    @property
    def targets(self) -> tuple[str, ...]:
        if self.profile in TARGETS:
            return (TARGETS[self.profile],)
        return ("thumbv6m-none-eabi",) if self.mode == "portable" else ()


def _policy_sections(portable: bool) -> str:
    source = (ROOT / "Cargo.toml").read_text(encoding="utf-8")
    lints = source[
        source.index("[workspace.lints.rust]") : source.index("[profile.release]")
    ]
    release = source[
        source.index("[profile.release]") : source.index("[profile.no-std]")
    ]
    profiles = (
        '\n[profile.no-std]\ninherits = "release"\n\n[profile.std]\ninherits = "release"\n'
        if portable
        else ""
    )
    return f"{lints}{release.rstrip()}\n{profiles}"


def _package_manifest(options: Options) -> str:
    author = options.author.replace("\\", "\\\\").replace(chr(34), '\\"')
    body = f'''[package]
name = "{options.name}"
version = "0.1.0"
description = "{options.name}"
authors = ["{author}"]
edition.workspace = true
rust-version.workspace = true
license.workspace = true
publish = false

[lints]
workspace = true
'''
    if options.mode == "portable":
        body += """
[features]
default = []
std = []
unsafe-code = []
"""
    return body


def _toml_list(values: list[str]) -> str:
    return "[" + ", ".join(f'"{value}"' for value in values) + "]"


def _root_manifest(options: Options) -> str:
    members = [] if options.shape == "root" else [options.package_dir]
    default_members = ""
    if options.profile == "tauri":
        members = ["src-tauri"]
    elif options.profile in TARGETS:
        members.append(f"crates/{options.profile_package}")
        # Host `cargo build` and `cargo test` skip the target-only package.
        default_members = f"default-members = {_toml_list([options.package_dir])}\n"
    package = _package_manifest(options) + "\n" if options.shape == "root" else ""
    portable = f'["{options.name}"]' if options.mode == "portable" else "[]"
    profile = ""
    if options.profile_package is not None:
        profile = (
            f'project-profile = "{options.profile}"\n'
            f"profile-packages = {_toml_list([options.profile_package])}\n"
        )
    return f"""{package}[workspace]
resolver = "3"
members = {_toml_list(members)}
{default_members}
[workspace.package]
edition = "2024"
rust-version = "1.98"
license = "MIT"

[workspace.metadata.rust-policy]
portable-packages = {portable}
cargo-deny-version = "0.20.2"
{profile}
{_policy_sections(options.mode == "portable")}"""


def _lib_source(options: Options) -> str:
    gates = ""
    if options.mode == "portable":
        gates = '#![cfg_attr(not(feature = "std"), no_std)]\n#![cfg_attr(not(feature = "unsafe-code"), forbid(unsafe_code))]\n\n'
    return f"""//! {options.name} library.

{gates}mod application;

pub use application::project_name;
"""


def _application_source(options: Options, library: bool) -> str:
    if library:
        implementation = f'''/// Returns this package's Cargo name.
#[must_use]
pub const fn project_name() -> &'static str {{
    "{options.name}"
}}
'''
    else:
        implementation = """/// Runs the application.
pub const fn run() {}
"""
    return implementation + "\n#[cfg(test)]\nmod tests;\n"


def _application_tests(options: Options, library: bool) -> str:
    if library:
        return f'''use super::project_name;

#[test]
fn reports_the_package_name() {{
    assert_eq!(
        project_name(),
        "{options.name}",
        "the public name must match Cargo metadata"
    );
}}
'''
    return """use super::run;

#[test]
fn application_runs() {
    run();
}
"""


PROFILE_README = {
    "embedded": """
## Embedded firmware

`crates/{name}` is the portable `no_std` library. `crates/{name}-firmware` is a
`cortex-m-rt` binary for `thumbv6m-none-eabi` (Cortex-M0/M0+) that links the library.
Before flashing real hardware:

- Replace `crates/{name}-firmware/memory.x` with your chip's flash and RAM regions.
- For another core, change the target in `rust-toolchain.toml`, `.cargo/config.toml`,
  and `.github/workflows/check.yml`, for example `thumbv7em-none-eabihf`.
- Add your board's PAC or HAL crate to the firmware package. Keep hardware access out of the portable library.
- Set the `probe-rs` runner in `.cargo/config.toml` to flash with `cargo run`.

Build the image:

```sh
rustup run 1.98.1 cargo build --package {name}-firmware --profile no-std --target thumbv6m-none-eabi
```

`tools/check.py` compiles and links the firmware. It never runs it on hardware.
The firmware package lowers `rust_2024_compatibility` from `forbid` to `deny` because
`#[cortex_m_rt::entry]` expands to `#[allow(static_mut_refs)]`. The gate rejects any other change.
""",
    "wasm": """
## WebAssembly

`crates/{name}` is the portable `no_std` library. `crates/{name}-wasm` is a `cdylib` for
`wasm32v1-none` that exports C ABI functions without `wasm-bindgen` or a JavaScript runtime.

```sh
rustup run 1.98.1 cargo build --package {name}-wasm --profile no-std --target wasm32v1-none
```

Load the module from any WebAssembly host, for example Bun:

```ts
const bytes = await Bun.file("target/wasm32v1-none/no-std/{crate}_wasm.wasm").arrayBuffer();
const {{ instance }} = await WebAssembly.instantiate(bytes);
console.log((instance.exports.project_name_len as () => number)());
```

Each export needs `#[unsafe(no_mangle)]`, a `SAFETY` comment naming why its symbol is unique,
and an `#[expect(unsafe_code, reason = ...)]`. The crate root keeps `#![deny(unsafe_code)]`.
""",
    "tauri": """
## Tauri desktop app

The Vite and TypeScript frontend lives at the repository root. The Rust application lives in
`src-tauri`. Bun is the only JavaScript runtime and package manager. It must be 1.4.2 or newer.

```sh
bun install
bun run tauri dev
bun run tauri build
```

Linux builds need the WebKitGTK system packages listed in `.github/workflows/check.yml`.
Replace the placeholder icons with `bun run tauri icon <source.png>`. Change
`identifier` in `src-tauri/tauri.conf.json` before publishing.

`tools/check.py` installs the frozen Bun lockfile, type-checks, and builds the frontend before
Cargo runs, because `tauri::generate_context!` embeds `dist` at compile time.
The `src-tauri` package lowers these lints from `forbid` to `deny` because Tauri's macros
emit `allow` attributes or code that trips them: `unused`, `clippy::all`, `clippy::pedantic`,
`clippy::exit`, and `clippy::large_stack_frames`. `deny.toml` admits the MPL-2.0 and
LLVM-exception licenses, duplicate crate versions, and two GTK advisories that Tauri's Linux
stack has no fixed release for. The gate rejects any other exception.
""",
}


def _readme(options: Options) -> str:
    if options.profile == "tauri":
        return f"""# {options.name}

Tauri 2 desktop application with a Bun, Vite, and TypeScript frontend.
{PROFILE_README["tauri"]}
{_readme_checks(options)}"""
    location = (
        "the repository root" if options.shape == "root" else f"`crates/{options.name}`"
    )
    portability = ""
    if options.mode == "portable":
        portability = " The library supports `no_std`."
        if options.profile == "crate":
            portability += " Replace the smoke target with each real supported target."
    extra = PROFILE_README.get(options.profile, "").format(
        name=options.name, crate=options.crate
    )
    return f"""# {options.name}

Rust {options.role} project. The first package is at {location}.{portability}
{extra}
{_readme_checks(options)}"""


def _readme_checks(options: Options) -> str:
    tools = (
        "Python 3.11+, rustup, and Bun 1.4.2+"
        if options.profile == "tauri"
        else "Python 3.11+ and rustup"
    )
    return f"""## Checks

Install {tools} before running these commands.

```sh
python3 tools/check.py setup-rust
rustup run 1.98.1 cargo install cargo-deny --locked --version 0.20.2
python3 tools/check.py
```

Keep `lib.rs`, `main.rs`, and every `mod.rs` limited to attributes, module declarations,
re-exports, and entry-point composition. Put implementation in ordinary modules.
Put unit tests in adjacent `<module>/tests.rs` files. Package integration and end-to-end tests live
under that package's top-level `tests/` directory. The same boundaries apply to nested
subsystems and independently owned workspace crates.

`.agents/skills` holds agent skills for this project. Agents that read that directory
(Codex, Gemini CLI) load them directly; install them for Claude Code with
`bunx skills add xsyetopz/rust-template`.
"""


PROFILE_AGENTS = {
    "embedded": "The firmware package links for its target only. Never claim hardware execution from a successful build.\n",
    "wasm": "The WebAssembly package builds for `wasm32v1-none` only. Keep every export's `SAFETY` comment accurate.\n",
    "tauri": "Use Bun and `bunx` only, never Node, npm, or npx. Keep `bun.lock` frozen and reviewed.\n",
}


def _agents(options: Options) -> str:
    return """# Repository instructions

Run `python3 tools/check.py quick` during edits.
Run `python3 tools/check.py` before acceptance.
Fix diagnostics at their cause. Do not weaken policy or add lint suppressions.
Keep wiring files (`lib.rs`, `main.rs`, and `mod.rs`) free of implementation. Keep unit
tests adjacent to their module and integration tests in each package's `tests/` directory.
Preserve unrelated files, manifests, lockfiles, CI, target settings, and licenses.
Profile packages carry only the lint and dependency exceptions that `tools/check.py` permits.
""" + PROFILE_AGENTS.get(options.profile, "")


def _license(options: Options) -> str:
    original = (ROOT / "LICENSE").read_text(encoding="utf-8")
    marker = "Copyright (c) 2026 Krystian J."
    return original.replace(
        marker, f"{marker}\nCopyright (c) {options.year} {options.author}", 1
    )


def _toolchain(options: Options) -> str:
    targets = (
        f"\ntargets = {_toml_list(list(options.targets))}" if options.targets else ""
    )
    return f"""[toolchain]
channel = "1.98.1"
profile = "minimal"
components = ["clippy", "rustfmt"]{targets}
"""


def _workflow(options: Options) -> str:
    workflow = (ROOT / ".github/workflows/check.yml").read_text()
    workflow = _replace_once(
        workflow,
        "      - run: rustup run 1.98.1 cargo install cargo-generate --locked --version 0.24.0\n",
        "",
    )
    targets = (
        f"          targets: {','.join(options.targets)}\n" if options.targets else ""
    )
    workflow = _replace_once(
        workflow, "          targets: thumbv6m-none-eabi,wasm32v1-none\n", targets
    )
    # The template's own tests generate Tauri projects, so its workflow carries
    # the Bun and WebKitGTK steps. Only generated Tauri projects keep them.
    tauri_steps = workflow[
        workflow.index("      - uses: oven-sh/setup-bun@") : workflow.index(
            "      - run: rustup run 1.98.1 cargo install cargo-deny"
        )
    ]
    if options.profile != "tauri":
        workflow = _replace_once(workflow, tauri_steps, "")
    return workflow


def _dependabot(options: Options) -> str:
    text = (ROOT / ".github/dependabot.yml").read_text(encoding="utf-8")
    if options.profile == "tauri":
        text += """  - package-ecosystem: bun
    directory: /
    schedule:
      interval: weekly
"""
    return text


def _gitignore(options: Options) -> str:
    text = "/target/\n__pycache__/\n*.py[cod]\n"
    if options.profile == "tauri":
        text += "/node_modules/\n/dist/\n/src-tauri/gen/schemas/\n"
    return text


def _skill_files() -> dict[str, bytes]:
    skills = ROOT / ".agents"
    if not skills.is_dir():
        return {}
    return {
        str(path.relative_to(ROOT)): path.read_bytes()
        for path in sorted(skills.rglob("*"))
        if path.is_file()
    }


def _member_manifest(name: str, description: str, body: str, lints: str) -> str:
    return f"""[package]
name = "{name}"
version = "0.1.0"
description = "{description}"
edition.workspace = true
rust-version.workspace = true
license.workspace = true
publish = false
{body}
{lints}"""


def _profile_lints(options: Options) -> str:
    """Return the member [lints] table that tools/check.py requires for the profile."""
    lowered = check.PROFILES[options.profile].lowered_lints
    if not lowered:
        return "[lints]\nworkspace = true\n"
    source = (ROOT / "Cargo.toml").read_text(encoding="utf-8")
    sections = source[
        source.index("[workspace.lints.rust]") : source.index("[profile.release]")
    ]
    rust, clippy = sections.split("[workspace.lints.clippy]")
    tables = {"rust": rust, "clippy": "[workspace.lints.clippy]" + clippy}
    for namespace, names in lowered.items():
        for name in names:
            tables[namespace], count = re.subn(
                rf'^({re.escape(name)} = (?:\{{ level = )?)"forbid"',
                r'\1"deny"',
                tables[namespace],
                flags=re.MULTILINE,
            )
            if count != 1:
                raise BootstrapError(f"workspace lint {namespace}::{name} is missing")
    return (tables["rust"] + tables["clippy"]).replace(
        "[workspace.lints.", "[lints."
    ).rstrip() + "\n"


def _deny(options: Options) -> str:
    text = (ROOT / "deny.toml").read_text(encoding="utf-8")
    if options.profile_package is None:
        return text
    profile = check.PROFILES[options.profile]
    if profile.licenses:
        added = "".join(f'    "{name}",\n' for name in sorted(profile.licenses))
        text = _replace_once(
            text,
            '    "Zlib",\n]',
            f'    "Zlib",\n    # Required by the {options.profile} profile dependency graph.\n{added}]',
        )
    if profile.duplicate_versions:
        text = _replace_once(
            text,
            'multiple-versions = "deny"',
            f"# The {options.profile} framework graph pins several major versions of shared crates.\n"
            'multiple-versions = "allow"',
        )
    if profile.advisories:
        entries = "".join(
            f'    {{ id = "{advisory}", reason = "{ADVISORY_REASONS[advisory]}" }},\n'
            for advisory in sorted(profile.advisories)
        )
        text = _replace_once(text, "ignore = []", f"ignore = [\n{entries}]")
    return text


def _replace_once(text: str, old: str, new: str) -> str:
    if text.count(old) != 1:
        raise BootstrapError(f"template configuration changed: {old!r} is not unique")
    return text.replace(old, new)


def _embedded_files(options: Options) -> dict[str, str]:
    package = f"crates/{options.profile_package}"
    target = TARGETS["embedded"]
    return {
        ".cargo/config.toml": f"""[target.{target}]
# cortex-m-rt provides link.x, which includes this package's memory.x.
rustflags = ["-C", "link-arg=-Tlink.x"]
# Set your probe-rs chip name, then uncomment to flash and run with `cargo run`.
# runner = "probe-rs run --chip <CHIP>"
""",
        f"{package}/Cargo.toml": _member_manifest(
            options.profile_package,
            f"{options.name} firmware",
            f"""
[[bin]]
name = "{options.profile_package}"
test = false
bench = false

[dependencies]
{options.name} = {{ path = "../{options.name}", default-features = false }}
cortex-m-rt = "0.7.7"
panic-halt = "1.0.0"
""",
            _profile_lints(options),
        ),
        f"{package}/memory.x": """/* Placeholder layout. Replace both regions with your chip's datasheet values. */
MEMORY
{
  FLASH : ORIGIN = 0x00000000, LENGTH = 256K
  RAM : ORIGIN = 0x20000000, LENGTH = 32K
}
""",
        f"{package}/build.rs": """//! Places `memory.x` on the linker search path for `cortex-m-rt`.

use std::{env, fs, io, path::PathBuf};

fn main() -> io::Result<()> {
    let out = PathBuf::from(env::var_os("OUT_DIR").ok_or(io::ErrorKind::NotFound)?);
    fs::write(out.join("memory.x"), include_bytes!("memory.x"))?;
    println!("cargo:rustc-link-search={}", out.display());
    println!("cargo:rerun-if-changed=memory.x");
    Ok(())
}
""",
        f"{package}/src/main.rs": f"""//! {options.name} firmware.

#![no_std]
#![no_main]

use panic_halt as _;

mod firmware;
""",
        f"{package}/src/firmware.rs": f"""//! Reset handler for the firmware image.

/// Runs after reset and never returns.
#[cortex_m_rt::entry]
fn main() -> ! {{
    let _name = {options.crate}::project_name();
    loop {{
        core::hint::spin_loop();
    }}
}}
""",
    }


def _wasm_files(options: Options) -> dict[str, str]:
    package = f"crates/{options.profile_package}"
    return {
        f"{package}/Cargo.toml": _member_manifest(
            options.profile_package,
            f"{options.name} WebAssembly exports",
            f"""
[lib]
crate-type = ["cdylib"]
test = false
doctest = false

[dependencies]
{options.name} = {{ path = "../{options.name}", default-features = false }}
""",
            _profile_lints(options),
        ),
        f"{package}/src/lib.rs": f"""//! {options.name} WebAssembly exports.

#![no_std]
#![deny(unsafe_code)]

mod exports;
mod panic;
""",
        f"{package}/src/exports.rs": f"""//! Functions that the WebAssembly host imports by name.

/// Returns the byte length of this package's Cargo name.
// SAFETY: No other symbol in the module exports this name.
#[expect(unsafe_code, reason = "exports a C ABI symbol to the WebAssembly host")]
#[unsafe(no_mangle)]
const extern "C" fn project_name_len() -> usize {{
    {options.crate}::project_name().len()
}}
""",
        f"{package}/src/panic.rs": """//! Traps the WebAssembly instance on panic.

use core::panic::PanicInfo;

#[panic_handler]
fn panic(_info: &PanicInfo<'_>) -> ! {
    core::arch::wasm32::unreachable()
}
""",
    }


def _png_icon(size: int = 32) -> bytes:
    """Return a solid RGBA PNG; Tauri requires RGBA icons."""

    def chunk(kind: bytes, data: bytes) -> bytes:
        return (
            struct.pack(">I", len(data))
            + kind
            + data
            + struct.pack(">I", zlib.crc32(kind + data))
        )

    row = b"\x00" + bytes((36, 200, 219, 255)) * size
    header = struct.pack(">IIBBBBB", size, size, 8, 6, 0, 0, 0)
    return (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", header)
        + chunk(b"IDAT", zlib.compress(row * size, 9))
        + chunk(b"IEND", b"")
    )


def _ico_icon(png: bytes, size: int = 32) -> bytes:
    """Wrap a PNG in an ICO container, which Windows resource compilation requires."""
    return (
        struct.pack("<HHH", 0, 1, 1)
        + struct.pack("<BBBBHHII", size, size, 0, 0, 1, 32, len(png), 22)
        + png
    )


def _json(value: object) -> str:
    return json.dumps(value, indent=2) + "\n"


def _tsconfig(lib: list[str], types: list[str], include: list[str]) -> str:
    return _json(
        {
            "compilerOptions": {
                "target": "ES2023",
                "module": "ESNext",
                "moduleResolution": "bundler",
                "lib": lib,
                "types": types,
                "strict": True,
                "noUncheckedIndexedAccess": True,
                "exactOptionalPropertyTypes": True,
                "noFallthroughCasesInSwitch": True,
                "noImplicitOverride": True,
                "noUnusedLocals": True,
                "noUnusedParameters": True,
                "verbatimModuleSyntax": True,
                "isolatedModules": True,
                "skipLibCheck": False,
                "noEmit": True,
            },
            "include": include,
        }
    )


def _tauri_files(options: Options) -> dict[str, str | bytes]:
    icon = _png_icon()
    library = f"{options.crate}_lib"
    return {
        "package.json": _json(
            {
                "name": options.name,
                "private": True,
                "type": "module",
                "engines": {"bun": ">=1.4.2"},
                "scripts": {
                    "dev": "bun --bun vite",
                    "build": "bun --bun vite build",
                    "typecheck": "tsc -p tsconfig.json && tsc -p tsconfig.config.json",
                    "tauri": "tauri",
                },
                "dependencies": {"@tauri-apps/api": "2.12.0"},
                "devDependencies": {
                    "@tauri-apps/cli": "2.12.0",
                    "@types/bun": "1.4.2",
                    "typescript": "7.0.2",
                    "vite": "8.3.1",
                },
            }
        ),
        # vite/client and bun both declare ImportMeta, so each program loads one.
        "tsconfig.json": _tsconfig(
            ["ES2023", "ESNext.Disposable", "DOM", "DOM.Iterable"],
            ["vite/client"],
            ["src"],
        ),
        "tsconfig.config.json": _tsconfig(
            ["ES2023", "ESNext.Disposable"], ["bun"], ["vite.config.ts"]
        ),
        "vite.config.ts": """import { defineConfig } from "vite";

export default defineConfig({
  clearScreen: false,
  server: { port: 5173, strictPort: true },
  envPrefix: ["VITE_", "TAURI_ENV_"],
  build: { target: "es2023", outDir: "dist", emptyOutDir: true },
});
""",
        "index.html": f"""<!doctype html>
<html lang="en">
  <head>
    <meta charset="UTF-8" />
    <meta name="viewport" content="width=device-width, initial-scale=1.0" />
    <title>{options.name}</title>
  </head>
  <body>
    <main>
      <h1 id="name"></h1>
    </main>
    <script type="module" src="/src/main.ts"></script>
  </body>
</html>
""",
        "src/main.ts": """import { invoke } from "@tauri-apps/api/core";

const heading = document.querySelector<HTMLHeadingElement>("#name");
if (heading !== null) {
  heading.textContent = await invoke<string>("project_name");
}
""",
        "src-tauri/Cargo.toml": _member_manifest(
            options.name,
            options.name,
            f"""
[lib]
name = "{library}"

[build-dependencies]
tauri-build = {{ version = "2.7.0", features = [] }}

[dependencies]
tauri = {{ version = "2.12.0", features = [] }}
""",
            _profile_lints(options),
        ),
        "src-tauri/build.rs": """//! Generates Tauri's build-time context.

fn main() {
    tauri_build::build();
}
""",
        "src-tauri/tauri.conf.json": _json(
            {
                "$schema": "https://schema.tauri.app/config/2",
                "productName": options.name,
                "version": "0.1.0",
                "identifier": f"com.example.{options.name}",
                "build": {
                    "beforeDevCommand": "bun run dev",
                    "devUrl": "http://localhost:5173",
                    "beforeBuildCommand": "bun run build",
                    "frontendDist": "../dist",
                },
                "app": {
                    "windows": [{"title": options.name, "width": 800, "height": 600}],
                    "security": {
                        "csp": "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; connect-src ipc: http://ipc.localhost"
                    },
                },
                "bundle": {
                    "active": True,
                    "targets": "all",
                    "icon": ["icons/icon.png", "icons/icon.ico"],
                },
            }
        ),
        "src-tauri/capabilities/default.json": _json(
            {
                "$schema": "../gen/schemas/desktop-schema.json",
                "identifier": "default",
                "description": "Permissions for the main window",
                "windows": ["main"],
                "permissions": ["core:default"],
            }
        ),
        "src-tauri/icons/icon.png": icon,
        "src-tauri/icons/icon.ico": _ico_icon(icon),
        "src-tauri/src/lib.rs": f"""//! {options.name} desktop application.

#![forbid(unsafe_code)]

mod application;

pub use application::run;
""",
        "src-tauri/src/main.rs": f"""//! {options.name} executable.

#![cfg_attr(not(debug_assertions), windows_subsystem = "windows")]

fn main() -> tauri::Result<()> {{
    {library}::run()
}}
""",
        "src-tauri/src/application.rs": """//! Tauri application builder.

mod commands;

/// Builds and runs the application until its event loop exits.
///
/// # Errors
///
/// Returns an error when Tauri cannot create the runtime or a window.
#[expect(
    clippy::exit,
    reason = "tauri::generate_context! expands to the runtime's process exit path"
)]
#[expect(
    clippy::large_stack_frames,
    reason = "tauri::generate_context! builds the embedded asset context on the stack"
)]
// Clippy skips wildcard_imports in test crates, so the expectation applies outside tests only.
#[cfg_attr(
    not(test),
    expect(
        clippy::wildcard_imports,
        reason = "tauri::generate_handler! expands each command's helper macro to a wildcard import"
    )
)]
pub fn run() -> tauri::Result<()> {
    tauri::Builder::default()
        .invoke_handler(tauri::generate_handler![commands::project_name])
        .run(tauri::generate_context!())
}
""",
        "src-tauri/src/application/commands.rs": """//! Commands that the frontend invokes by name.

/// Returns this package's Cargo name to the frontend.
#[tauri::command]
#[must_use]
pub(super) const fn project_name() -> &'static str {
    env!("CARGO_PKG_NAME")
}

#[cfg(test)]
mod tests;
""",
        "src-tauri/src/application/commands/tests.rs": f"""use super::project_name;

#[test]
fn reports_the_package_name() {{
    assert_eq!(
        project_name(),
        "{options.name}",
        "the command must return the Cargo package name"
    );
}}
""",
    }


def render(options: Options) -> dict[str, bytes]:
    package = Path(options.package_dir)
    files: dict[str, str | bytes] = {
        "Cargo.toml": _root_manifest(options),
        "README.md": _readme(options),
        "AGENTS.md": _agents(options),
        "LICENSE": _license(options),
        ".gitignore": _gitignore(options),
        "rust-toolchain.toml": _toolchain(options),
        ".github/workflows/check.yml": _workflow(options),
        ".github/dependabot.yml": _dependabot(options),
        "deny.toml": _deny(options),
        **_skill_files(),
    }
    for name in (
        "rustfmt.toml",
        "clippy.toml",
        ".clippy-test/clippy.toml",
        "tools/check.py",
    ):
        files[name] = (ROOT / name).read_bytes()
    if options.profile == "tauri":
        files.update(_tauri_files(options))
    else:
        files.update(_crate_files(options, package))
    if options.profile == "embedded":
        files.update(_embedded_files(options))
    elif options.profile == "wasm":
        files.update(_wasm_files(options))
    return {
        name: value if isinstance(value, bytes) else value.encode()
        for name, value in files.items()
    }


def _crate_files(options: Options, package: Path) -> dict[str, str]:
    files = {}
    if options.shape == "workspace":
        files[str(package / "Cargo.toml")] = _package_manifest(options)
    has_lib = options.role in {"lib", "both"}
    if has_lib:
        files[str(package / "src/lib.rs")] = _lib_source(options)
    files[str(package / "src/application.rs")] = _application_source(options, has_lib)
    files[str(package / "src/application/tests.rs")] = _application_tests(
        options, has_lib
    )
    if options.role in {"bin", "both"}:
        entry = (
            f"const fn main() {{\n    let _project_name = {options.crate}::project_name();\n}}\n"
            if has_lib
            else "mod application;\n\npub use application::run as main;\n"
        )
        files[str(package / "src/main.rs")] = (
            f"//! {options.name} executable.\n\n{entry}"
        )
    return files


def _write_tree(
    destination: Path, files: dict[str, bytes], *, replace_template: bool = False
) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(dir=destination.parent) as directory:
        staging = Path(directory) / "project"
        staging.mkdir()
        for name, content in files.items():
            path = staging / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(content)
        cargo = subprocess.run(
            ["rustup", "which", "--toolchain", "1.98.1", "cargo"],
            check=True,
            text=True,
            stdout=subprocess.PIPE,
        ).stdout.strip()
        environment = os.environ.copy()
        environment["PATH"] = (
            f"{Path(cargo).parent}{os.pathsep}{environment.get('PATH', '')}"
        )
        subprocess.run(
            [cargo, "generate-lockfile"], cwd=staging, env=environment, check=True
        )
        if (staging / "package.json").exists():
            bun = shutil.which("bun")
            if bun is None:
                raise BootstrapError("the tauri profile requires Bun 1.4.2 or newer")
            subprocess.run([bun, "install", "--lockfile-only"], cwd=staging, check=True)
        if destination.exists():
            if any(destination.iterdir()) and not replace_template:
                raise BootstrapError(f"destination is not empty: {destination}")
            for child in destination.iterdir():
                if child.name != ".git":
                    shutil.rmtree(child) if child.is_dir() else child.unlink()
        else:
            destination.mkdir()
        for child in staging.iterdir():
            shutil.move(str(child), destination / child.name)


def parse_options(
    arguments: list[str] | None = None,
) -> tuple[Options, Path, bool, bool]:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--name")
    parser.add_argument("--author")
    parser.add_argument("--year", type=int, default=localtime().tm_year)
    parser.add_argument("--profile", choices=PROFILE_NAMES, default="crate")
    # These apply to the crate profile only. Other profiles fix their layout.
    parser.add_argument("--shape", choices=("root", "workspace"))
    parser.add_argument("--role", choices=("lib", "bin", "both"))
    parser.add_argument("--mode", choices=("host", "portable"))
    parser.add_argument("--output", type=Path)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument(
        "--replace-cargo-generate-template", action="store_true", help=argparse.SUPPRESS
    )
    parser.add_argument("--cargo-generate-values", type=Path, help=argparse.SUPPRESS)
    args = parser.parse_args(arguments)
    if args.cargo_generate_values:
        text = args.cargo_generate_values.read_text(encoding="utf-8").removesuffix("\n")
        name, separator, remaining = text.partition("\n")
        values = [name, *remaining.rsplit("\n", 5)] if separator else []
        if len(values) != 7:
            raise BootstrapError(
                "cargo-generate supplied invalid renderer values. Remove line breaks from the author and retry."
            )
        args.name, args.author, year, args.profile = values[:4]
        # cargo-generate leaves crate-only placeholders unset for other profiles.
        args.shape, args.role, args.mode = (value or None for value in values[4:])
        if year:
            try:
                args.year = int(year)
            except ValueError as error:
                raise BootstrapError("--year must be a positive integer") from error
    if args.name is None or args.author is None:
        raise BootstrapError("--name and --author are required")
    if not NAME.fullmatch(args.name):
        raise BootstrapError("--name must be Cargo-compatible kebab-case")
    if any(ord(character) < 32 or ord(character) == 127 for character in args.author):
        raise BootstrapError(
            "--author must not contain ASCII control characters. Remove them and retry."
        )
    if not args.author.strip():
        raise BootstrapError("--author must not be empty")
    if args.year < 1:
        raise BootstrapError("--year must be a positive integer")
    if args.profile not in PROFILE_NAMES:
        raise BootstrapError("cargo-generate supplied an unsupported profile")
    if args.profile == "crate":
        args.shape = args.shape or "root"
        args.role = args.role or "lib"
        args.mode = args.mode or "host"
    elif (args.shape, args.role, args.mode) != (None, None, None):
        raise BootstrapError(
            f"--shape, --role, and --mode apply only to the crate profile, not {args.profile}"
        )
    else:
        # Target profiles pair a portable library with one target-only package.
        args.shape, args.role, args.mode = (
            ("workspace", "both", "host")
            if args.profile == "tauri"
            else ("workspace", "lib", "portable")
        )
    if (
        args.shape not in {"root", "workspace"}
        or args.role not in {"lib", "bin", "both"}
        or args.mode not in {"host", "portable"}
    ):
        raise BootstrapError(
            "cargo-generate supplied an unsupported shape, role, or mode"
        )
    if args.mode == "portable" and args.role == "bin":
        raise BootstrapError(
            "portable binary-only projects require an explicit runtime, linker script, and board configuration"
        )
    options = Options(
        args.name,
        args.author.strip(),
        args.year,
        args.shape,
        args.role,
        args.mode,
        args.profile,
    )
    return (
        options,
        (args.output or Path(args.name)).resolve(),
        args.dry_run,
        args.replace_cargo_generate_template,
    )


def main(arguments: list[str] | None = None) -> int:
    try:
        options, destination, dry_run, replace_template = parse_options(arguments)
        files = render(options)
        if dry_run:
            generated = (
                ("Cargo.lock", "bun.lock")
                if "package.json" in files
                else ("Cargo.lock",)
            )
            print(
                f"Would generate {len(files) + len(generated)} files at {destination}"
            )
            for name in sorted((*files, *generated)):
                print(name)
        else:
            _write_tree(destination, files, replace_template=replace_template)
            print(f"Generated {options.name} at {destination}")
        return 0
    except (BootstrapError, OSError, subprocess.CalledProcessError) as error:
        print(f"FAILED: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
