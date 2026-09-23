// See src/layout.rs module docs. Repulsion is naive all-pairs O(n^2) on
// every backend (CPU and GPU) -- validated by gpu-bench: GPU wins 5x-149x
// over CPU at >=10k nodes even on integrated graphics, at this exact
// workload. Attraction walks each node's own incident edges via a CSR
// adjacency list (no atomics needed -- WGSL doesn't reliably support
// atomic<f32>, and this way no invocation ever writes another node's data).

struct Params {
    num_nodes: u32,
    repulsion_k: f32,
    attraction_k: f32,
    rest_length: f32,
};

// Cluster-aware attraction (see layout.rs's ClusterForces doc comment for
// the full rationale/measurements). `cluster_of[i]` is a dense group id;
// `centroids[g]` is that group's centroid from the CURRENT positions buffer
// -- recomputed and re-uploaded by the host every iteration (see
// GpuLayout::upload_centroids / run_gpu_to_settle), never staled, to match
// the CPU path exactly and hold CPU/GPU parity. Always bound (even when
// cluster_attraction_k is 0) to keep one pipeline/bind-group-layout for both
// the cluster-aware and cluster-unaware cases -- the branch below makes the
// unaware case a true no-op, not just a small extra cost.
struct ClusterParams {
    cluster_attraction_k: f32,
};

@group(0) @binding(0) var<storage, read> positions: array<vec2<f32>>;
@group(0) @binding(1) var<storage, read_write> forces: array<vec2<f32>>;
@group(0) @binding(2) var<storage, read> adj_offsets: array<u32>;
@group(0) @binding(3) var<storage, read> adj_indices: array<u32>;
@group(0) @binding(4) var<uniform> params: Params;
@group(0) @binding(5) var<storage, read> cluster_of: array<u32>;
@group(0) @binding(6) var<storage, read> centroids: array<vec2<f32>>;
@group(0) @binding(7) var<uniform> cluster_params: ClusterParams;

@compute @workgroup_size(256)
fn main(@builtin(global_invocation_id) gid: vec3<u32>) {
    let i = gid.x;
    if (i >= params.num_nodes) { return; }
    let pi = positions[i];
    var f = vec2<f32>(0.0, 0.0);

    for (var j: u32 = 0u; j < params.num_nodes; j = j + 1u) {
        if (j == i) { continue; }
        let d = pi - positions[j];
        let dist2 = max(dot(d, d), 0.0001);
        f = f + d * (params.repulsion_k / (dist2 * sqrt(dist2)));
    }

    let start = adj_offsets[i];
    let end = adj_offsets[i + 1u];
    for (var k = start; k < end; k = k + 1u) {
        let j = adj_indices[k];
        let d = positions[j] - pi;
        let dist = max(length(d), 0.0001);
        let disp = dist - params.rest_length;
        f = f + (d / dist) * (params.attraction_k * disp);
    }

    if (cluster_params.cluster_attraction_k > 0.0) {
        let c = centroids[cluster_of[i]];
        f = f + (c - pi) * cluster_params.cluster_attraction_k;
    }

    forces[i] = f;
}
