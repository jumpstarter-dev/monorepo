use std::env;
use std::fs::File;
use std::io::Write;
use std::path::PathBuf;

fn main() {
    let out = PathBuf::from(env::var_os("OUT_DIR").expect("OUT_DIR"));
    File::create(out.join("memory.x"))
        .expect("memory.x in OUT_DIR")
        .write_all(include_bytes!("memory.x"))
        .expect("write memory.x");
    println!("cargo:rustc-link-search={}", out.display());
    println!("cargo:rerun-if-changed=memory.x");
}
