//! Standalone spike: does GPU compute clearly beat CPU for force-directed
//! graph layout at our actual sizes? Not part of the shipped app. See
//! gpu-bench/Cargo.toml and the session that requested this for the full
//! brief. Answers three things per (method, graph size):
//!   1. steady-state time per iteration
//!   2. time-to-settle (converged, or hit a bounded cap -- see RunCap)
//!   3. for GPU only: the cost of one VRAM -> CPU position readback,
//!      measured separately from compute, since the brief's hypothesis is
//!      that IPC/readback, not compute, is the real bottleneck.
//!
//! Repulsion is naive all-pairs O(n^2) on every backend (see forces.wgsl's
//! header for why) -- the numbers below are specifically about that
//! workload, not a verdict on force-directed layout in general.

use std::time::{Duration, Instant};

use bytemuck::{Pod, Zeroable};
use rand::prelude::*;
use rand_pcg::Pcg64;
use rayon::prelude::*;
use wgpu::util::DeviceExt;

const SIZES: [u32; 4] = [1_000, 10_000, 50_000, 200_000];
// Matches this project's own case-study snapshot (35,756 edges / 19,119
// vertices from docs/benchmark_results.md) rather than an arbitrary density.
const EDGE_RATIO: f32 = 1.87;

const REPULSION_K: f32 = 2000.0;
const ATTRACTION_K: f32 = 0.02;
const REST_LENGTH: f32 = 30.0;
const DT: f32 = 0.02;
const DAMPING: f32 = 0.9;
const CONVERGENCE_EPS: f32 = 0.05;

/// Bounds so no single combination can run away for hours -- a combination
/// that hits the cap without converging is itself a real, reportable
/// result ("this path cannot settle this graph in practical time"), not a
/// failure of the benchmark.
struct RunCap {
    max_iterations: usize,
    max_wall_clock: Duration,
}
const SETTLE_CAP: RunCap = RunCap { max_iterations: 500, max_wall_clock: Duration::from_secs(30) };
const TIMING_ITERATIONS: usize = 3; // steady-state per-iteration sample count, after 1 warmup

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

struct Graph {
    positions: Vec<[f32; 2]>,
    adj_offsets: Vec<u32>,
    adj_indices: Vec<u32>,
    edge_count: usize,
}

fn generate_graph(num_nodes: u32, seed: u64) -> Graph {
    let mut rng = Pcg64::seed_from_u64(seed);
    let positions: Vec<[f32; 2]> = (0..num_nodes)
        .map(|_| [rng.gen_range(-500.0..500.0), rng.gen_range(-500.0..500.0)])
        .collect();

    let num_edges = ((num_nodes as f32) * EDGE_RATIO) as usize;
    let mut edges = Vec::with_capacity(num_edges);
    for _ in 0..num_edges {
        let u = rng.gen_range(0..num_nodes);
        let mut v = rng.gen_range(0..num_nodes);
        if v == u {
            v = (v + 1) % num_nodes;
        }
        edges.push((u, v));
    }

    // CSR, undirected for layout purposes (both endpoints pull toward each
    // other) -- used identically by every CPU and GPU path below so the
    // attraction pass is apples-to-apples, and so no atomics are needed
    // anywhere (each node only ever writes its own force/position).
    let n = num_nodes as usize;
    let mut degree = vec![0u32; n + 1];
    for &(u, v) in &edges {
        degree[u as usize + 1] += 1;
        degree[v as usize + 1] += 1;
    }
    for i in 1..degree.len() {
        degree[i] += degree[i - 1];
    }
    let adj_offsets = degree.clone();
    let mut cursor = degree;
    let mut adj_indices = vec![0u32; adj_offsets[n] as usize];
    for &(u, v) in &edges {
        adj_indices[cursor[u as usize] as usize] = v;
        cursor[u as usize] += 1;
        adj_indices[cursor[v as usize] as usize] = u;
        cursor[v as usize] += 1;
    }

    Graph { positions, adj_offsets, adj_indices, edge_count: edges.len() }
}

#[inline]
fn force_for_node(i: usize, positions: &[[f32; 2]], adj_offsets: &[u32], adj_indices: &[u32]) -> [f32; 2] {
    let pi = positions[i];
    let mut f = [0f32; 2];
    for (j, pj) in positions.iter().enumerate() {
        if j == i {
            continue;
        }
        let dx = pi[0] - pj[0];
        let dy = pi[1] - pj[1];
        let dist2 = (dx * dx + dy * dy).max(0.0001);
        let k = REPULSION_K / (dist2 * dist2.sqrt());
        f[0] += dx * k;
        f[1] += dy * k;
    }
    let start = adj_offsets[i] as usize;
    let end = adj_offsets[i + 1] as usize;
    for &nb in &adj_indices[start..end] {
        let pj = positions[nb as usize];
        let dx = pj[0] - pi[0];
        let dy = pj[1] - pi[1];
        let dist = (dx * dx + dy * dy).sqrt().max(0.0001);
        let scale = ATTRACTION_K * (dist - REST_LENGTH) / dist;
        f[0] += dx * scale;
        f[1] += dy * scale;
    }
    f
}

fn integrate(
    positions: &mut [[f32; 2]],
    velocities: &mut [[f32; 2]],
    forces: &[[f32; 2]],
) -> f32 {
    let mut max_disp = 0f32;
    for i in 0..positions.len() {
        velocities[i][0] = (velocities[i][0] + forces[i][0] * DT) * DAMPING;
        velocities[i][1] = (velocities[i][1] + forces[i][1] * DT) * DAMPING;
        let dx = velocities[i][0] * DT;
        let dy = velocities[i][1] * DT;
        positions[i][0] += dx;
        positions[i][1] += dy;
        max_disp = max_disp.max((dx * dx + dy * dy).sqrt());
    }
    max_disp
}

#[derive(Clone, Copy, PartialEq)]
enum CpuMode {
    Single,
    Multi,
}

struct CpuRunner {
    positions: Vec<[f32; 2]>,
    velocities: Vec<[f32; 2]>,
    forces: Vec<[f32; 2]>,
    adj_offsets: Vec<u32>,
    adj_indices: Vec<u32>,
    mode: CpuMode,
}

impl CpuRunner {
    fn new(graph: &Graph, mode: CpuMode) -> Self {
        let n = graph.positions.len();
        Self {
            positions: graph.positions.clone(),
            velocities: vec![[0.0; 2]; n],
            forces: vec![[0.0; 2]; n],
            adj_offsets: graph.adj_offsets.clone(),
            adj_indices: graph.adj_indices.clone(),
            mode,
        }
    }

    /// One iteration; returns max per-node displacement for convergence checks.
    fn step(&mut self) -> f32 {
        let positions = &self.positions;
        let adj_offsets = &self.adj_offsets;
        let adj_indices = &self.adj_indices;
        match self.mode {
            CpuMode::Single => {
                for i in 0..positions.len() {
                    self.forces[i] = force_for_node(i, positions, adj_offsets, adj_indices);
                }
            }
            CpuMode::Multi => {
                self.forces
                    .par_iter_mut()
                    .enumerate()
                    .for_each(|(i, f)| *f = force_for_node(i, positions, adj_offsets, adj_indices));
            }
        }
        integrate(&mut self.positions, &mut self.velocities, &self.forces)
    }
}

struct GpuCtx {
    device: wgpu::Device,
    queue: wgpu::Queue,
    adapter_name: String,
}

fn init_gpu(power_preference: wgpu::PowerPreference, want_discrete: Option<bool>) -> Option<GpuCtx> {
    let instance = wgpu::Instance::default();
    let adapters: Vec<wgpu::Adapter> = instance.enumerate_adapters(wgpu::Backends::PRIMARY);
    let adapter = adapters.into_iter().find(|a| {
        let info = a.get_info();
        if info.device_type == wgpu::DeviceType::Cpu {
            return false;
        }
        match want_discrete {
            Some(true) => info.device_type == wgpu::DeviceType::DiscreteGpu,
            Some(false) => info.device_type == wgpu::DeviceType::IntegratedGpu,
            None => true,
        }
    })?;
    let info = adapter.get_info();
    let (device, queue) = pollster::block_on(adapter.request_device(
        &wgpu::DeviceDescriptor {
            label: Some("gpu-bench"),
            required_features: wgpu::Features::empty(),
            required_limits: wgpu::Limits::downlevel_defaults().using_resolution(adapter.limits()),
            memory_hints: wgpu::MemoryHints::Performance,
        },
        None,
    ))
    .ok()?;
    let _ = power_preference;
    Some(GpuCtx { device, queue, adapter_name: format!("{} ({:?})", info.name, info.device_type) })
}

struct GpuRunner<'a> {
    ctx: &'a GpuCtx,
    n: u32,
    positions_buf: wgpu::Buffer,
    forces_buf: wgpu::Buffer,
    velocities_buf: wgpu::Buffer,
    adj_offsets_buf: wgpu::Buffer,
    adj_indices_buf: wgpu::Buffer,
    force_params_buf: wgpu::Buffer,
    int_params_buf: wgpu::Buffer,
    force_pipeline: wgpu::ComputePipeline,
    force_bind_group: wgpu::BindGroup,
    int_pipeline: wgpu::ComputePipeline,
    int_bind_group: wgpu::BindGroup,
    staging_buf: wgpu::Buffer,
    pos_bytes: u64,
}

impl<'a> GpuRunner<'a> {
    fn new(ctx: &'a GpuCtx, graph: &Graph) -> Self {
        let device = &ctx.device;
        let n = graph.positions.len() as u32;
        let pos_bytes = (graph.positions.len() * std::mem::size_of::<[f32; 2]>()) as u64;

        let positions_buf = device.create_buffer_init(&wgpu::util::BufferInitDescriptor {
            label: Some("positions"),
            contents: bytemuck::cast_slice(&graph.positions),
            usage: wgpu::BufferUsages::STORAGE | wgpu::BufferUsages::COPY_SRC | wgpu::BufferUsages::COPY_DST,
        });
        let velocities_buf = device.create_buffer_init(&wgpu::util::BufferInitDescriptor {
            label: Some("velocities"),
            contents: bytemuck::cast_slice(&vec![[0f32; 2]; graph.positions.len()]),
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
        let force_params = ForceParamsGpu {
            num_nodes: n,
            repulsion_k: REPULSION_K,
            attraction_k: ATTRACTION_K,
            rest_length: REST_LENGTH,
        };
        let force_params_buf = device.create_buffer_init(&wgpu::util::BufferInitDescriptor {
            label: Some("force_params"),
            contents: bytemuck::bytes_of(&force_params),
            usage: wgpu::BufferUsages::UNIFORM,
        });
        let int_params = IntegrateParamsGpu { num_nodes: n, dt: DT, damping: DAMPING, _pad: 0 };
        let int_params_buf = device.create_buffer_init(&wgpu::util::BufferInitDescriptor {
            label: Some("int_params"),
            contents: bytemuck::bytes_of(&int_params),
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

        let force_layout = force_pipeline.get_bind_group_layout(0);
        let force_bind_group = device.create_bind_group(&wgpu::BindGroupDescriptor {
            label: Some("force_bg"),
            layout: &force_layout,
            entries: &[
                wgpu::BindGroupEntry { binding: 0, resource: positions_buf.as_entire_binding() },
                wgpu::BindGroupEntry { binding: 1, resource: forces_buf.as_entire_binding() },
                wgpu::BindGroupEntry { binding: 2, resource: adj_offsets_buf.as_entire_binding() },
                wgpu::BindGroupEntry { binding: 3, resource: adj_indices_buf.as_entire_binding() },
                wgpu::BindGroupEntry { binding: 4, resource: force_params_buf.as_entire_binding() },
            ],
        });
        let int_layout = int_pipeline.get_bind_group_layout(0);
        let int_bind_group = device.create_bind_group(&wgpu::BindGroupDescriptor {
            label: Some("int_bg"),
            layout: &int_layout,
            entries: &[
                wgpu::BindGroupEntry { binding: 0, resource: positions_buf.as_entire_binding() },
                wgpu::BindGroupEntry { binding: 1, resource: velocities_buf.as_entire_binding() },
                wgpu::BindGroupEntry { binding: 2, resource: forces_buf.as_entire_binding() },
                wgpu::BindGroupEntry { binding: 3, resource: int_params_buf.as_entire_binding() },
            ],
        });

        Self {
            ctx,
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

    /// Dispatches `iterations` compute passes back-to-back with NO readback
    /// in between -- the whole point being tested: the simulation stays
    /// resident in VRAM across iterations, host only submits work and waits
    /// once at the end of the batch.
    fn run_iterations_no_readback(&self, iterations: usize) {
        let workgroups = self.n.div_ceil(256);
        let mut encoder = self.ctx.device.create_command_encoder(&wgpu::CommandEncoderDescriptor { label: None });
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
        self.ctx.queue.submit(Some(encoder.finish()));
        self.ctx.device.poll(wgpu::Maintain::Wait);
    }

    /// The measurement the brief specifically asked for: cost of getting
    /// positions out of VRAM and into host memory, isolated from compute.
    fn readback_positions(&self) -> (Vec<[f32; 2]>, Duration) {
        let t0 = Instant::now();
        let mut encoder = self.ctx.device.create_command_encoder(&wgpu::CommandEncoderDescriptor { label: None });
        encoder.copy_buffer_to_buffer(&self.positions_buf, 0, &self.staging_buf, 0, self.pos_bytes);
        self.ctx.queue.submit(Some(encoder.finish()));

        let slice = self.staging_buf.slice(..);
        let (tx, rx) = std::sync::mpsc::channel();
        slice.map_async(wgpu::MapMode::Read, move |r| { let _ = tx.send(r); });
        self.ctx.device.poll(wgpu::Maintain::Wait);
        rx.recv().unwrap().unwrap();
        let data = slice.get_mapped_range();
        let positions: Vec<[f32; 2]> = bytemuck::cast_slice(&data).to_vec();
        drop(data);
        self.staging_buf.unmap();
        (positions, t0.elapsed())
    }
}

struct StepResult {
    per_iter: Duration,
    settle: Duration,
    settle_iterations: usize,
    converged: bool,
    readback: Option<Duration>,
}

fn bench_cpu(graph: &Graph, mode: CpuMode) -> StepResult {
    let mut runner = CpuRunner::new(graph, mode);
    runner.step(); // warm up (first-call cache effects, thread pool spin-up for rayon)
    let t0 = Instant::now();
    for _ in 0..TIMING_ITERATIONS {
        runner.step();
    }
    let per_iter = t0.elapsed() / TIMING_ITERATIONS as u32;

    let mut runner = CpuRunner::new(graph, mode);
    let t0 = Instant::now();
    let mut iterations = 0;
    let mut converged = false;
    while iterations < SETTLE_CAP.max_iterations && t0.elapsed() < SETTLE_CAP.max_wall_clock {
        let max_disp = runner.step();
        iterations += 1;
        if max_disp < CONVERGENCE_EPS {
            converged = true;
            break;
        }
    }
    StepResult { per_iter, settle: t0.elapsed(), settle_iterations: iterations, converged, readback: None }
}

fn bench_gpu(ctx: &GpuCtx, graph: &Graph) -> StepResult {
    let runner = GpuRunner::new(ctx, graph);
    runner.run_iterations_no_readback(1); // warm up (pipeline/shader compile is lazy on first dispatch on some drivers)
    let t0 = Instant::now();
    runner.run_iterations_no_readback(TIMING_ITERATIONS);
    let per_iter = t0.elapsed() / TIMING_ITERATIONS as u32;

    let (_, readback_time) = runner.readback_positions();

    // Batched settle loop: dispatch a batch of iterations with NO readback,
    // then do exactly one readback per batch to check convergence -- this
    // is the "periodic, not every frame" design the brief asked for, and
    // doubles as the settle-time measurement.
    let runner = GpuRunner::new(ctx, graph);
    const BATCH: usize = 20;
    let t0 = Instant::now();
    let mut iterations = 0;
    let mut converged = false;
    let mut prev: Option<Vec<[f32; 2]>> = None;
    while iterations < SETTLE_CAP.max_iterations && t0.elapsed() < SETTLE_CAP.max_wall_clock {
        runner.run_iterations_no_readback(BATCH);
        iterations += BATCH;
        let (positions, _) = runner.readback_positions();
        if let Some(prev_positions) = &prev {
            let max_disp = positions
                .iter()
                .zip(prev_positions.iter())
                .map(|(a, b)| ((a[0] - b[0]).powi(2) + (a[1] - b[1]).powi(2)).sqrt())
                .fold(0f32, f32::max);
            // divide by BATCH: this is displacement over BATCH iterations,
            // compared like-for-like against the CPU path's per-iteration eps
            if max_disp / (BATCH as f32) < CONVERGENCE_EPS {
                converged = true;
                prev = Some(positions);
                break;
            }
        }
        prev = Some(positions);
    }
    StepResult {
        per_iter,
        settle: t0.elapsed(),
        settle_iterations: iterations,
        converged,
        readback: Some(readback_time),
    }
}

fn fmt_result(label: &str, size: u32, r: &StepResult) {
    let settle_note = if r.converged {
        format!("converged in {} iters ({:.2?})", r.settle_iterations, r.settle)
    } else {
        format!(
            "DID NOT converge within cap ({} iters / {:.2?} -- extrapolated full settle would exceed the cap)",
            r.settle_iterations, r.settle
        )
    };
    let readback_note = match r.readback {
        Some(d) => format!(", VRAM->CPU readback: {d:.2?}"),
        None => String::new(),
    };
    println!("  [{label:>18}] n={size:<7} per-iter: {:>10.2?}  settle: {settle_note}{readback_note}", r.per_iter);
}

fn main() {
    println!("gpu-bench -- force-directed layout, CPU vs GPU (see module docs for methodology)\n");

    let dedicated = init_gpu(wgpu::PowerPreference::HighPerformance, Some(true));
    let integrated = init_gpu(wgpu::PowerPreference::LowPower, Some(false));

    match &dedicated {
        Some(g) => println!("Dedicated GPU found: {}", g.adapter_name),
        None => println!("Dedicated GPU: none found on this machine (expected on an integrated-only laptop)"),
    }
    match &integrated {
        Some(g) => println!("Integrated GPU found: {}", g.adapter_name),
        None => println!("Integrated GPU: none found"),
    }
    println!();

    for &size in &SIZES {
        let graph = generate_graph(size, 42);
        println!("=== n = {size} nodes, {} edges ===", graph.edge_count);

        let r = bench_cpu(&graph, CpuMode::Single);
        fmt_result("CPU single-thread", size, &r);

        let r = bench_cpu(&graph, CpuMode::Multi);
        fmt_result(&format!("CPU rayon ({}T)", rayon::current_num_threads()), size, &r);

        if let Some(ctx) = &integrated {
            let r = bench_gpu(ctx, &graph);
            fmt_result("GPU integrated", size, &r);
        }
        if let Some(ctx) = &dedicated {
            let r = bench_gpu(ctx, &graph);
            fmt_result("GPU dedicated", size, &r);
        }
        println!();
    }
}
