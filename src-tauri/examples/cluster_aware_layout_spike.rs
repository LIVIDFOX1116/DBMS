//! Evaluation spike for the cluster-visibility problem: a real Louvain
//! cluster's members can be scattered across the whole layout (maxDist
//! ~68,509 world units observed for cluster 32769, 3,408 members) because
//! force layout optimizes edge length and Louvain optimizes modularity --
//! they have no reason to agree spatially.
//!
//! Prototypes "cluster-aware forces" (option A from the evaluation): adds
//! one extra force term pulling each cluster member toward its cluster's
//! current centroid, recomputed every iteration. Deliberately NOT wired
//! into production layout.rs -- this duplicates force_for_node the same
//! way gpu-bench already does, precisely so evaluating the idea doesn't
//! require committing to it in the real engine first.
//!
//! Usage: cargo run --release --example cluster_aware_layout_spike --
//!   <backend_base_url> <snapshot_id> <cluster_id> <cluster_attraction_k> <out_prefix> [recompute_interval]
//! e.g.:
//!   cargo run --release --example cluster_aware_layout_spike -- \
//!     http://127.0.0.1:8801 1 32769 40.0 cluster32769 5

use app_lib::layout::{self, Graph, LayoutParams};
use rayon::prelude::*;
use serde::{Deserialize, Serialize};

#[derive(Deserialize)]
struct EdgeRow {
    src_wallet_id: i64,
    dst_wallet_id: i64,
}

#[derive(Deserialize)]
struct MemberRow {
    wallet_id: i64,
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

#[inline]
fn force_for_node(i: usize, positions: &[[f32; 2]], graph: &Graph, p: &LayoutParams) -> [f32; 2] {
    let pi = positions[i];
    let mut f = [0f32; 2];
    for (j, pj) in positions.iter().enumerate() {
        if j == i {
            continue;
        }
        let dx = pi[0] - pj[0];
        let dy = pi[1] - pj[1];
        let dist2 = (dx * dx + dy * dy).max(0.0001);
        let k = p.repulsion_k / (dist2 * dist2.sqrt());
        f[0] += dx * k;
        f[1] += dy * k;
    }
    let start = graph.adj_offsets[i] as usize;
    let end = graph.adj_offsets[i + 1] as usize;
    for &nb in &graph.adj_indices[start..end] {
        let pj = positions[nb as usize];
        let dx = pj[0] - pi[0];
        let dy = pj[1] - pi[1];
        let dist = (dx * dx + dy * dy).sqrt().max(0.0001);
        let scale = p.attraction_k * (dist - p.rest_length) / dist;
        f[0] += dx * scale;
        f[1] += dy * scale;
    }
    f
}

/// Runs `iterations` steps of naive all-pairs + edge-attraction, optionally
/// with an extra term pulling `cluster_members` toward their shared,
/// every-iteration-recomputed centroid. `cluster_attraction_k = 0.0`
/// reproduces the unmodified baseline exactly (same force_for_node, same
/// order) -- used to generate the "before" dump with this same code path
/// as the "after" one, so the comparison isn't confounded by any other
/// difference.
/// `recompute_interval` controls how often the cluster centroid is
/// refreshed from current positions (every iteration when 1). Answers the
/// question of whether a GPU implementation can get away with computing
/// centroids CPU-side and uploading them only every N iterations (avoiding
/// a per-iteration CPU<->GPU sync) without visibly hurting the settled
/// result -- see the recompute-interval sweep this spike was extended for.
fn run_layout(
    graph: &Graph,
    mut positions: Vec<[f32; 2]>,
    params: LayoutParams,
    iterations: usize,
    cluster_members: &[usize],
    cluster_attraction_k: f32,
    recompute_interval: usize,
) -> Vec<[f32; 2]> {
    let n = positions.len();
    let mut velocities = vec![[0f32; 2]; n];
    let mut centroid = [0f32; 2];
    for iter in 0..iterations {
        let mut forces: Vec<[f32; 2]> = (0..n).into_par_iter().map(|i| force_for_node(i, &positions, graph, &params)).collect();

        if cluster_attraction_k > 0.0 && !cluster_members.is_empty() {
            if iter % recompute_interval == 0 {
                // Summed in `cluster_members`'s fixed slice order (not a
                // HashSet's randomly-seeded-per-process iteration order) so
                // floating-point rounding is identical run to run -- this
                // sim is a chaotic dynamical system over 300 iterations, and
                // even tiny summation-order rounding differences compound
                // into materially different settled layouts, which made an
                // earlier version of this sweep non-reproducible.
                let mut cx = 0f32;
                let mut cy = 0f32;
                for &i in cluster_members {
                    cx += positions[i][0];
                    cy += positions[i][1];
                }
                centroid = [cx / cluster_members.len() as f32, cy / cluster_members.len() as f32];
            }
            for &i in cluster_members {
                forces[i][0] += (centroid[0] - positions[i][0]) * cluster_attraction_k;
                forces[i][1] += (centroid[1] - positions[i][1]) * cluster_attraction_k;
            }
        }

        for i in 0..n {
            velocities[i][0] = (velocities[i][0] + forces[i][0] * params.dt) * params.damping;
            velocities[i][1] = (velocities[i][1] + forces[i][1] * params.dt) * params.damping;
            positions[i][0] += velocities[i][0] * params.dt;
            positions[i][1] += velocities[i][1] * params.dt;
        }
    }
    positions
}

fn main() {
    let args: Vec<String> = std::env::args().collect();
    let base_url = args.get(1).cloned().unwrap_or_else(|| "http://127.0.0.1:8000".to_string());
    let snapshot_id: i64 = args.get(2).and_then(|s| s.parse().ok()).unwrap_or(1);
    let cluster_id: i64 = args.get(3).and_then(|s| s.parse().ok()).unwrap_or(32769);
    let cluster_attraction_k: f32 = args.get(4).and_then(|s| s.parse().ok()).unwrap_or(40.0);
    let out_prefix = args.get(5).cloned().unwrap_or_else(|| "cluster_spike".to_string());
    let recompute_interval: usize = args.get(6).and_then(|s| s.parse().ok()).unwrap_or(1);

    let client = reqwest::blocking::Client::builder().timeout(std::time::Duration::from_secs(60)).build().unwrap();

    let edges: Vec<(i64, i64)> = client
        .get(format!("{base_url}/snapshots/{snapshot_id}/edges"))
        .query(&[("limit", "1000000")])
        .send()
        .expect("edges request failed")
        .json::<Vec<EdgeRow>>()
        .expect("malformed edges response")
        .into_iter()
        .map(|r| (r.src_wallet_id, r.dst_wallet_id))
        .collect();
    println!("fetched {} real edges", edges.len());

    let members: Vec<i64> = client
        .get(format!("{base_url}/clusters/{cluster_id}/members"))
        .send()
        .expect("members request failed")
        .json::<Vec<MemberRow>>()
        .expect("malformed members response")
        .into_iter()
        .map(|m| m.wallet_id)
        .collect();
    println!("cluster {cluster_id}: {} real members", members.len());

    let (graph, index_to_id) = layout::build_graph_from_id_pairs(&edges);
    let id_to_index: std::collections::HashMap<i64, usize> =
        index_to_id.iter().enumerate().map(|(i, &id)| (id, i)).collect();
    // A Vec, not a HashSet: iteration order feeds the per-iteration centroid
    // summation inside run_layout, and a HashSet's randomly-seeded-per-
    // process order made otherwise-identical runs non-reproducible (see
    // run_layout's comment). Sorted + deduped so repeated ids in `members`
    // (shouldn't happen, but cheap to guard) can't double-count.
    let mut cluster_indices: Vec<usize> = members.iter().filter_map(|id| id_to_index.get(id).copied()).collect();
    cluster_indices.sort_unstable();
    cluster_indices.dedup();
    println!("cluster members found among graph nodes: {}", cluster_indices.len());

    let initial = layout::initial_circle_positions(index_to_id.len());
    let params = LayoutParams::default();
    const ITERATIONS: usize = 300;

    let t0 = std::time::Instant::now();
    let baseline = run_layout(&graph, initial.clone(), params, ITERATIONS, &cluster_indices, 0.0, 1);
    println!("baseline settled in {:.2?}", t0.elapsed());

    let t0 = std::time::Instant::now();
    let cluster_aware = run_layout(&graph, initial, params, ITERATIONS, &cluster_indices, cluster_attraction_k, recompute_interval);
    println!("cluster-aware (k={cluster_attraction_k}, recompute_interval={recompute_interval}) settled in {:.2?}", t0.elapsed());

    // Report the cluster's own spread in both, the concrete number the
    // fix is meant to shrink.
    fn cluster_spread(positions: &[[f32; 2]], members: &[usize]) -> f32 {
        if members.is_empty() {
            return 0.0;
        }
        let cx: f32 = members.iter().map(|&i| positions[i][0]).sum::<f32>() / members.len() as f32;
        let cy: f32 = members.iter().map(|&i| positions[i][1]).sum::<f32>() / members.len() as f32;
        members.iter().map(|&i| ((positions[i][0] - cx).powi(2) + (positions[i][1] - cy).powi(2)).sqrt()).fold(0f32, f32::max)
    }
    println!("baseline cluster maxDist from centroid: {:.1}", cluster_spread(&baseline, &cluster_indices));
    println!("cluster-aware maxDist from centroid: {:.1}", cluster_spread(&cluster_aware, &cluster_indices));

    for (label, positions) in [("before", &baseline), ("after", &cluster_aware)] {
        let nodes: Vec<NodeOut> = index_to_id.iter().zip(positions.iter()).map(|(&id, &[x, y])| NodeOut { id, x, y }).collect();
        let edges_out: Vec<[i64; 2]> = edges.iter().map(|&(a, b)| [a, b]).collect();
        let dump = DumpOut { nodes, edges: edges_out };
        let path = format!("{out_prefix}_{label}.json");
        std::fs::write(&path, serde_json::to_string(&dump).unwrap()).unwrap();
        println!("wrote {path}");
    }
}
