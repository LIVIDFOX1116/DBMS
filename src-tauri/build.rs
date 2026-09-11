fn main() {
  // Exposes the target triple to src/backend.rs as env!("SIDECAR_TARGET_TRIPLE")
  // -- release-mode sidecar resolution needs it to build the exact filename
  // scripts/build_backend_sidecar.py produces (Tauri's externalBin naming
  // convention is `<name>-<target-triple>`). Cargo sets TARGET for build
  // scripts specifically so they don't have to shell out to `rustc -vV`
  // themselves; std::env::var here reads that, not the final binary's env.
  let target = std::env::var("TARGET").expect("Cargo always sets TARGET for build scripts");
  println!("cargo:rustc-env=SIDECAR_TARGET_TRIPLE={target}");

  tauri_build::build()
}
