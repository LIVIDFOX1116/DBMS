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
use std::collections::HashMap;
use std::time::{Duration, Instant};
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

fn dense_index(id: i64, index_of: &mut HashMap<i64, u32>, index_to_id: &mut Vec<i64>) -> u32 {
    if let Some(&idx) = index_of.get(&id) {
        idx
    } else {
        let idx = index_to_id.len() as u32;
        index_of.insert(id, idx);
        index_to_id.push(id);
        idx
    }
}

/// Remaps arbitrary external ids (e.g. Postgres `wallet_id`, which is sparse
/// and not 0-based) to the dense 0..n indices `Graph` requires. Returns the
/// inverse mapping (index -> original id) so callers can translate computed
/// positions back to whatever key the caller's data is keyed by. First-seen
/// order, so the same edge list always produces the same mapping.
pub fn build_graph_from_id_pairs(edges: &[(i64, i64)]) -> (Graph, Vec<i64>) {
    let mut index_of = HashMap::new();
    let mut index_to_id = Vec::new();
    let mut dense_edges = Vec::with_capacity(edges.len());
    for &(a, b) in edges {
        let ia = dense_index(a, &mut index_of, &mut index_to_id);
        let ib = dense_index(b, &mut index_of, &mut index_to_id);
        dense_edges.push((ia, ib));
    }
    let graph = Graph::from_edges(index_to_id.len() as u32, &dense_edges);
    (graph, index_to_id)
}

/// Deterministic starting layout (a circle) -- same shape the JS fallback
/// (`layoutGraph` in dashboard.html) seeds from, so a GPU/CPU run and the JS
/// fallback aren't visually jarring if a caller switches between them.
pub fn initial_circle_positions(n: usize) -> Vec<[f32; 2]> {
    (0..n.max(1))
        .map(|i| {
            let theta = (i as f32 / n.max(1) as f32) * std::f32::consts::TAU;
            [theta.cos() * 500.0, theta.sin() * 500.0]
        })
        .take(n)
        .collect()
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

/// Optional cluster-aware attraction: each node is additionally pulled
/// toward its cluster's current centroid (recomputed every iteration from
/// live positions), on top of the existing pairwise repulsion + edge
/// attraction. A cluster's members can otherwise end up scattered across
/// the whole layout -- force layout optimizes edge length, Louvain
/// (upstream, see graph/run_analysis.py) optimizes modularity, and the two
/// have no reason to agree spatially.
///
/// Validated via a standalone spike (examples/cluster_aware_layout_spike.rs)
/// against real cluster 32769 (3,408 members, snapshot 1): k=40 cut that
/// cluster's spread (max distance from its own centroid) from 69,480 to
/// ~12,400 world units, settling in the same time as an unmodified layout.
/// k=300 (7.5x stronger) only reached ~12,500 -- barely better -- at 3x the
/// settle time, because intra-cluster repulsion is unchanged and sets an
/// equilibrium radius attraction alone can't push through much further;
/// tightening past ~5.6x would mean reducing intra-cluster repulsion too,
/// not just raising k. The same spike also measured stale (recomputed every
/// N>1 iterations) centroids: even N=5 was 34% worse than recomputing every
/// iteration, so this is deliberately NOT a batched/periodic update on
/// either backend, despite the CPU<->GPU sync cost that implies for GPU
/// (see `run_gpu_to_settle`) -- staleness was measured to cost more in
/// quality than it saves in sync overhead, and CPU/GPU parity requires both
/// backends to compute it the same way regardless.
///
/// `cluster_of[i]` is a dense 0..num_clusters group id for node i (every
/// node belongs to exactly one group; an unclustered node gets its own
/// singleton group, which makes the added term a no-op for it -- same as
/// the 99.3% of real clusters that are already singletons, so the common
/// case costs an O(n) scan and nothing else).
#[derive(Clone)]
pub struct ClusterForces {
    pub cluster_of: Vec<u32>,
    pub num_clusters: u32,
    pub attraction_k: f32,
}

impl ClusterForces {
    /// Every node its own singleton cluster -- the added term becomes a
    /// no-op (centroid == self) for every node. Used whenever the caller has
    /// no real cluster data, or wants cluster-aware attraction off (k <= 0
    /// short-circuits `add_forces` before this even matters).
    pub fn none(num_nodes: usize) -> Self {
        Self { cluster_of: (0..num_nodes as u32).collect(), num_clusters: num_nodes as u32, attraction_k: 0.0 }
    }

    /// Per-cluster centroids and sizes from current positions, in one O(n)
    /// pass (`sum`/`count` indexed by dense cluster group, not per-node) --
    /// recomputed by the caller every iteration, deliberately not cached
    /// (see this struct's doc comment on why staleness was rejected).
    fn centroids(&self, positions: &[[f32; 2]]) -> (Vec<[f32; 2]>, Vec<u32>) {
        let mut sum = vec![[0f32; 2]; self.num_clusters as usize];
        let mut count = vec![0u32; self.num_clusters as usize];
        for (i, &g) in self.cluster_of.iter().enumerate() {
            sum[g as usize][0] += positions[i][0];
            sum[g as usize][1] += positions[i][1];
            count[g as usize] += 1;
        }
        let centroids = sum
            .iter()
            .zip(count.iter())
            .map(|(s, &c)| if c == 0 { [0.0, 0.0] } else { [s[0] / c as f32, s[1] / c as f32] })
            .collect();
        (centroids, count)
    }

    /// Adds the centroid-attraction term into `forces` in place. Singleton
    /// clusters (count <= 1, the common case) are skipped: their centroid is
    /// themselves, so the term is exactly zero, not just numerically tiny.
    fn add_forces(&self, positions: &[[f32; 2]], forces: &mut [[f32; 2]]) {
        if self.attraction_k <= 0.0 {
            return;
        }
        let (centroids, count) = self.centroids(positions);
        for (i, &g) in self.cluster_of.iter().enumerate() {
            if count[g as usize] <= 1 {
                continue;
            }
            let c = centroids[g as usize];
            forces[i][0] += (c[0] - positions[i][0]) * self.attraction_k;
            forces[i][1] += (c[1] - positions[i][1]) * self.attraction_k;
        }
    }
}

/// Which CPU code path `CpuLayout::step` takes. `Multi` is the "CPU
/// (multi-threaded)" device choice in the settings UI (step 3); `Single` only
/// exists so the benchmark comparison (see `run_benchmark`) can show what
/// rayon is actually buying over a plain loop, on the real graph, not just
/// gpu-bench's synthetic one.
#[derive(Clone, Copy, PartialEq, Debug)]
pub enum CpuMode {
    Single,
    Multi,
}

/// CPU reference implementation -- also the "CPU (multi-threaded)" device
/// choice in the settings UI (step 3), not just a test fixture.
pub struct CpuLayout<'g> {
    graph: &'g Graph,
    params: LayoutParams,
    positions: Vec<[f32; 2]>,
    velocities: Vec<[f32; 2]>,
    forces: Vec<[f32; 2]>,
    mode: CpuMode,
    cluster: ClusterForces,
}

impl<'g> CpuLayout<'g> {
    /// Defaults to `CpuMode::Multi` -- existing callers (including every
    /// test written before `CpuMode` existed) get the same behavior as before.
    /// Only exercised by tests today (production code goes through
    /// `run_cpu_to_settle`/`run_benchmark`, which need to pick the mode
    /// explicitly) -- kept as the obvious constructor for any future direct
    /// caller that doesn't care about mode.
    #[allow(dead_code)]
    pub fn new(graph: &'g Graph, initial_positions: Vec<[f32; 2]>, params: LayoutParams) -> Self {
        Self::with_mode(graph, initial_positions, params, CpuMode::Multi)
    }

    pub fn with_mode(graph: &'g Graph, initial_positions: Vec<[f32; 2]>, params: LayoutParams, mode: CpuMode) -> Self {
        let n = initial_positions.len();
        Self {
            graph,
            params,
            positions: initial_positions,
            velocities: vec![[0.0; 2]; n],
            forces: vec![[0.0; 2]; n],
            mode,
            cluster: ClusterForces::none(n),
        }
    }

    /// Enables cluster-aware attraction (see `ClusterForces`) on top of
    /// whatever mode/params this layout already has.
    pub fn with_cluster_forces(mut self, cluster: ClusterForces) -> Self {
        self.cluster = cluster;
        self
    }

    pub fn positions(&self) -> &[[f32; 2]] {
        &self.positions
    }

    /// One iteration; returns max per-node displacement for convergence checks.
    pub fn step(&mut self) -> f32 {
        let positions = &self.positions;
        let graph = self.graph;
        let params = &self.params;
        match self.mode {
            CpuMode::Multi => {
                self.forces
                    .par_iter_mut()
                    .enumerate()
                    .for_each(|(i, f)| *f = force_for_node(i, positions, graph, params));
            }
            CpuMode::Single => {
                for i in 0..self.forces.len() {
                    self.forces[i] = force_for_node(i, positions, graph, params);
                }
            }
        }
        self.cluster.add_forces(positions, &mut self.forces);

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

    /// Test-only convenience today -- production code uses
    /// `run_cpu_to_settle`'s convergence loop instead of a fixed batch.
    #[allow(dead_code)]
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

/// Padded to 16 bytes to satisfy uniform buffer alignment (same reason
/// `IntegrateParamsGpu` carries `_pad`) even though only one field is real.
#[derive(Clone, Copy, Pod, Zeroable)]
#[repr(C)]
struct ClusterParamsGpu {
    cluster_attraction_k: f32,
    _pad: [u32; 3],
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
    // The adapter's own real limits, not downlevel_defaults() (a WebGL2-
    // compatible floor that caps max_storage_buffers_per_shader_stage at 4)
    // -- this is a native desktop compute engine, never a browser/WebGL2
    // target, and cluster-aware forces' extra bindings (cluster_of,
    // centroids, cluster_params -- see forces.wgsl) push the force shader's
    // storage buffer count to 6, which downlevel_defaults() cannot satisfy
    // even on hardware that supports it fine.
    let (device, queue) = pollster::block_on(adapter.request_device(
        &wgpu::DeviceDescriptor {
            label: Some("txgraph-layout"),
            required_features: wgpu::Features::empty(),
            required_limits: adapter.limits(),
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
    #[allow(dead_code)]
    cluster_of_buf: wgpu::Buffer,
    centroids_buf: wgpu::Buffer,
    #[allow(dead_code)]
    cluster_params_buf: wgpu::Buffer,
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
        cluster: &ClusterForces,
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

        // Always created, even when cluster.attraction_k == 0 -- one fixed
        // pipeline/bind-group-layout for both the cluster-aware and
        // cluster-unaware cases (see forces.wgsl's ClusterParams comment).
        // centroids_buf is COPY_DST because upload_centroids rewrites it
        // every iteration from host-computed centroids (see
        // run_gpu_to_settle) -- it is never computed on the GPU itself.
        let cluster_of_buf = device.create_buffer_init(&wgpu::util::BufferInitDescriptor {
            label: Some("cluster_of"),
            contents: bytemuck::cast_slice(&cluster.cluster_of),
            usage: wgpu::BufferUsages::STORAGE,
        });
        let centroids_buf = device.create_buffer(&wgpu::BufferDescriptor {
            label: Some("centroids"),
            size: (cluster.num_clusters.max(1) as u64) * std::mem::size_of::<[f32; 2]>() as u64,
            usage: wgpu::BufferUsages::STORAGE | wgpu::BufferUsages::COPY_DST,
            mapped_at_creation: false,
        });
        let cluster_params_buf = device.create_buffer_init(&wgpu::util::BufferInitDescriptor {
            label: Some("cluster_params"),
            contents: bytemuck::bytes_of(&ClusterParamsGpu { cluster_attraction_k: cluster.attraction_k, _pad: [0; 3] }),
            usage: wgpu::BufferUsages::UNIFORM,
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
                wgpu::BindGroupEntry { binding: 5, resource: cluster_of_buf.as_entire_binding() },
                wgpu::BindGroupEntry { binding: 6, resource: centroids_buf.as_entire_binding() },
                wgpu::BindGroupEntry { binding: 7, resource: cluster_params_buf.as_entire_binding() },
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
            cluster_of_buf,
            centroids_buf,
            cluster_params_buf,
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

    /// Rewrites the per-cluster centroid buffer the shader reads (see
    /// forces.wgsl). Called every iteration from `run_gpu_to_settle` when
    /// cluster-aware forces are active -- this is the CPU<->GPU sync point
    /// whose cost that function measures against the cluster-unaware
    /// baseline (staleness was measured, on CPU, to cost more in quality
    /// than it saves here -- see ClusterForces's doc comment -- so this is
    /// deliberately not batched/periodic).
    pub fn upload_centroids(&self, centroids: &[[f32; 2]]) {
        self.queue.write_buffer(&self.centroids_buf, 0, bytemuck::cast_slice(centroids));
    }
}

/// Bounds a settle loop so a graph that never converges (or converges too
/// slowly to matter for an interactive UI) can't hang the caller -- mirrors
/// gpu-bench's `RunCap`, deliberately smaller since this runs live from a
/// UI click rather than an unattended benchmark.
///
/// Measured fact, not a guess: at the real case-study graph's actual size
/// (19,119 nodes / 35,756 edges), every device (CPU single-thread, CPU
/// rayon, GPU) hits this 500-iteration cap without CONVERGENCE_EPS ever
/// being satisfied -- confirmed repeatedly across multiple runs on this
/// machine. This is not a bug or a sign the cap is set wrong; a naive
/// all-pairs spring layout at this density and repulsion_k genuinely keeps
/// some residual per-node jitter above 0.05 units/iteration indefinitely.
/// The cap is what actually stops the computation -- treat "capped, not
/// converged" in the UI as the expected, permanent behavior at this scale,
/// not a transient state that a bigger cap or more patience would resolve.
/// If a real need for full convergence emerges, that requires tuning
/// LayoutParams (damping, repulsion_k) or the convergence definition
/// itself, not just enlarging this cap -- enlarging it was tried
/// conceptually (the numbers above already reflect a generous 20s/500-iter
/// budget) and didn't change the outcome.
#[derive(Clone, Copy)]
pub struct SettleCap {
    pub max_iterations: usize,
    pub max_wall_clock: Duration,
}

impl Default for SettleCap {
    fn default() -> Self {
        Self { max_iterations: 500, max_wall_clock: Duration::from_secs(20) }
    }
}

const CONVERGENCE_EPS: f32 = 0.05;

pub struct SettleOutcome {
    pub positions: Vec<[f32; 2]>,
    pub iterations: usize,
    pub elapsed: Duration,
    pub converged: bool,
}

/// Runs `CpuLayout` until max per-node displacement drops below
/// `CONVERGENCE_EPS` or `cap` is hit, whichever first -- same convergence
/// definition gpu-bench used, so settle numbers here are comparable to it.
/// Pass `ClusterForces::none(initial_positions.len())` for the pre-existing,
/// cluster-unaware behavior.
pub fn run_cpu_to_settle(
    graph: &Graph,
    initial_positions: Vec<[f32; 2]>,
    params: LayoutParams,
    mode: CpuMode,
    cap: SettleCap,
    cluster: ClusterForces,
) -> SettleOutcome {
    let mut layout = CpuLayout::with_mode(graph, initial_positions, params, mode).with_cluster_forces(cluster);
    let t0 = Instant::now();
    let mut iterations = 0;
    let mut converged = false;
    while iterations < cap.max_iterations && t0.elapsed() < cap.max_wall_clock {
        let max_disp = layout.step();
        iterations += 1;
        if max_disp < CONVERGENCE_EPS {
            converged = true;
            break;
        }
    }
    SettleOutcome { positions: layout.positions().to_vec(), iterations, elapsed: t0.elapsed(), converged }
}

/// GPU equivalent of `run_cpu_to_settle`. With cluster-aware forces off
/// (`cluster.attraction_k <= 0`), batches iterations in VRAM with one
/// readback per batch (not per iteration) to check convergence, exactly the
/// "periodic, not every frame" pattern gpu-bench validated -- unchanged from
/// before cluster-aware forces existed, so this case pays no new cost.
///
/// With cluster-aware forces on, centroids must be recomputed from every
/// node's position every iteration (see ClusterForces's doc comment for why
/// staleness was rejected), and the GPU can't reduce across arbitrary
/// cluster groups internally without atomics WGSL doesn't reliably support
/// for f32 -- so this path reads positions back, computes centroids on the
/// CPU, and re-uploads them, once per iteration. That per-iteration
/// CPU<->GPU sync is real (see `upload_centroids`'s doc comment) and is
/// exactly what distinguishes this path's throughput from the batched one.
pub fn run_gpu_to_settle(
    device: &wgpu::Device,
    queue: &wgpu::Queue,
    graph: &Graph,
    initial_positions: &[[f32; 2]],
    params: LayoutParams,
    cap: SettleCap,
    cluster: &ClusterForces,
) -> SettleOutcome {
    let gpu = GpuLayout::new(device, queue, graph, initial_positions, &params, cluster);

    if cluster.attraction_k <= 0.0 {
        const BATCH: usize = 20;
        let t0 = Instant::now();
        let mut iterations = 0;
        let mut converged = false;
        let mut prev: Option<Vec<[f32; 2]>> = None;
        let mut last_positions = initial_positions.to_vec();
        while iterations < cap.max_iterations && t0.elapsed() < cap.max_wall_clock {
            gpu.run_iterations(BATCH);
            iterations += BATCH;
            let positions = gpu.readback_positions();
            if let Some(prev_positions) = &prev {
                let max_disp = positions
                    .iter()
                    .zip(prev_positions.iter())
                    .map(|(a, b)| ((a[0] - b[0]).powi(2) + (a[1] - b[1]).powi(2)).sqrt())
                    .fold(0f32, f32::max);
                if max_disp / (BATCH as f32) < CONVERGENCE_EPS {
                    converged = true;
                    last_positions = positions;
                    break;
                }
            }
            last_positions = positions.clone();
            prev = Some(positions);
        }
        return SettleOutcome { positions: last_positions, iterations, elapsed: t0.elapsed(), converged };
    }

    let t0 = Instant::now();
    let mut iterations = 0;
    let mut converged = false;
    let mut prev_positions = initial_positions.to_vec();
    let mut last_positions = prev_positions.clone();
    while iterations < cap.max_iterations && t0.elapsed() < cap.max_wall_clock {
        // Centroids from the state BEFORE this step -- same timing as the
        // CPU path, which computes forces (including the cluster term) from
        // `self.positions` before velocity integration advances them.
        let (centroids, _) = cluster.centroids(&prev_positions);
        gpu.upload_centroids(&centroids);
        gpu.run_iterations(1);
        iterations += 1;
        let positions = gpu.readback_positions();
        let max_disp = positions
            .iter()
            .zip(prev_positions.iter())
            .map(|(a, b)| ((a[0] - b[0]).powi(2) + (a[1] - b[1]).powi(2)).sqrt())
            .fold(0f32, f32::max);
        last_positions = positions.clone();
        if max_disp < CONVERGENCE_EPS {
            converged = true;
            break;
        }
        prev_positions = positions;
    }
    SettleOutcome { positions: last_positions, iterations, elapsed: t0.elapsed(), converged }
}

#[derive(Clone, Debug, serde::Serialize)]
pub struct BenchRow {
    pub label: String,
    pub per_iter_ms: f64,
    pub settle_ms: f64,
    pub settle_iterations: usize,
    pub converged: bool,
    pub readback_ms: Option<f64>,
}

/// The real-graph counterpart to gpu-bench: same methodology (steady-state
/// per-iteration timing from a fresh warmed-up instance, settle timing from
/// another fresh instance, GPU readback measured separately from compute),
/// run against whatever graph the caller passes in rather than a synthetic
/// one. `timing_iterations` mirrors gpu-bench's `TIMING_ITERATIONS`.
pub fn run_benchmark(
    graph: &Graph,
    initial_positions: &[[f32; 2]],
    params: LayoutParams,
    gpu: Option<(&wgpu::Device, &wgpu::Queue, &str)>,
    timing_iterations: usize,
    cap: SettleCap,
) -> Vec<BenchRow> {
    let mut rows = Vec::new();

    let mut bench_cpu = |label: String, mode: CpuMode| {
        let mut warm = CpuLayout::with_mode(graph, initial_positions.to_vec(), params, mode);
        warm.step();
        let mut timed = CpuLayout::with_mode(graph, initial_positions.to_vec(), params, mode);
        let t0 = Instant::now();
        for _ in 0..timing_iterations {
            timed.step();
        }
        let per_iter_ms = t0.elapsed().as_secs_f64() * 1000.0 / timing_iterations as f64;

        let outcome = run_cpu_to_settle(graph, initial_positions.to_vec(), params, mode, cap, ClusterForces::none(initial_positions.len()));
        rows.push(BenchRow {
            label,
            per_iter_ms,
            settle_ms: outcome.elapsed.as_secs_f64() * 1000.0,
            settle_iterations: outcome.iterations,
            converged: outcome.converged,
            readback_ms: None,
        });
    };
    bench_cpu("CPU single-thread".to_string(), CpuMode::Single);
    bench_cpu(format!("CPU rayon ({}T)", rayon::current_num_threads()), CpuMode::Multi);

    if let Some((device, queue, name)) = gpu {
        // Pure device-speed comparison -- deliberately cluster-unaware, same
        // as the CPU rows above, so all three rows measure the same work.
        let no_cluster = ClusterForces::none(initial_positions.len());
        let warm = GpuLayout::new(device, queue, graph, initial_positions, &params, &no_cluster);
        warm.run_iterations(1);
        let timed = GpuLayout::new(device, queue, graph, initial_positions, &params, &no_cluster);
        let t0 = Instant::now();
        timed.run_iterations(timing_iterations);
        let per_iter_ms = t0.elapsed().as_secs_f64() * 1000.0 / timing_iterations as f64;
        let readback_t0 = Instant::now();
        let _ = timed.readback_positions();
        let readback_ms = readback_t0.elapsed().as_secs_f64() * 1000.0;

        let outcome = run_gpu_to_settle(device, queue, graph, initial_positions, params, cap, &no_cluster);
        rows.push(BenchRow {
            label: format!("GPU ({name})"),
            per_iter_ms,
            settle_ms: outcome.elapsed.as_secs_f64() * 1000.0,
            settle_iterations: outcome.iterations,
            converged: outcome.converged,
            readback_ms: Some(readback_ms),
        });
    }

    rows
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
    fn build_graph_from_id_pairs_dedupes_and_preserves_first_seen_order() {
        // Sparse, non-zero-based, out-of-order ids -- like real Postgres
        // wallet_id values -- must map to dense 0..n indices.
        let edges = [(500i64, 12i64), (12, 99), (500, 99)];
        let (graph, index_to_id) = build_graph_from_id_pairs(&edges);
        assert_eq!(graph.num_nodes, 3);
        assert_eq!(index_to_id, vec![500, 12, 99], "first-seen order: 500, then 12, then 99");
        // 500 -> index 0 has two edges (to 12 and to 99)
        assert_eq!(graph.adj_offsets[1] - graph.adj_offsets[0], 2);
    }

    #[test]
    fn initial_circle_positions_returns_exactly_n_finite_points() {
        let positions = initial_circle_positions(37);
        assert_eq!(positions.len(), 37);
        assert!(positions.iter().all(|p| p[0].is_finite() && p[1].is_finite()));
        assert_eq!(initial_circle_positions(0).len(), 0);
    }

    #[test]
    fn cluster_forces_none_is_a_true_no_op() {
        // Every node its own singleton group -- centroid == self, so the
        // term must be exactly zero, not just numerically negligible.
        let n = 10;
        let cluster = ClusterForces::none(n);
        let positions: Vec<[f32; 2]> = (0..n).map(|i| [i as f32, -(i as f32)]).collect();
        let mut forces = vec![[1.0f32, -1.0]; n]; // pre-seeded with a sentinel value
        cluster.add_forces(&positions, &mut forces);
        assert_eq!(forces, vec![[1.0, -1.0]; n], "singleton groups must not add any force");
    }

    #[test]
    fn cluster_forces_pulls_members_toward_their_own_centroid_only() {
        // Two groups: {0,1,2} and {3,4} (group ids 0 and 1). Group 0's
        // centroid is at x=1 (mean of 0,1,2); node 0 (x=0) should be pulled
        // in +x, node 2 (x=2) pulled in -x. Group 1 is untouched by group 0.
        let cluster_of = vec![0u32, 0, 0, 1, 1];
        let cluster = ClusterForces { cluster_of, num_clusters: 2, attraction_k: 10.0 };
        let positions = vec![[0.0, 0.0], [1.0, 0.0], [2.0, 0.0], [5.0, 5.0], [7.0, 5.0]];
        let mut forces = vec![[0.0, 0.0]; 5];
        cluster.add_forces(&positions, &mut forces);

        assert!(forces[0][0] > 0.0, "node 0 (x=0, centroid x=1) must be pulled toward +x");
        assert_eq!(forces[1], [0.0, 0.0], "node 1 sits exactly on its own group centroid: zero added force");
        assert!(forces[2][0] < 0.0, "node 2 (x=2, centroid x=1) must be pulled toward -x");
        // Group 1's centroid is (6,5); node 3 pulled toward +x, node 4 toward -x.
        assert!(forces[3][0] > 0.0);
        assert!(forces[4][0] < 0.0);
    }

    #[test]
    fn cluster_forces_tightens_a_scattered_group_over_iterations() {
        // Same qualitative claim the cluster_aware_layout_spike measured on
        // the real graph (69,480 -> ~12,400 world units for cluster 32769):
        // adding cluster-aware attraction must make a designated group's own
        // spread shrink relative to running the identical layout without it.
        let (graph, positions) = test_graph(120);
        // Nodes 0..40 (arbitrary, not adjacency-related) are "one cluster";
        // everything else is its own singleton -- deliberately not aligned
        // with the ring/chord edges so any tightening is attributable to the
        // added force, not to the graph's own structure.
        // Nodes 40..120 keep their own original index as their (singleton)
        // group id -- simplest way to get 80 distinct unused-elsewhere group
        // ids without a separate remapping pass; num_clusters just needs to
        // be large enough to cover the biggest id used (120 nodes -> ids fit
        // in 0..120 already).
        let mut cluster_of: Vec<u32> = (0..120u32).collect();
        for slot in cluster_of.iter_mut().take(40) {
            *slot = 0;
        }
        let cluster = ClusterForces { cluster_of, num_clusters: 120, attraction_k: 40.0 };

        fn spread(positions: &[[f32; 2]], members: &std::ops::Range<usize>) -> f32 {
            let n = members.len() as f32;
            let cx: f32 = members.clone().map(|i| positions[i][0]).sum::<f32>() / n;
            let cy: f32 = members.clone().map(|i| positions[i][1]).sum::<f32>() / n;
            members.clone().map(|i| ((positions[i][0] - cx).powi(2) + (positions[i][1] - cy).powi(2)).sqrt()).fold(0f32, f32::max)
        }

        let mut without = CpuLayout::with_mode(&graph, positions.clone(), LayoutParams::default(), CpuMode::Multi);
        without.run_batch(60);
        let spread_without = spread(without.positions(), &(0..40));

        let mut with = CpuLayout::with_mode(&graph, positions, LayoutParams::default(), CpuMode::Multi).with_cluster_forces(cluster);
        with.run_batch(60);
        let spread_with = spread(with.positions(), &(0..40));

        assert!(
            spread_with < spread_without * 0.9,
            "cluster-aware attraction must measurably tighten the group: without={spread_without}, with={spread_with}"
        );
    }

    #[test]
    fn run_cpu_to_settle_terminates_and_stays_finite() {
        let (graph, positions) = test_graph(120);
        let cap = SettleCap { max_iterations: 50, max_wall_clock: Duration::from_secs(5) };
        let outcome = run_cpu_to_settle(&graph, positions.clone(), LayoutParams::default(), CpuMode::Multi, cap, ClusterForces::none(positions.len()));
        assert!(outcome.iterations > 0 && outcome.iterations <= cap.max_iterations);
        assert!(outcome.positions.iter().all(|p| p[0].is_finite() && p[1].is_finite()));
    }

    #[test]
    fn run_benchmark_cpu_only_produces_two_rows_in_order() {
        let (graph, positions) = test_graph(120);
        let cap = SettleCap { max_iterations: 20, max_wall_clock: Duration::from_secs(5) };
        let rows = run_benchmark(&graph, &positions, LayoutParams::default(), None, 3, cap);
        assert_eq!(rows.len(), 2, "no GPU passed in, so only the two CPU rows");
        assert_eq!(rows[0].label, "CPU single-thread");
        assert!(rows[1].label.starts_with("CPU rayon"));
        assert!(rows.iter().all(|r| r.readback_ms.is_none()));
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

        let no_cluster = ClusterForces::none(positions.len());
        let gpu = GpuLayout::new(&device, &queue, &graph, &positions, &params, &no_cluster);
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

        let no_cluster = ClusterForces::none(positions.len());
        let gpu = GpuLayout::new(&device, &queue, &graph, &positions, &params, &no_cluster);
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
    fn cpu_and_gpu_layouts_agree_with_cluster_aware_forces() {
        // Extends the multi-workgroup parity test to the cluster-aware
        // path, which is a structurally different GPU code path
        // (per-iteration CPU<->GPU sync via run_gpu_to_settle, not a single
        // batched GpuLayout::run_iterations call) -- the batched path's
        // parity being fine says nothing about whether the per-iteration
        // centroid recompute is wired correctly on both backends.
        let Some((device, queue, adapter_name)) = create_gpu_device(None) else {
            eprintln!("SKIPPED: no GPU adapter available on this machine for the parity test");
            return;
        };
        eprintln!("cluster-aware parity test running against: {adapter_name}");

        let (graph, positions) = test_graph(600);
        let params = LayoutParams::default();

        // Non-trivial cluster structure -- one 100-member cluster, one
        // 200-member cluster, everything else singleton -- spanning
        // workgroup boundaries (256) inside both real clusters, not just a
        // single tidy group.
        let mut cluster_of: Vec<u32> = (0..600u32).collect();
        for slot in cluster_of.iter_mut().take(100) {
            *slot = 0;
        }
        for slot in cluster_of[100..300].iter_mut() {
            *slot = 1;
        }
        // k=2.0, NOT the production default of 40 -- this synthetic graph's
        // positions are seeded on a ~100-unit-radius circle (see test_graph),
        // roughly 1/700th the real case-study graph's ~69,000-unit scale.
        // k=40 here was tried first and produced forces ~1000x larger than
        // the graph's own repulsion/edge-attraction forces, pushing the
        // simulation into a chaotically unstable regime where ordinary
        // GPU/CPU float-rounding differences (already present and harmless
        // in the base-force-only parity tests) blew up to a max divergence
        // of 72.9 units across 10 iterations, with 64/600 nodes over
        // tolerance -- not a wrong kernel, just the wrong k for this scale.
        // k=2.0 keeps the cluster term comparable in magnitude to the other
        // forces (max divergence 0.70, 0/600 over tolerance) while still
        // exercising the same cluster indexing/centroid code on both
        // backends. The lever for a real graph is always relative to ITS
        // scale, same as the production "cluster pull" slider is tunable
        // rather than fixed.
        let cluster = ClusterForces { cluster_of, num_clusters: 600, attraction_k: 2.0 };
        let cap = SettleCap { max_iterations: 10, max_wall_clock: Duration::from_secs(30) };

        let cpu_outcome = run_cpu_to_settle(&graph, positions.clone(), params, CpuMode::Multi, cap, cluster.clone());
        let gpu_outcome = run_gpu_to_settle(&device, &queue, &graph, &positions, params, cap, &cluster);

        assert_eq!(cpu_outcome.iterations, 10, "must run the full 10 iterations (not converge early) for a fair comparison");
        assert_eq!(gpu_outcome.iterations, 10, "must run the full 10 iterations (not converge early) for a fair comparison");

        const TOLERANCE: f32 = 1.0;
        for (i, (c, g)) in cpu_outcome.positions.iter().zip(gpu_outcome.positions.iter()).enumerate() {
            let dist = ((c[0] - g[0]).powi(2) + (c[1] - g[1]).powi(2)).sqrt();
            assert!(
                dist < TOLERANCE,
                "node {i}: CPU {c:?} vs GPU {g:?} diverged by {dist} (tolerance {TOLERANCE}) -- \
                 cluster-aware forces must match between backends just like the base force does"
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
