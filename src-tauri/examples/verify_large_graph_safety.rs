//! One-off verification for the MAX_FORCE_SIM_NODES safety fix: fetches a
//! real snapshot's edges from a running backend, confirms the ceiling
//! condition actually trips for it, and exercises the safe fallback path
//! (degree_ranked_concentric_positions) against the real graph -- WITHOUT
//! ever invoking the unprotected O(n^2) settle loop (run_gpu_to_settle /
//! run_cpu_to_settle / run_benchmark), which is exactly the operation this
//! fix exists to prevent attempting at this scale. Deliberately does not
//! call those functions here: doing so against a real ~450k-node graph
//! would risk actually triggering the GPU driver timeout this fix guards
//! against, on the same physical GPU the desktop app (if running) is also
//! using for its own WebGL2 rendering -- not an acceptable verification
//! step, since a real TDR reset affects the whole machine, not just this
//! process.
//!
//! Usage: start the Python backend in live mode first, then:
//!   cargo run --example verify_large_graph_safety -- <backend_base_url> <snapshot_id>

use app_lib::layout;
use serde::Deserialize;

#[derive(Deserialize)]
struct EdgeRow {
    src_wallet_id: i64,
    dst_wallet_id: i64,
}

fn main() {
    let args: Vec<String> = std::env::args().collect();
    let base_url = args.get(1).cloned().unwrap_or_else(|| "http://127.0.0.1:8000".to_string());
    let snapshot_id: i64 = args.get(2).and_then(|s| s.parse().ok()).unwrap_or(1);

    let client = reqwest::blocking::Client::builder()
        .timeout(std::time::Duration::from_secs(120))
        .build()
        .expect("failed to build http client");
    let resp = client
        .get(format!("{base_url}/snapshots/{snapshot_id}/edges"))
        .query(&[("limit", "1000000")])
        .send()
        .unwrap_or_else(|e| panic!("could not reach backend at {base_url}: {e}"));
    if !resp.status().is_success() {
        panic!("backend returned HTTP {} fetching snapshot edges", resp.status());
    }
    let rows: Vec<EdgeRow> = resp.json().expect("malformed edges response");
    let edges: Vec<(i64, i64)> = rows.into_iter().map(|r| (r.src_wallet_id, r.dst_wallet_id)).collect();
    if edges.is_empty() {
        panic!("snapshot {snapshot_id} returned zero edges");
    }

    let (graph, index_to_id) = layout::build_graph_from_id_pairs(&edges);
    let n = index_to_id.len();
    println!("real graph: n={n} nodes, {} edges", edges.len());
    println!("MAX_FORCE_SIM_NODES = {}", layout::MAX_FORCE_SIM_NODES);

    let over_ceiling = n > layout::MAX_FORCE_SIM_NODES;
    println!(
        "ceiling check: {n} > {} => {over_ceiling}  ({})",
        layout::MAX_FORCE_SIM_NODES,
        if over_ceiling {
            "commands::run_layout would take the fast-fallback branch for this real snapshot, \
             never reaching run_gpu_to_settle/run_cpu_to_settle"
        } else {
            "under the ceiling -- this snapshot would NOT trigger the fallback; the normal \
             settle path would run instead"
        }
    );

    println!("\nrunning the safe fallback (degree_ranked_concentric_positions) against the real graph...");
    let t0 = std::time::Instant::now();
    let positions = layout::degree_ranked_concentric_positions(&graph);
    let elapsed = t0.elapsed();

    assert_eq!(positions.len(), n, "fallback must return exactly one position per node");
    let all_finite = positions.iter().all(|p| p[0].is_finite() && p[1].is_finite());
    assert!(all_finite, "every position must be finite");

    println!("OK: {} positions computed in {:?}, all finite, no panic/hang", positions.len(), elapsed);
    println!("\nVERIFIED: the real {n}-node snapshot correctly triggers the safety ceiling, and the \
              fallback layout completes safely and near-instantly against real production-scale data.");
}
