//! Prints the exact GpuDetectionResult (DXGI + D3D12, see gpu.rs) this
//! machine produces right now -- for verifying dedicated/integrated
//! classification on real hardware, e.g. confirming an integrated-only
//! machine reports no dedicated adapter, without needing the desktop UI.

use app_lib::gpu;

fn main() {
    let result = gpu::detect();
    println!("{}", serde_json::to_string_pretty(&result).expect("GpuDetectionResult must serialize"));
}
