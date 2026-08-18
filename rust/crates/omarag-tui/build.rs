//! Embeds every theme under `assets/themes/` into the binary.
//!
//! Generated rather than hand-listed so that re-running
//! `scripts/import_themes.py` is the only step needed to add or drop a theme.

use std::{env, fs, path::PathBuf};

fn main() {
    let root = PathBuf::from(env!("CARGO_MANIFEST_DIR"))
        .join("..")
        .join("..")
        .join("..")
        .join("assets")
        .join("themes");

    println!("cargo:rerun-if-changed={}", root.display());

    let mut entries: Vec<PathBuf> = fs::read_dir(&root)
        .unwrap_or_else(|error| panic!("cannot read {}: {error}", root.display()))
        .filter_map(Result::ok)
        .map(|entry| entry.path())
        .filter(|path| path.extension().is_some_and(|ext| ext == "toml"))
        .collect();
    entries.sort();

    let mut generated = String::from(
        "/// Every bundled theme, as (file stem, TOML source).\n\
         pub(crate) static BUNDLED_THEMES: &[(&str, &str)] = &[\n",
    );
    for path in &entries {
        println!("cargo:rerun-if-changed={}", path.display());
        let stem = path.file_stem().unwrap().to_string_lossy();
        generated.push_str(&format!(
            "    ({:?}, include_str!({:?})),\n",
            stem,
            path.canonicalize().unwrap_or_else(|_| path.clone())
        ));
    }
    generated.push_str("];\n");

    let out = PathBuf::from(env::var("OUT_DIR").expect("OUT_DIR")).join("themes.rs");
    fs::write(&out, generated).expect("write themes.rs");
}
