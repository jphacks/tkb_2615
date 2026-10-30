//! Finds the lane modules: every `src/state/*.rs` and `src/rules/*.rs` becomes a module of
//! the crate and a Python submodule, so adding a file never edits `lib.rs`. Each file exports
//! `pub fn register(m: &Bound<'_, PyModule>) -> PyResult<()>`. The generated code is
//! `$OUT_DIR/lanes.rs`, included by `lib.rs`.

use std::env;
use std::fmt::Write as _;
use std::fs;
use std::path::{Path, PathBuf};

/// The modules of one directory, sorted so that the generated code does not depend on the
/// order the file system lists them in. A directory that does not exist has none.
fn modules(directory: &Path) -> Vec<(String, PathBuf)> {
    let Ok(entries) = fs::read_dir(directory) else {
        return Vec::new();
    };
    let mut found: Vec<(String, PathBuf)> = entries
        .map(|entry| entry.expect("a directory entry can be read").path())
        .filter(|path| path.extension().is_some_and(|extension| extension == "rs"))
        .map(|path| {
            let stem = path.file_stem().and_then(|stem| stem.to_str());
            let name = stem.expect("a module file name is UTF-8").to_owned();
            let valid = name.starts_with(|first: char| first.is_ascii_lowercase())
                && name
                    .chars()
                    .all(|c| c.is_ascii_lowercase() || c.is_ascii_digit() || c == '_');
            assert!(valid, "{} is not a snake_case module name", path.display());
            (name, path)
        })
        .collect();
    found.sort();
    found
}

fn main() {
    let root = PathBuf::from(env::var_os("CARGO_MANIFEST_DIR").expect("cargo sets this"));
    let mut declarations = String::new();
    let mut registrations = String::new();
    for group in ["state", "rules"] {
        let directory = root.join("src").join(group);
        // A directory is watched recursively, so a new file reruns this script.
        println!("cargo:rerun-if-changed=src/{group}");
        let found = modules(&directory);
        writeln!(declarations, "pub(crate) mod {group} {{").unwrap();
        writeln!(
            registrations,
            "    let group = pyo3::types::PyModule::new(m.py(), {group:?})?;"
        )
        .unwrap();
        for (name, path) in &found {
            // {:?} writes a Rust string literal, with the backslashes of a Windows path escaped.
            let path = path.to_str().expect("the source path is UTF-8");
            writeln!(declarations, "    #[path = {path:?}]\n    pub mod {name};").unwrap();
            writeln!(
                registrations,
                "    let module = pyo3::types::PyModule::new(m.py(), {name:?})?;\n    \
                 {group}::{name}::register(&module)?;\n    \
                 group.add({name:?}, module)?;"
            )
            .unwrap();
        }
        writeln!(declarations, "}}").unwrap();
        writeln!(registrations, "    m.add({group:?}, group)?;").unwrap();
    }
    let generated = format!(
        "{declarations}\n\
         fn register_lanes(m: &pyo3::Bound<'_, pyo3::types::PyModule>) -> pyo3::PyResult<()> {{\n\
         {registrations}    Ok(())\n}}\n"
    );
    let out = PathBuf::from(env::var_os("OUT_DIR").expect("cargo sets this"));
    fs::write(out.join("lanes.rs"), generated).expect("OUT_DIR is writable");
    println!("cargo:rerun-if-changed=build.rs");
}
