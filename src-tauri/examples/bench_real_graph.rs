//! Real-graph counterpart to gpu-bench: runs the actual production
//! CpuLayout/GpuLayout engine (app_lib::layout) against a real snapshot's
//! edges fetched from a running backend, instead of gpu-bench's synthetic
//! random graph. Reuses layout::run_benchmark so this is the same code path
//! `benchmark_layout` (the Tauri command) calls -- this just lets it be run
//! standalone, e.g. on a different machine, without going through the full
//! desktop app UI.
//!
//! Usage: start the Python backend in live mode first (TXGRAPH_API_MODE=live),
//! then:
//!   cargo run --release --example bench_real_graph -- <backend_base_url> <snapshot_id> [max_edges]
//! e.g.:
//!   cargo run --release --example bench_real_graph -- http://127.0.0.1:8000 1 1000000

use app_lib::layout;
use serde::Deserialize;

#[derive(Deserialize)]
struct EdgeRow {
    src_wallet_id: i64,
    dst_wallet_id: i64,
}

fn fmt_ms(ms: f64) -> String {
    if ms >= 1000.0 {
        format!("{:.2}s", ms / 1000.0)
    } else {
        format!("{ms:.2}ms")
    }
}

fn main() {
    let args: Vec<String> = std::env::args().collect();
    let base_url = args.get(1).cloned().unwrap_or_else(|| "http://127.0.0.1:8000".to_string());
    let snapshot_id: i64 = args.get(2).and_then(|s| s.parse().ok()).unwrap_or(1);
    let max_edges: usize = args.get(3).and_then(|s| s.parse().ok()).unwrap_or(1_000_000);

    println!("bench_real_graph -- fetching real snapshot edges and running the production layout engine\n");
    println!("backend: {base_url}, snapshot_id: {snapshot_id}, max_edges: {max_edges}\n");

    let client = reqwest::blocking::Client::builder()
        .timeout(std::time::Duration::from_secs(60))
        .build()
        .expect("failed to build http client");
    let resp = client
        .get(format!("{base_url}/snapshots/{snapshot_id}/edges"))
        .query(&[("limit", max_edges.to_string())])
        .send()
        .unwrap_or_else(|e| panic!("could not reach backend at {base_url}: {e}\n(is the Python backend running in live mode? TXGRAPH_API_MODE=live)"));
    if !resp.status().is_success() {
        panic!("backend returned HTTP {} fetching snapshot edges", resp.status());
    }
    let rows: Vec<EdgeRow> = resp.json().expect("malformed edges response");
    let edges: Vec<(i64, i64)> = rows.into_iter().map(|r| (r.src_wallet_id, r.dst_wallet_id)).collect();
    if edges.is_empty() {
        panic!("snapshot {snapshot_id} returned zero edges -- wrong snapshot_id, or backend still in mock mode?");
    }

    let (graph, index_to_id) = layout::build_graph_from_id_pairs(&edges);
    let initial = layout::initial_circle_positions(index_to_id.len());
    println!("real graph: n={} nodes, {} edges\n", index_to_id.len(), edges.len());

    let gpu_device = layout::create_gpu_device(None);
    match &gpu_device {
        Some((_, _, name)) => println!("GPU adapter found: {name}"),
        None => println!("No GPU adapter found -- GPU row will be skipped"),
    }
    println!();

    let params = layout::LayoutParams::default();
    let cap = layout::SettleCap::default();
    let rows = layout::run_benchmark(
        &graph,
        &initial,
        params,
        gpu_device.as_ref().map(|(d, q, n)| (d, q, n.as_str())),
        3,
        cap,
    );

    for r in &rows {
        let settle_note = if r.converged {
            format!("converged in {} iters ({})", r.settle_iterations, fmt_ms(r.settle_ms))
        } else {
            format!("DID NOT converge within cap ({} iters / {})", r.settle_iterations, fmt_ms(r.settle_ms))
        };
        let readback_note = match r.readback_ms {
            Some(ms) => format!(", VRAM->CPU readback: {}", fmt_ms(ms)),
            None => String::new(),
        };
        println!("  [{:>18}] per-iter: {:>10}  settle: {settle_note}{readback_note}", r.label, fmt_ms(r.per_iter_ms));
    }

    // Partial IPC-cost signal: JSON-serializing a positions map the same
    // shape run_layout returns to the webview. This measures ONLY
    // serialization on the Rust side -- it does NOT include the actual
    // cross-process IPC transport or the webview's JSON.parse + JS loop to
    // apply positions, neither of which is measurable without the real
    // webview. Treat this as a lower bound, not the full IPC cost.
    let positions_map: std::collections::HashMap<i64, [f32; 2]> =
        index_to_id.iter().zip(initial.iter()).map(|(&id, &p)| (id, p)).collect();
    let t0 = std::time::Instant::now();
    let json = serde_json::to_string(&positions_map).expect("positions map must serialize");
    let serialize_ms = t0.elapsed().as_secs_f64() * 1000.0;
    println!(
        "\n  [serde_json::to_string only] {} entries, {} bytes, {}  (excludes IPC transport + webview JSON.parse)",
        positions_map.len(),
        json.len(),
        fmt_ms(serialize_ms)
    );
}
