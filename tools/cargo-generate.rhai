// Crate-only placeholders are unset for other profiles; the renderer reads blanks as unset.
let shape = if variable::is_set("shape") { variable::get("shape") } else { "" };
let role = if variable::is_set("role") { variable::get("role") } else { "" };
let mode = if variable::is_set("mode") { variable::get("mode") } else { "" };

file::write(".cargo-generate-values", [
    variable::get("project-name"),
    variable::get("author"),
    variable::get("year"),
    variable::get("profile"),
    shape,
    role,
    mode,
]);

system::command("python3", [
    "tools/bootstrap.py", "--cargo-generate-values", ".cargo-generate-values",
    "--output", ".", "--replace-cargo-generate-template",
]);
