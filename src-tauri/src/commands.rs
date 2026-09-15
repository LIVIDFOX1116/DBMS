//! Tauri commands invoked from the webview via window.__TAURI__.core.invoke
//! (withGlobalTauri, no @tauri-apps/api npm package -- this repo has no
//! Node/npm anywhere, deliberately, see README).

use crate::backend::BackendHandle;
use crate::gpu::{self, GpuDetectionResult};
use crate::keychain;
use crate::layout::{self, BenchRow, CpuMode, LayoutParams, SettleCap};
use serde::{Deserialize, Serialize};
use std::collections::HashMap;
use std::sync::Mutex;

pub struct AppState(pub Mutex<BackendHandle>);

#[tauri::command]
pub fn get_api_base(state: tauri::State<AppState>) -> String {
    let handle = state.0.lock().unwrap();
    format!("http://127.0.0.1:{}", handle.port)
}

/// Recent backend stdout/stderr lines (oldest first), for a future "view
/// logs" panel -- a runtime backend error is otherwise invisible once the
/// app is packaged (no console in a release build).
#[tauri::command]
pub fn get_backend_logs(state: tauri::State<AppState>) -> Vec<String> {
    state.0.lock().unwrap().recent_log_lines()
}

/// Native GPU adapter enumeration (see gpu.rs for the full rationale) --
/// dedicated/integrated/virtual/unknown per adapter, with VRAM figures.
/// Not passed to the Python backend: nothing there consumes it today, and
/// piping it through would just be unused plumbing until something does
/// (a graph-rendering path choice, a system-info panel, etc.).
#[tauri::command]
pub fn detect_gpus() -> GpuDetectionResult {
    gpu::detect()
}

/// Deliberately a bool, not the key -- Settings never reads the stored value
/// back out to render it, per the "write-only" requirement. The UI shows
/// "•••• saved" from this alone.
#[tauri::command]
pub fn has_api_key() -> bool {
    keychain::get_key().map(|k| !k.trim().is_empty()).unwrap_or(false)
}

fn hot_swap(state: &tauri::State<AppState>, new_key: &str) -> Result<(), String> {
    let (port, token) = {
        let handle = state.0.lock().unwrap();
        (handle.port, handle.internal_token.clone())
    };
    let client = reqwest::blocking::Client::new();
    let resp = client
        .post(format!("http://127.0.0.1:{port}/internal/api-key"))
        .header("X-Internal-Token", token)
        .json(&serde_json::json!({ "api_key": new_key }))
        .send()
        .map_err(|e| format!("could not reach backend to apply the new key: {e}"))?;
    if !resp.status().is_success() {
        return Err(format!("backend rejected the key update: HTTP {}", resp.status()));
    }
    Ok(())
}

#[tauri::command]
pub fn set_api_key(new_key: String, state: tauri::State<AppState>) -> Result<(), String> {
    let new_key = new_key.trim().to_string();
    if new_key.is_empty() {
        return Err("key cannot be empty -- use clear_api_key to remove it".into());
    }
    keychain::set_key(&new_key)?;
    hot_swap(&state, &new_key)
}

#[tauri::command]
pub fn clear_api_key(state: tauri::State<AppState>) -> Result<(), String> {
    keychain::clear_key()?;
    hot_swap(&state, "")
}

#[derive(Serialize)]
pub struct TestResult {
    pub ok: bool,
    pub message: String,
}

/// Rust-owned, one cheap real call, no backend involvement -- works even
/// during first-run onboarding while the FastAPI child may still be
/// booting. Pass `candidate_key` to test an unsaved value while the user is
/// typing; omit it to test whatever's currently in the keychain.
#[tauri::command]
pub fn test_api_key(candidate_key: Option<String>) -> Result<TestResult, String> {
    let key = match candidate_key.map(|k| k.trim().to_string()).filter(|k| !k.is_empty()) {
        Some(k) => k,
        None => keychain::get_key()
            .filter(|k| !k.trim().is_empty())
            .ok_or_else(|| "no key saved yet, and none provided to test".to_string())?,
    };

    let client = reqwest::blocking::Client::builder()
        .timeout(std::time::Duration::from_secs(8))
        .build()
        .map_err(|e| e.to_string())?;

    let resp = match client
        .get("https://api.etherscan.io/v2/api")
        .query(&[("chainid", "1"), ("module", "stats"), ("action", "ethsupply"), ("apikey", &key)])
        .send()
    {
        Ok(r) => r,
        Err(e) if e.is_timeout() => {
            return Ok(TestResult { ok: false, message: "No network — the request timed out.".into() })
        }
        Err(e) if e.is_connect() => {
            return Ok(TestResult { ok: false, message: "No network — could not reach Etherscan.".into() })
        }
        Err(e) => return Ok(TestResult { ok: false, message: format!("Request failed: {e}") }),
    };

    let status = resp.status();
    let body: serde_json::Value = resp.json().unwrap_or_else(|_| serde_json::json!({}));
    let es_status = body.get("status").and_then(|v| v.as_str()).unwrap_or("");
    let es_message = body.get("message").and_then(|v| v.as_str()).unwrap_or("").to_string();

    if status.as_u16() == 429 || es_message.to_lowercase().contains("rate limit") {
        return Ok(TestResult { ok: false, message: "Rate limited by Etherscan — try again in a moment.".into() });
    }
    if es_status == "1" {
        return Ok(TestResult { ok: true, message: "Connected — key is valid.".into() });
    }
    if es_message.to_lowercase().contains("invalid api key") {
        return Ok(TestResult { ok: false, message: "Invalid API key.".into() });
    }
    Ok(TestResult { ok: false, message: format!("Unexpected response from Etherscan: {es_message}") })
}

#[derive(Deserialize)]
struct EdgeRow {
    src_wallet_id: i64,
    dst_wallet_id: i64,
}

#[derive(Deserialize)]
struct ClusterIdRow {
    cluster_id: i64,
}

/// wallet_id -> cluster_id for every wallet clustered under `run_id`, used
/// to build a `layout::ClusterForces` (see its doc comment). Two-step,
/// mirroring the frontend's own pattern for the same data (dashboard.html's
/// cluster modal): list every cluster for the run, then batch-fetch
/// membership. Batched at 500 ids/request rather than one huge query string
/// -- a real run can have close to as many clusters as nodes (99.3% of real
/// clusters are singletons), and a single comma-joined list of that many
/// ids risks exceeding typical HTTP request-line/header size limits.
fn fetch_cluster_membership(port: u16, run_id: i64) -> Result<HashMap<i64, i64>, String> {
    let client = reqwest::blocking::Client::builder()
        .timeout(std::time::Duration::from_secs(30))
        .build()
        .map_err(|e| e.to_string())?;

    let cluster_ids: Vec<i64> = client
        .get(format!("http://127.0.0.1:{port}/clusters"))
        .query(&[("run_id", run_id.to_string())])
        .send()
        .map_err(|e| format!("could not reach backend for clusters: {e}"))?
        .json::<Vec<ClusterIdRow>>()
        .map_err(|e| format!("malformed clusters response: {e}"))?
        .into_iter()
        .map(|c| c.cluster_id)
        .collect();

    let mut wallet_to_cluster = HashMap::new();
    for chunk in cluster_ids.chunks(500) {
        let ids_param = chunk.iter().map(|id| id.to_string()).collect::<Vec<_>>().join(",");
        let membership: HashMap<i64, Vec<i64>> = client
            .get(format!("http://127.0.0.1:{port}/cluster-membership"))
            .query(&[("cluster_ids", ids_param)])
            .send()
            .map_err(|e| format!("could not reach backend for cluster membership: {e}"))?
            .json()
            .map_err(|e| format!("malformed cluster-membership response: {e}"))?;
        for (wallet_id, clusters) in membership {
            // Louvain (graph/run_analysis.py) assigns each wallet to exactly
            // one community -- the array is defensive schema generality
            // (see api/main.py's /cluster-membership), not a real overlap
            // case. The first (only, in practice) entry is what matters.
            if let Some(&cluster_id) = clusters.first() {
                wallet_to_cluster.insert(wallet_id, cluster_id);
            }
        }
    }
    Ok(wallet_to_cluster)
}

/// Remaps raw DB cluster_ids (sparse, arbitrary) to the dense 0..num_clusters
/// group ids `ClusterForces` needs to size its per-iteration centroid/count
/// buffers. A wallet with no cluster assignment (fetch failed, or genuinely
/// unclustered) gets its own singleton group -- the same no-op behavior a
/// real singleton cluster already has (see `ClusterForces::none`).
fn build_cluster_forces(index_to_id: &[i64], wallet_to_cluster: &HashMap<i64, i64>, attraction_k: f32) -> layout::ClusterForces {
    let mut group_of_cluster_id: HashMap<i64, u32> = HashMap::new();
    let mut cluster_of = Vec::with_capacity(index_to_id.len());
    let mut next_group = 0u32;
    for &wallet_id in index_to_id {
        let group = match wallet_to_cluster.get(&wallet_id) {
            Some(&cluster_id) => *group_of_cluster_id.entry(cluster_id).or_insert_with(|| {
                let g = next_group;
                next_group += 1;
                g
            }),
            None => {
                let g = next_group;
                next_group += 1;
                g
            }
        };
        cluster_of.push(group);
    }
    layout::ClusterForces { cluster_of, num_clusters: next_group, attraction_k }
}

/// Shared by `run_layout` and `benchmark_layout` -- both need the same real
/// edge set from the same already-running backend the rest of the app talks
/// to (see `get_api_base`), not a separate DB connection from Rust.
fn fetch_snapshot_edges(port: u16, snapshot_id: i64, max_edges: usize) -> Result<Vec<(i64, i64)>, String> {
    let client = reqwest::blocking::Client::builder()
        .timeout(std::time::Duration::from_secs(30))
        .build()
        .map_err(|e| e.to_string())?;
    let resp = client
        .get(format!("http://127.0.0.1:{port}/snapshots/{snapshot_id}/edges"))
        .query(&[("limit", max_edges.to_string())])
        .send()
        .map_err(|e| format!("could not reach backend for snapshot edges: {e}"))?;
    if !resp.status().is_success() {
        return Err(format!("backend returned HTTP {} fetching snapshot edges", resp.status()));
    }
    let rows: Vec<EdgeRow> = resp.json().map_err(|e| format!("malformed edges response: {e}"))?;
    Ok(rows.into_iter().map(|r| (r.src_wallet_id, r.dst_wallet_id)).collect())
}

#[derive(Serialize)]
pub struct LayoutResult {
    /// Keyed by wallet_id (as it appears everywhere else in the app), not
    /// the dense index used internally -- the frontend never needs to know
    /// that remapping happened.
    pub positions: HashMap<i64, [f32; 2]>,
    pub device_requested: String,
    pub device_used: String,
    pub node_count: usize,
    pub edge_count: usize,
    pub iterations: usize,
    pub elapsed_ms: u64,
    pub converged: bool,
}

/// Runs the real production layout engine over a snapshot's edges and
/// returns settled positions keyed by wallet_id. `device` is "gpu" or "cpu";
/// requesting "gpu" on a machine/build with no adapter falls back to CPU
/// automatically -- `device_used` tells the caller which one actually ran,
/// so a UI can be honest about it instead of silently lying about what
/// rendered the graph. `cluster_attraction_k` <= 0 disables cluster-aware
/// forces entirely (skips the membership fetch); `run_id` identifies which
/// clustering to pull membership from (see `fetch_cluster_membership`) and
/// is only used when `cluster_attraction_k` > 0.
///
/// GPU currently ignores `cluster_attraction_k` and always lays out
/// cluster-unaware -- wiring cluster-aware forces into the GPU path (with
/// CPU/GPU parity preserved) is the next step, not yet done.
#[tauri::command]
pub fn run_layout(
    snapshot_id: i64,
    max_edges: usize,
    device: String,
    run_id: i64,
    cluster_attraction_k: f32,
    state: tauri::State<AppState>,
) -> Result<LayoutResult, String> {
    let port = state.0.lock().unwrap().port;
    let edges = fetch_snapshot_edges(port, snapshot_id, max_edges)?;
    if edges.is_empty() {
        return Err(format!("snapshot {snapshot_id} has no edges to lay out"));
    }
    let (graph, index_to_id) = layout::build_graph_from_id_pairs(&edges);
    let initial = layout::initial_circle_positions(index_to_id.len());
    let params = LayoutParams::default();
    let cap = SettleCap::default();

    let cluster = if cluster_attraction_k > 0.0 {
        match fetch_cluster_membership(port, run_id) {
            Ok(membership) => build_cluster_forces(&index_to_id, &membership, cluster_attraction_k),
            Err(e) => {
                // Degrade to the cluster-unaware layout rather than failing
                // the whole render -- the base layout is still useful
                // without the cluster fix, and this fetch is a nice-to-have
                // on top of it, not a hard dependency of laying out at all.
                log::warn!("cluster membership fetch failed for run {run_id}, laying out without cluster-aware forces: {e}");
                layout::ClusterForces::none(index_to_id.len())
            }
        }
    } else {
        layout::ClusterForces::none(index_to_id.len())
    };

    // "gpu" = any adapter, "gpu-discrete"/"gpu-integrated" = a specific one
    // (for the Settings device picker on a hybrid machine with both), "cpu"
    // (or anything else) = CPU rayon. Auto-mode threshold resolution happens
    // in JS (dashboard.html's resolveLayoutDevice) since it needs the node
    // count being laid out, which this command doesn't otherwise need.
    let prefer_discrete = match device.as_str() {
        "gpu-discrete" => Some(true),
        "gpu-integrated" => Some(false),
        _ => None,
    };
    let (dense_positions, device_used, iterations, elapsed_ms, converged) = if device.starts_with("gpu") {
        match layout::create_gpu_device(prefer_discrete) {
            Some((gpu_device, queue, name)) => {
                let outcome = layout::run_gpu_to_settle(&gpu_device, &queue, &graph, &initial, params, cap);
                (outcome.positions, format!("gpu: {name}"), outcome.iterations, outcome.elapsed.as_millis() as u64, outcome.converged)
            }
            None => {
                let outcome = layout::run_cpu_to_settle(&graph, initial, params, CpuMode::Multi, cap, cluster.clone());
                (outcome.positions, "cpu (no matching GPU adapter found, fell back)".to_string(), outcome.iterations, outcome.elapsed.as_millis() as u64, outcome.converged)
            }
        }
    } else {
        let outcome = layout::run_cpu_to_settle(&graph, initial, params, CpuMode::Multi, cap, cluster.clone());
        (outcome.positions, "cpu".to_string(), outcome.iterations, outcome.elapsed.as_millis() as u64, outcome.converged)
    };

    let positions = index_to_id
        .into_iter()
        .zip(dense_positions)
        .collect::<HashMap<i64, [f32; 2]>>();

    Ok(LayoutResult {
        node_count: positions.len(),
        edge_count: edges.len(),
        positions,
        device_requested: device,
        device_used,
        iterations,
        elapsed_ms,
        converged,
    })
}

#[derive(Serialize)]
pub struct BenchmarkResult {
    pub node_count: usize,
    pub edge_count: usize,
    pub rows: Vec<BenchRow>,
}

/// The comparison the GPU-vs-CPU decision actually needs: CPU single-thread,
/// CPU rayon, and GPU (if any adapter is found) run back-to-back on the
/// SAME real snapshot graph, so the numbers are directly comparable to each
/// other and reproducible on different hardware (e.g. the GTX 1650 machine)
/// by calling this same command there.
#[tauri::command]
pub fn benchmark_layout(snapshot_id: i64, max_edges: usize, state: tauri::State<AppState>) -> Result<BenchmarkResult, String> {
    let port = state.0.lock().unwrap().port;
    let edges = fetch_snapshot_edges(port, snapshot_id, max_edges)?;
    if edges.is_empty() {
        return Err(format!("snapshot {snapshot_id} has no edges to benchmark"));
    }
    let (graph, index_to_id) = layout::build_graph_from_id_pairs(&edges);
    let initial = layout::initial_circle_positions(index_to_id.len());
    let params = LayoutParams::default();
    let cap = SettleCap::default();

    let gpu_device = layout::create_gpu_device(None);
    let rows = layout::run_benchmark(
        &graph,
        &initial,
        params,
        gpu_device.as_ref().map(|(d, q, n)| (d, q, n.as_str())),
        3,
        cap,
    );

    Ok(BenchmarkResult { node_count: index_to_id.len(), edge_count: edges.len(), rows })
}
