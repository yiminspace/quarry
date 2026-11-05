fn main() {
    // include_dir embeds a directory whose file names change on every Vite build.
    // Watch the directory, not only files known by the previous expansion.
    println!("cargo:rerun-if-changed=../../src/quarry/web_dist");
}
