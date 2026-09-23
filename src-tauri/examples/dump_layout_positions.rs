//! Fetches a snapshot's real edges, runs the production GPU layout engine
//! to the same settle cap run_layout uses, and writes {nodes, edges} JSON
//! (wallet_id-keyed) for the rendering spikes to consume -- so both
//! approach-A and approach-B spikes render identical, real, GPU-settled
//! positions rather than each inventing their own placeholder layout.
//!
//! Usage: cargo run --release --example dump_layout_positions -- <backend_base_url> <snapshot_id> <out_path>

use app_lib::layout;
use serde::{Deserialize, Serialize};

#[derive(Deserialize)]
struct EdgeRow {
    src_wallet_id: i64,
    dst_wallet_id: i64,
}

#[derive(Serialize)]
struct NodeOut {
    id: i64,
    x: f32,
    y: f32,
}

#[derive(Serialize)]
struct DumpOut {
    nodes: Vec<NodeOut>,
    edges: Vec<[i64; 2]>,
}

fn main() {
    let args: Vec<String> = std::env::args().collect();
    let base_url = args.get(1).cloned().unwrap_or_else(|| "http://127.0.0.1:8000".to_string());
    let snapshot_id: i64 = args.get(2).and_then(|s| s.parse().ok()).unwrap_or(1);
    let out_path = args.get(3).cloned().unwrap_or_else(|| "positions.json".to_string());

    let client = reqwest::blocking::Client::builder().timeout(std::time::Duration::from_secs(60)).build().unwrap();
    let resp = client
        .get(format!("{base_url}/snapshots/{snapshot_id}/edges"))
        .query(&[("limit", "1000000")])
        .send()
        .expect("could not reach backend");
    let rows: Vec<EdgeRow> = resp.json().expect("malformed edges response");
    let edges: Vec<(i64, i64)> = rows.iter().map(|r| (r.src_wallet_id, r.dst_wallet_id)).collect();
    println!("fetched {} real edges", edges.len());

    let (graph, index_to_id) = layout::build_graph_from_id_pairs(&edges);
    let initial = layout::initial_circle_positions(index_to_id.len());
    println!("n={} nodes", index_to_id.len());

    let params = layout::LayoutParams::default();
    let cap = layout::SettleCap { max_iterations: 500, max_wall_clock: std::time::Duration::from_secs(30) };

    let (device, queue, name) = layout::create_gpu_device(None).expect("no GPU adapter found");
    println!("running layout on: {name}");
    let no_cluster = layout::ClusterForces::none(index_to_id.len());
    let outcome = layout::run_gpu_to_settle(&device, &queue, &graph, &initial, params, cap, &no_cluster);
    println!("settled: {} iterations, converged={}, elapsed={:.2?}", outcome.iterations, outcome.converged, outcome.elapsed);

    let nodes: Vec<NodeOut> = index_to_id.iter().zip(outcome.positions.iter()).map(|(&id, &[x, y])| NodeOut { id, x, y }).collect();
    let edges_out: Vec<[i64; 2]> = edges.iter().map(|&(a, b)| [a, b]).collect();
    let dump = DumpOut { nodes, edges: edges_out };
    std::fs::write(&out_path, serde_json::to_string(&dump).unwrap()).expect("failed to write output file");
    println!("wrote {out_path}");
}
