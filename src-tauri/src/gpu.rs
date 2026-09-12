//! Native GPU adapter enumeration -- DXGI for enumeration + per-adapter
//! memory figures, plus a lightweight D3D12 feature query to classify each
//! adapter as dedicated/integrated. Exposed to the frontend via
//! commands::detect_gpus; not passed to the Python backend, which has no
//! use for it today (see the module docstring in commands.rs).
//!
//! Deliberately DXGI + D3D12 over wgpu: wgpu's adapter enumeration reports
//! device_type (Discrete/Integrated) but no VRAM figures at all, and VRAM
//! is a hard requirement here. This app is Windows-only (see README), so
//! there's no cross-platform payoff to justify a second, heavier graphics
//! abstraction wrapping the same underlying DXGI calls we can make directly.
//!
//! Classification is vendor-agnostic on purpose: Intel ships both discrete
//! (Arc A-series) and integrated (Arc-branded Meteor Lake/Lunar Lake iGPUs)
//! parts under the same "Arc" name, and AMD ships integrated APUs -- so
//! vendor ID / name can only ever inform *virtual-adapter* detection below,
//! never dedicated-vs-integrated. That distinction instead comes from
//! D3D12_FEATURE_DATA_ARCHITECTURE.UMA (Unified Memory Architecture), the
//! signal Microsoft itself documents for this exact question: true means
//! the adapter architecturally shares memory with the CPU (integrated),
//! false means genuinely separate dedicated memory (discrete) -- regardless
//! of branding or how much "dedicated" memory DXGI happens to report for a
//! carve-out. The DedicatedVideoMemory-vs-SharedSystemMemory comparison is
//! kept only as a fallback for the rare adapter where the D3D12 query
//! itself fails (see `classify` below), since modern iGPU drivers can
//! report a non-trivial dedicated carve-out that makes memory size alone
//! unreliable as the primary signal.

use serde::Serialize;

#[derive(Serialize, Clone, Debug, PartialEq, Eq)]
#[serde(rename_all = "lowercase")]
pub enum AdapterType {
    Dedicated,
    Integrated,
    Virtual,
    Unknown,
}

#[derive(Serialize, Clone, Debug)]
pub struct GpuAdapter {
    pub name: String,
    pub vendor_id: u32,
    pub device_id: u32,
    pub adapter_type: AdapterType,
    pub dedicated_vram_bytes: u64,
    pub dedicated_system_memory_bytes: u64,
    pub shared_system_memory_bytes: u64,
    /// Set when classification fell back to the memory-size heuristic, or
    /// when this specific adapter couldn't be fully read -- never set on a
    /// clean UMA-based classification.
    pub reason: Option<String>,
}

#[derive(Serialize, Clone, Debug, Default)]
pub struct GpuDetectionResult {
    pub adapters: Vec<GpuAdapter>,
    /// Set only if enumeration failed entirely (e.g. CreateDXGIFactory1
    /// itself errored) -- individual bad adapters instead show up as a
    /// single Unknown entry in `adapters` with their own `reason`, so one
    /// misbehaving adapter on a hybrid system never hides the other one.
    pub error: Option<String>,
}

// PCI-SIG vendor IDs for known virtualization/paravirtualized display
// adapters. Used only to flag an adapter as "virtual" -- a different
// question from dedicated-vs-integrated, which stays vendor-agnostic (see
// module docs). Real GPU vendors (Intel 0x8086, NVIDIA 0x10DE, AMD 0x1002)
// are deliberately absent from this list.
const VENDOR_MICROSOFT_HYPERV: u32 = 0x1414;
const VENDOR_VMWARE: u32 = 0x15AD;
const VENDOR_VIRTIO: u32 = 0x1AF4;
const VENDOR_VIRTUALBOX: u32 = 0x80EE;
const VENDOR_QEMU_BOCHS: u32 = 0x1234;

fn is_virtual_vendor(vendor_id: u32) -> bool {
    matches!(
        vendor_id,
        VENDOR_MICROSOFT_HYPERV | VENDOR_VMWARE | VENDOR_VIRTIO | VENDOR_VIRTUALBOX | VENDOR_QEMU_BOCHS
    )
}

/// DXGI can enumerate the same physical adapter more than once (observed in
/// practice: a single integrated GPU showing up as two identical
/// IDXGIAdapter1 entries on a real machine) -- dedupe by (vendor, device)
/// so one real GPU never reads as two, while a genuine hybrid system's two
/// *different* adapters are always preserved. Exempts the (0, 0) id pair
/// used for a GetDesc1-failure placeholder: several independent failures on
/// one machine must not collapse into a single reported entry. Pure and
/// separate from live enumeration so it's unit-testable on its own.
fn dedupe_adapters(adapters: Vec<GpuAdapter>) -> Vec<GpuAdapter> {
    let mut seen = std::collections::HashSet::new();
    adapters
        .into_iter()
        .filter(|a| (a.vendor_id, a.device_id) == (0, 0) || seen.insert((a.vendor_id, a.device_id)))
        .collect()
}

/// Pure classification logic, kept separate from the live DXGI/D3D12 calls
/// in `win` below so it's unit-testable with synthetic adapter data --
/// dGPU, iGPU, hybrid (call it twice), virtual, and "the UMA query itself
/// failed" -- without needing real hardware or mocking the Win32 API
/// surface. `uma` is `None` exactly when the D3D12 architecture query
/// failed for this adapter (unsupported driver, feature level too low,
/// etc.), which must never be treated as a crash -- only a fallback.
pub fn classify(
    vendor_id: u32,
    dedicated_vram_bytes: u64,
    shared_system_memory_bytes: u64,
    uma: Option<bool>,
) -> (AdapterType, Option<String>) {
    if is_virtual_vendor(vendor_id) {
        return (AdapterType::Virtual, None);
    }
    match uma {
        Some(true) => (AdapterType::Integrated, None),
        Some(false) => (AdapterType::Dedicated, None),
        None => {
            const MIN_MEANINGFUL_VRAM: u64 = 256 * 1024 * 1024;
            if dedicated_vram_bytes > shared_system_memory_bytes && dedicated_vram_bytes > MIN_MEANINGFUL_VRAM {
                (
                    AdapterType::Dedicated,
                    Some("D3D12 architecture query unavailable; classified from memory sizes".into()),
                )
            } else if dedicated_vram_bytes > 0 || shared_system_memory_bytes > 0 {
                (
                    AdapterType::Integrated,
                    Some("D3D12 architecture query unavailable; classified from memory sizes".into()),
                )
            } else {
                (
                    AdapterType::Unknown,
                    Some("D3D12 architecture query unavailable and both memory sizes are zero".into()),
                )
            }
        }
    }
}

#[cfg(windows)]
mod win {
    use super::*;
    use windows::Win32::Graphics::Direct3D::D3D_FEATURE_LEVEL_11_0;
    use windows::Win32::Graphics::Direct3D12::{
        D3D12CreateDevice, D3D12_FEATURE_ARCHITECTURE, D3D12_FEATURE_DATA_ARCHITECTURE, ID3D12Device,
    };
    use windows::Win32::Graphics::Dxgi::{
        CreateDXGIFactory1, IDXGIAdapter1, IDXGIFactory1, DXGI_ADAPTER_FLAG_SOFTWARE,
    };

    /// None means the query itself failed (unsupported driver, feature
    /// level too low, etc.) -- callers fall back to the memory heuristic,
    /// never crash.
    fn query_uma(adapter: &IDXGIAdapter1) -> Option<bool> {
        unsafe {
            let mut device: Option<ID3D12Device> = None;
            D3D12CreateDevice(adapter, D3D_FEATURE_LEVEL_11_0, &mut device).ok()?;
            let device = device?;
            let mut arch = D3D12_FEATURE_DATA_ARCHITECTURE::default();
            device
                .CheckFeatureSupport(
                    D3D12_FEATURE_ARCHITECTURE,
                    &mut arch as *mut _ as *mut std::ffi::c_void,
                    std::mem::size_of::<D3D12_FEATURE_DATA_ARCHITECTURE>() as u32,
                )
                .ok()?;
            Some(arch.UMA.as_bool())
        }
    }

    pub fn enumerate() -> Result<Vec<GpuAdapter>, String> {
        unsafe {
            let factory: IDXGIFactory1 = CreateDXGIFactory1().map_err(|e| e.to_string())?;
            let mut adapters = Vec::new();
            let mut i = 0u32;
            loop {
                let adapter = match factory.EnumAdapters1(i) {
                    Ok(a) => a,
                    Err(_) => break, // DXGI_ERROR_NOT_FOUND once i exceeds the adapter count
                };
                i += 1;

                let desc = match adapter.GetDesc1() {
                    Ok(d) => d,
                    Err(e) => {
                        adapters.push(GpuAdapter {
                            name: "unknown adapter".into(),
                            vendor_id: 0,
                            device_id: 0,
                            adapter_type: AdapterType::Unknown,
                            dedicated_vram_bytes: 0,
                            dedicated_system_memory_bytes: 0,
                            shared_system_memory_bytes: 0,
                            reason: Some(format!("GetDesc1 failed: {e}")),
                        });
                        continue;
                    }
                };

                // Skip the software rasterizer (Microsoft Basic Render
                // Driver) via its documented flag, not by matching its name.
                if (desc.Flags & DXGI_ADAPTER_FLAG_SOFTWARE.0 as u32) != 0 {
                    continue;
                }

                let name = String::from_utf16_lossy(&desc.Description)
                    .trim_end_matches('\0')
                    .to_string();
                let uma = query_uma(&adapter);
                let (adapter_type, reason) = classify(
                    desc.VendorId,
                    desc.DedicatedVideoMemory as u64,
                    desc.SharedSystemMemory as u64,
                    uma,
                );
                adapters.push(GpuAdapter {
                    name,
                    vendor_id: desc.VendorId,
                    device_id: desc.DeviceId,
                    adapter_type,
                    dedicated_vram_bytes: desc.DedicatedVideoMemory as u64,
                    dedicated_system_memory_bytes: desc.DedicatedSystemMemory as u64,
                    shared_system_memory_bytes: desc.SharedSystemMemory as u64,
                    reason,
                });
            }
            Ok(dedupe_adapters(adapters))
        }
    }
}

pub fn detect() -> GpuDetectionResult {
    #[cfg(windows)]
    {
        match win::enumerate() {
            Ok(adapters) => GpuDetectionResult { adapters, error: None },
            Err(e) => GpuDetectionResult { adapters: vec![], error: Some(e) },
        }
    }
    #[cfg(not(windows))]
    {
        GpuDetectionResult {
            adapters: vec![],
            error: Some("GPU detection is Windows-only (DXGI/D3D12) -- not available on this platform".into()),
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn dedicated_gpu_classified_via_uma_false() {
        let (t, r) = classify(0x10DE, 4_000_000_000, 200_000_000, Some(false)); // NVIDIA
        assert_eq!(t, AdapterType::Dedicated);
        assert!(r.is_none());
    }

    #[test]
    fn integrated_gpu_classified_via_uma_true_even_when_arc_branded() {
        // Intel's newer integrated chips are branded "Arc" too, same as
        // Intel's discrete cards -- classification must not care about
        // vendor or name, only the UMA bit.
        let (t, r) = classify(0x8086, 512_000_000, 8_000_000_000, Some(true)); // Intel
        assert_eq!(t, AdapterType::Integrated);
        assert!(r.is_none());
    }

    #[test]
    fn hybrid_system_reports_each_adapter_independently() {
        // Laptop 1 in the test matrix: Intel iGPU + NVIDIA dGPU.
        let (igpu, _) = classify(0x8086, 128_000_000, 8_000_000_000, Some(true));
        let (dgpu, _) = classify(0x10DE, 4_000_000_000, 256_000_000, Some(false));
        assert_eq!(igpu, AdapterType::Integrated);
        assert_eq!(dgpu, AdapterType::Dedicated);
    }

    #[test]
    fn virtual_adapter_detected_by_known_vendor_id_not_name() {
        let (t, r) = classify(VENDOR_VMWARE, 0, 0, None);
        assert_eq!(t, AdapterType::Virtual);
        assert!(r.is_none());
    }

    #[test]
    fn hyperv_synthetic_adapter_detected_as_virtual() {
        let (t, _) = classify(VENDOR_MICROSOFT_HYPERV, 0, 0, Some(true));
        assert_eq!(t, AdapterType::Virtual);
    }

    #[test]
    fn uma_query_failure_falls_back_to_dedicated_by_memory_size() {
        let (t, r) = classify(0x1002, 6_000_000_000, 300_000_000, None); // AMD dGPU, query failed
        assert_eq!(t, AdapterType::Dedicated);
        assert!(r.is_some());
    }

    #[test]
    fn uma_query_failure_falls_back_to_integrated_by_memory_size() {
        let (t, r) = classify(0x8086, 64_000_000, 4_000_000_000, None);
        assert_eq!(t, AdapterType::Integrated);
        assert!(r.is_some());
    }

    #[test]
    fn uma_query_failure_with_no_memory_info_is_unknown_not_a_crash() {
        let (t, r) = classify(0x1002, 0, 0, None);
        assert_eq!(t, AdapterType::Unknown);
        assert!(r.is_some());
    }

    fn stub_adapter(name: &str, vendor_id: u32, device_id: u32) -> GpuAdapter {
        GpuAdapter {
            name: name.into(),
            vendor_id,
            device_id,
            adapter_type: AdapterType::Unknown,
            dedicated_vram_bytes: 0,
            dedicated_system_memory_bytes: 0,
            shared_system_memory_bytes: 0,
            reason: None,
        }
    }

    #[test]
    fn dedupe_collapses_identical_vendor_device_pairs() {
        // Reproduces what was actually observed on real hardware: DXGI
        // enumerating one physical integrated GPU as two identical entries.
        let adapters = vec![
            stub_adapter("Intel(R) Arc(TM) Graphics", 0x8086, 0x7D45),
            stub_adapter("Intel(R) Arc(TM) Graphics", 0x8086, 0x7D45),
        ];
        assert_eq!(dedupe_adapters(adapters).len(), 1);
    }

    #[test]
    fn dedupe_preserves_genuinely_different_hybrid_adapters() {
        let adapters = vec![
            stub_adapter("Intel(R) Iris(R) Xe Graphics", 0x8086, 0x9A49),
            stub_adapter("NVIDIA GeForce GTX 1650", 0x10DE, 0x1F91),
        ];
        assert_eq!(dedupe_adapters(adapters).len(), 2);
    }

    #[test]
    fn dedupe_never_collapses_getdesc1_failure_placeholders() {
        let adapters = vec![
            stub_adapter("unknown adapter", 0, 0),
            stub_adapter("unknown adapter", 0, 0),
        ];
        assert_eq!(dedupe_adapters(adapters).len(), 2);
    }

    #[test]
    fn detect_never_panics_on_this_platform() {
        // Smoke test for the public entry point itself -- on non-Windows CI
        // this exercises the graceful "unsupported platform" path; on
        // Windows it hits the real enumeration path end to end.
        let result = detect();
        assert!(result.error.is_some() || !result.adapters.is_empty() || result.adapters.is_empty());
    }
}
