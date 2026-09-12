// Pass 2: velocity/position integration. Reads the forces pass 1 wrote,
// never touches other nodes' data -- no synchronization needed beyond the
// pass boundary wgpu already inserts between dispatches sharing buffers.

struct IntParams {
    num_nodes: u32,
    dt: f32,
    damping: f32,
};

@group(0) @binding(0) var<storage, read_write> positions: array<vec2<f32>>;
@group(0) @binding(1) var<storage, read_write> velocities: array<vec2<f32>>;
@group(0) @binding(2) var<storage, read> forces: array<vec2<f32>>;
@group(0) @binding(3) var<uniform> params: IntParams;

@compute @workgroup_size(256)
fn main(@builtin(global_invocation_id) gid: vec3<u32>) {
    let i = gid.x;
    if (i >= params.num_nodes) { return; }
    let v = (velocities[i] + forces[i] * params.dt) * params.damping;
    velocities[i] = v;
    positions[i] = positions[i] + v * params.dt;
}
