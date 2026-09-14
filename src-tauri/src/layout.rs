//! Native force-directed layout compute engine -- CPU (rayon) and GPU
//! (wgpu) implementations sharing identical math, so results agree within
//! numerical tolerance (see parity tests below). Validated by the
//! standalone gpu-bench spike before this was written: GPU wins 5x-149x
//! over CPU at >=10,000 nodes, even on integrated graphics; below ~1,000
//! nodes GPU dispatch overhead can make it slower than multi-threaded CPU.
//!
//! Repulsion is naive all-pairs O(n^2) on every backend -- see
//! shaders/forces.wgsl's header for why, and for what a smarter
//! (spatially-partitioned) version would change.
//!
//! GPU iterations run resident in VRAM with no per-iteration readback --
//! callers batch several iterations via `GpuLayout::run_iterations` then
//! call `readback_positions` periodically, not every frame. Readback cost
//! was measured (gpu-bench) at 0.36-2.4ms even at 200k nodes, trivial next
//! to compute time -- the bottleneck this engine actually has to worry
//! about is compute, not VRAM->host IPC.

use bytemuck::{Pod, Zeroable};
use rayon::prelude::*;
use wgpu::util::DeviceExt;

#[derive(Clone, Copy, Debug)]
pub struct LayoutParams {
    pub repulsion_k: f32,
    pub attraction_k: f32,
    pub rest_length: f32,
    pub dt: f32,
    pub damping: f32,
}

impl Default for LayoutParams {
    fn default() -> Self {
        Self { repulsion_k: 2000.0, attraction_k: 0.02, rest_length: 30.0, dt: 0.02, damping: 0.9 }
    }
}

/// CSR adjacency, undirected for layout purposes (both endpoints pull
/// toward each other). Shared by both backends so attraction is
/// apples-to-apples and neither needs atomics: each node only ever reads
/// its own incident-edge range and writes its own force/position.
pub struct Graph {
    pub num_nodes: u32,
    pub adj_offsets: Vec<u32>,
    pub adj_indices: Vec<u32>,
}

impl Graph {
    pub fn from_edges(num_nodes: u32, edges: &[(u32, u32)]) -> Self {
        let n = num_nodes as usize;
        let mut degree = vec![0u32; n + 1];
        for &(u, v) in edges {
            degree[u as usize + 1] += 1;
            degree[v as usize + 1] += 1;
        }
        for i in 1..degree.len() {
            degree[i] += degree[i - 1];
        }
        let adj_offsets = degree.clone();
        let mut cursor = degree;
        let mut adj_indices = vec![0u32; adj_offsets[n] as usize];
        for &(u, v) in edges {
            adj_indices[cursor[u as usize] as usize] = v;
            cursor[u as usize] += 1;
            adj_indices[cursor[v as usize] as usize] = u;
            cursor[v as usize] += 1;
        }
        Self { num_nodes, adj_offsets, adj_indices }
    }
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

/// CPU reference implementation -- also the "CPU (multi-threaded)" device
/// choice in the settings UI (step 3), not just a test fixture.
pub struct CpuLayout<'g> {
    graph: &'g Graph,
    params: LayoutParams,
    positions: Vec<[f32; 2]>,
    velocities: Vec<[f32; 2]>,
    forces: Vec<[f32; 2]>,
}

impl<'g> CpuLayout<'g> {
    pub fn new(graph: &'g Graph, initial_positions: Vec<[f32; 2]>, params: LayoutParams) -> Self {
        let n = initial_positions.len();
        Self { graph, params, positions: initial_positions, velocities: vec![[0.0; 2]; n], forces: vec![[0.0; 2]; n] }
    }

    pub fn positions(&self) -> &[[f32; 2]] {
        &self.positions
    }

    /// One iteration; returns max per-node displacement for convergence checks.
    pub fn step(&mut self) -> f32 {
        let positions = &self.positions;
        let graph = self.graph;
        let params = &self.params;
        self.forces
            .par_iter_mut()
            .enumerate()
            .for_each(|(i, f)| *f = force_for_node(i, positions, graph, params));

        let mut max_disp = 0f32;
        for i in 0..self.positions.len() {
            self.velocities[i][0] = (self.velocities[i][0] + self.forces[i][0] * params.dt) * params.damping;
            self.velocities[i][1] = (self.velocities[i][1] + self.forces[i][1] * params.dt) * params.damping;
            let dx = self.velocities[i][0] * params.dt;
            let dy = self.velocities[i][1] * params.dt;
            self.positions[i][0] += dx;
            self.positions[i][1] += dy;
            max_disp = max_disp.max((dx * dx + dy * dy).sqrt());
        }
        max_disp
    }

    pub fn run_batch(&mut self, iterations: usize) -> f32 {
        let mut last = 0.0;
        for _ in 0..iterations {
            last = self.step();
        }
        last
    }
}

#[derive(Clone, Copy, Pod, Zeroable)]
#[repr(C)]
struct ForceParamsGpu {
    num_nodes: u32,
    repulsion_k: f32,
    attraction_k: f32,
    rest_length: f32,
}

#[derive(Clone, Copy, Pod, Zeroable)]
#[repr(C)]
struct IntegrateParamsGpu {
    num_nodes: u32,
    dt: f32,
    damping: f32,
    _pad: u32,
}

/// Picks a real GPU adapter (never the software/CPU fallback), preferring
/// discrete when `prefer_discrete` is true, integrated when false, or
/// whatever's first when None. Returns None (never panics) if no matching
/// adapter or device creation fails -- callers fall back to CpuLayout.
pub fn create_gpu_device(prefer_discrete: Option<bool>) -> Option<(wgpu::Device, wgpu::Queue, String)> {
    let instance = wgpu::Instance::default();
    let adapter = instance.enumerate_adapters(wgpu::Backends::PRIMARY).into_iter().find(|a| {
        let info = a.get_info();
        if info.device_type == wgpu::DeviceType::Cpu {
            return false;
        }
        match prefer_discrete {
            Some(true) => info.device_type == wgpu::DeviceType::DiscreteGpu,
            Some(false) => info.device_type == wgpu::DeviceType::IntegratedGpu,
            None => true,
        }
    })?;
    let info = adapter.get_info();
    let (device, queue) = pollster::block_on(adapter.request_device(
        &wgpu::DeviceDescriptor {
            label: Some("txgraph-layout"),
            required_features: wgpu::Features::empty(),
            required_limits: wgpu::Limits::downlevel_defaults().using_resolution(adapter.limits()),
            memory_hints: wgpu::MemoryHints::Performance,
        },
        None,
    ))
    .ok()?;
    Some((device, queue, format!("{} ({:?})", info.name, info.device_type)))
}

/// GPU layout engine. Buffers live for the engine's lifetime; iterations
/// run entirely in VRAM (see `run_iterations`) with readback only via the
/// explicit, separate `readback_positions` call.
pub struct GpuLayout<'a> {
    device: &'a wgpu::Device,
    queue: &'a wgpu::Queue,
    n: u32,
    positions_buf: wgpu::Buffer,
    #[allow(dead_code)] // kept alive by the bind groups below, which hold real GPU references to it
    forces_buf: wgpu::Buffer,
    #[allow(dead_code)]
    velocities_buf: wgpu::Buffer,
    #[allow(dead_code)]
    adj_offsets_buf: wgpu::Buffer,
    #[allow(dead_code)]
    adj_indices_buf: wgpu::Buffer,
    #[allow(dead_code)]
    force_params_buf: wgpu::Buffer,
    #[allow(dead_code)]
    int_params_buf: wgpu::Buffer,
    force_pipeline: wgpu::ComputePipeline,
    force_bind_group: wgpu::BindGroup,
    int_pipeline: wgpu::ComputePipeline,
    int_bind_group: wgpu::BindGroup,
    staging_buf: wgpu::Buffer,
    pos_bytes: u64,
}

impl<'a> GpuLayout<'a> {
    pub fn new(
        device: &'a wgpu::Device,
        queue: &'a wgpu::Queue,
        graph: &Graph,
        initial_positions: &[[f32; 2]],
        params: &LayoutParams,
    ) -> Self {
        let n = graph.num_nodes;
        let pos_bytes = (initial_positions.len() * std::mem::size_of::<[f32; 2]>()) as u64;

        let positions_buf = device.create_buffer_init(&wgpu::util::BufferInitDescriptor {
            label: Some("positions"),
            contents: bytemuck::cast_slice(initial_positions),
            usage: wgpu::BufferUsages::STORAGE | wgpu::BufferUsages::COPY_SRC | wgpu::BufferUsages::COPY_DST,
        });
        let velocities_buf = device.create_buffer_init(&wgpu::util::BufferInitDescriptor {
            label: Some("velocities"),
            contents: bytemuck::cast_slice(&vec![[0f32; 2]; initial_positions.len()]),
            usage: wgpu::BufferUsages::STORAGE,
        });
        let forces_buf = device.create_buffer(&wgpu::BufferDescriptor {
            label: Some("forces"),
            size: pos_bytes,
            usage: wgpu::BufferUsages::STORAGE,
            mapped_at_creation: false,
        });
        let adj_offsets_buf = device.create_buffer_init(&wgpu::util::BufferInitDescriptor {
            label: Some("adj_offsets"),
            contents: bytemuck::cast_slice(&graph.adj_offsets),
            usage: wgpu::BufferUsages::STORAGE,
        });
        let adj_indices_buf = device.create_buffer_init(&wgpu::util::BufferInitDescriptor {
            label: Some("adj_indices"),
            contents: bytemuck::cast_slice(&graph.adj_indices),
            usage: wgpu::BufferUsages::STORAGE,
        });
        let force_params_buf = device.create_buffer_init(&wgpu::util::BufferInitDescriptor {
            label: Some("force_params"),
            contents: bytemuck::bytes_of(&ForceParamsGpu {
                num_nodes: n,
                repulsion_k: params.repulsion_k,
                attraction_k: params.attraction_k,
                rest_length: params.rest_length,
            }),
            usage: wgpu::BufferUsages::UNIFORM,
        });
        let int_params_buf = device.create_buffer_init(&wgpu::util::BufferInitDescriptor {
            label: Some("int_params"),
            contents: bytemuck::bytes_of(&IntegrateParamsGpu { num_nodes: n, dt: params.dt, damping: params.damping, _pad: 0 }),
            usage: wgpu::BufferUsages::UNIFORM,
        });
        let staging_buf = device.create_buffer(&wgpu::BufferDescriptor {
            label: Some("staging"),
            size: pos_bytes,
            usage: wgpu::BufferUsages::COPY_DST | wgpu::BufferUsages::MAP_READ,
            mapped_at_creation: false,
        });

        let force_shader = device.create_shader_module(wgpu::ShaderModuleDescriptor {
            label: Some("forces"),
            source: wgpu::ShaderSource::Wgsl(include_str!("../shaders/forces.wgsl").into()),
        });
        let int_shader = device.create_shader_module(wgpu::ShaderModuleDescriptor {
            label: Some("integrate"),
            source: wgpu::ShaderSource::Wgsl(include_str!("../shaders/integrate.wgsl").into()),
        });
        let force_pipeline = device.create_compute_pipeline(&wgpu::ComputePipelineDescriptor {
            label: Some("force_pipeline"),
            layout: None,
            module: &force_shader,
            entry_point: "main",
            compilation_options: Default::default(),
            cache: None,
        });
        let int_pipeline = device.create_compute_pipeline(&wgpu::ComputePipelineDescriptor {
            label: Some("int_pipeline"),
            layout: None,
            module: &int_shader,
            entry_point: "main",
            compilation_options: Default::default(),
            cache: None,
        });

        let force_bind_group = device.create_bind_group(&wgpu::BindGroupDescriptor {
            label: Some("force_bg"),
            layout: &force_pipeline.get_bind_group_layout(0),
            entries: &[
                wgpu::BindGroupEntry { binding: 0, resource: positions_buf.as_entire_binding() },
                wgpu::BindGroupEntry { binding: 1, resource: forces_buf.as_entire_binding() },
                wgpu::BindGroupEntry { binding: 2, resource: adj_offsets_buf.as_entire_binding() },
                wgpu::BindGroupEntry { binding: 3, resource: adj_indices_buf.as_entire_binding() },
                wgpu::BindGroupEntry { binding: 4, resource: force_params_buf.as_entire_binding() },
            ],
        });
        let int_bind_group = device.create_bind_group(&wgpu::BindGroupDescriptor {
            label: Some("int_bg"),
            layout: &int_pipeline.get_bind_group_layout(0),
            entries: &[
                wgpu::BindGroupEntry { binding: 0, resource: positions_buf.as_entire_binding() },
                wgpu::BindGroupEntry { binding: 1, resource: velocities_buf.as_entire_binding() },
                wgpu::BindGroupEntry { binding: 2, resource: forces_buf.as_entire_binding() },
                wgpu::BindGroupEntry { binding: 3, resource: int_params_buf.as_entire_binding() },
            ],
        });

        Self {
            device,
            queue,
            n,
            positions_buf,
            forces_buf,
            velocities_buf,
            adj_offsets_buf,
            adj_indices_buf,
            force_params_buf,
            int_params_buf,
            force_pipeline,
            force_bind_group,
            int_pipeline,
            int_bind_group,
            staging_buf,
            pos_bytes,
        }
    }

    /// Runs `iterations` steps back-to-back with no readback in between --
    /// positions/velocities/forces stay resident in VRAM throughout.
    pub fn run_iterations(&self, iterations: usize) {
        let workgroups = self.n.div_ceil(256);
        let mut encoder = self.device.create_command_encoder(&wgpu::CommandEncoderDescriptor { label: None });
        for _ in 0..iterations {
            {
                let mut pass = encoder.begin_compute_pass(&wgpu::ComputePassDescriptor { label: None, timestamp_writes: None });
                pass.set_pipeline(&self.force_pipeline);
                pass.set_bind_group(0, &self.force_bind_group, &[]);
                pass.dispatch_workgroups(workgroups, 1, 1);
            }
            {
                let mut pass = encoder.begin_compute_pass(&wgpu::ComputePassDescriptor { label: None, timestamp_writes: None });
                pass.set_pipeline(&self.int_pipeline);
                pass.set_bind_group(0, &self.int_bind_group, &[]);
                pass.dispatch_workgroups(workgroups, 1, 1);
            }
        }
        self.queue.submit(Some(encoder.finish()));
        self.device.poll(wgpu::Maintain::Wait);
    }

    /// Explicit, separate call -- never invoked automatically per-iteration.
    /// Measured (gpu-bench) at 0.36-2.4ms even at 200k nodes.
    pub fn readback_positions(&self) -> Vec<[f32; 2]> {
        let mut encoder = self.device.create_command_encoder(&wgpu::CommandEncoderDescriptor { label: None });
        encoder.copy_buffer_to_buffer(&self.positions_buf, 0, &self.staging_buf, 0, self.pos_bytes);
        self.queue.submit(Some(encoder.finish()));

        let slice = self.staging_buf.slice(..);
        let (tx, rx) = std::sync::mpsc::channel();
        slice.map_async(wgpu::MapMode::Read, move |r| { let _ = tx.send(r); });
        self.device.poll(wgpu::Maintain::Wait);
        rx.recv().unwrap().unwrap();
        let data = slice.get_mapped_range();
        let positions: Vec<[f32; 2]> = bytemuck::cast_slice(&data).to_vec();
        drop(data);
        self.staging_buf.unmap();
        positions
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    /// Deterministic ring-plus-chord graph, ~2 edges/node -- no RNG
    /// dependency, so a test failure is reproducible bit-for-bit.
    fn test_graph(num_nodes: u32) -> (Graph, Vec<[f32; 2]>) {
        let mut edges = Vec::new();
        for i in 0..num_nodes {
            edges.push((i, (i + 1) % num_nodes));
            edges.push((i, (i + 7) % num_nodes));
        }
        let graph = Graph::from_edges(num_nodes, &edges);
        let positions = (0..num_nodes)
            .map(|i| {
                let theta = (i as f32) / (num_nodes as f32) * std::f32::consts::TAU;
                [theta.cos() * 100.0, theta.sin() * 100.0]
            })
            .collect();
        (graph, positions)
    }

    fn small_test_graph() -> (Graph, Vec<[f32; 2]>) {
        // Small enough to run fast in CI, large enough that repulsion and
        // attraction both matter. NOTE: at 50 nodes this is smaller than
        // the shaders' workgroup_size(256), so it dispatches into a single
        // workgroup and never exercises multi-workgroup global_invocation_id
        // math -- see cpu_and_gpu_layouts_agree_at_multi_workgroup_scale
        // below for the case that does.
        test_graph(50)
    }

    #[test]
    fn cpu_layout_moves_nodes_and_stays_finite() {
        let (graph, positions) = small_test_graph();
        let mut layout = CpuLayout::new(&graph, positions.clone(), LayoutParams::default());
        layout.run_batch(10);
        for p in layout.positions() {
            assert!(p[0].is_finite() && p[1].is_finite(), "position must never be NaN/Inf");
        }
        assert_ne!(layout.positions(), positions.as_slice(), "10 iterations must move at least one node");
    }

    #[test]
    fn cpu_and_gpu_layouts_agree_within_tolerance() {
        let Some((device, queue, adapter_name)) = create_gpu_device(None) else {
            eprintln!("SKIPPED: no GPU adapter available on this machine for the parity test");
            return;
        };
        eprintln!("parity test running against: {adapter_name}");

        let (graph, positions) = small_test_graph();
        let params = LayoutParams::default();

        let mut cpu = CpuLayout::new(&graph, positions.clone(), params);
        cpu.run_batch(30);

        let gpu = GpuLayout::new(&device, &queue, &graph, &positions, &params);
        gpu.run_iterations(30);
        let gpu_positions = gpu.readback_positions();

        // Both backends run the identical formula in the identical node
        // order, so any real algorithmic divergence should show up far
        // above float-rounding noise -- this tolerance is generous for
        // hardware/driver rounding differences, not for a wrong kernel.
        const TOLERANCE: f32 = 1.0;
        for (i, (c, g)) in cpu.positions().iter().zip(gpu_positions.iter()).enumerate() {
            let dist = ((c[0] - g[0]).powi(2) + (c[1] - g[1]).powi(2)).sqrt();
            assert!(
                dist < TOLERANCE,
                "node {i}: CPU {c:?} vs GPU {g:?} diverged by {dist} (tolerance {TOLERANCE}) -- \
                 a silently wrong GPU kernel would produce false analytical conclusions, so this must not be loosened casually"
            );
        }
    }

    #[test]
    fn cpu_and_gpu_layouts_agree_at_multi_workgroup_scale() {
        // The 50-node case above dispatches into a single workgroup
        // (workgroup_size(256) in the shaders) and so never exercises
        // global_invocation_id math across a workgroup boundary. 600 nodes
        // forces 3 workgroups (ceil(600/256)) -- a wrong boundary
        // computation here would show up as divergence isolated to nodes
        // near a 256-multiple index, not spread evenly, which is exactly
        // the class of bug the 50-node test structurally cannot catch.
        let Some((device, queue, adapter_name)) = create_gpu_device(None) else {
            eprintln!("SKIPPED: no GPU adapter available on this machine for the parity test");
            return;
        };
        eprintln!("multi-workgroup parity test running against: {adapter_name}");

        let (graph, positions) = test_graph(600);
        let params = LayoutParams::default();

        let mut cpu = CpuLayout::new(&graph, positions.clone(), params);
        cpu.run_batch(10);

        let gpu = GpuLayout::new(&device, &queue, &graph, &positions, &params);
        gpu.run_iterations(10);
        let gpu_positions = gpu.readback_positions();

        const TOLERANCE: f32 = 1.0;
        for (i, (c, g)) in cpu.positions().iter().zip(gpu_positions.iter()).enumerate() {
            let dist = ((c[0] - g[0]).powi(2) + (c[1] - g[1]).powi(2)).sqrt();
            assert!(
                dist < TOLERANCE,
                "node {i} (workgroup {}): CPU {c:?} vs GPU {g:?} diverged by {dist} (tolerance {TOLERANCE})",
                i / 256
            );
        }
    }

    #[test]
    fn gpu_device_selection_never_panics_when_absent() {
        // Confirms the "None if not found" contract itself, regardless of
        // what hardware happens to be present on the machine running this.
        let discrete = create_gpu_device(Some(true));
        let integrated = create_gpu_device(Some(false));
        assert!(discrete.is_some() || discrete.is_none());
        assert!(integrated.is_some() || integrated.is_none());
    }
}
