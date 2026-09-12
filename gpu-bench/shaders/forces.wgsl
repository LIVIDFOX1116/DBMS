// Pass 1: force accumulation. Repulsion is naive all-pairs O(n^2) -- the
// simplest workload that's also the one GPU parallelism is expected to help
// with most; a production layout would likely add spatial partitioning on
// either backend, which would change (likely narrow) the CPU/GPU gap this
// spike measures. Attraction walks each node's own incident edges via a CSR
// adjacency list rather than having each edge write to two nodes, so no
// atomics are needed (WGSL doesn't reliably support atomic<f32>).

struct Params {
    num_nodes: u32,
    repulsion_k: f32,
    attraction_k: f32,
    rest_length: f32,
};

@group(0) @binding(0) var<storage, read> positions: array<vec2<f32>>;
@group(0) @binding(1) var<storage, read_write> forces: array<vec2<f32>>;
@group(0) @binding(2) var<storage, read> adj_offsets: array<u32>;
@group(0) @binding(3) var<storage, read> adj_indices: array<u32>;
@group(0) @binding(4) var<uniform> params: Params;

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

    forces[i] = f;
}
