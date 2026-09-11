//! Spawns, health-checks, and tears down the FastAPI backend child process.
//!
//! Dev builds run api/main.py through the Windows-native venv
//! (.venv-win/, CPU-only deps -- see requirements.txt) directly from the
//! source tree. Packaged (release) builds spawn the PyInstaller-frozen
//! sidecar built by scripts/build_backend_sidecar.py instead (desktop-app
//! packaging, Part 1) -- see resolve_backend_command() below for both paths.
//! We spawn it with plain std::process::Command rather than going through
//! tauri-plugin-shell's sidecar API: Tauri's externalBin bundling step (see
//! tauri.conf.json) only copies the file to a predictable place next to the
//! app exe, it doesn't require any particular API to launch it, and staying
//! on std::process keeps one code path (env vars, log draining, health
//! check, Drop-based cleanup) for both dev and release instead of two.

use std::collections::VecDeque;
use std::io::{BufRead, BufReader};
use std::net::TcpListener;
use std::process::{Child, ChildStderr, ChildStdout, Command, Stdio};
use std::sync::{Arc, Mutex};
use std::time::{Duration, Instant};

#[cfg(windows)]
use std::os::windows::process::CommandExt;

// CREATE_NO_WINDOW (winbase.h). In release builds app.exe itself has no
// console (windows_subsystem = "windows" in main.rs), so spawning a
// console-subsystem child without this flag makes Windows allocate it a
// brand new console window -- a visible flash on every launch. Applied in
// dev too since the frozen sidecar isn't the only thing spawned here and
// there's no reason to special-case it.
#[cfg(windows)]
const CREATE_NO_WINDOW: u32 = 0x08000000;

const LOG_RING_CAPACITY: usize = 200;

pub struct BackendHandle {
    child: Child,
    pub port: u16,
    pub internal_token: String,
    /// Last LOG_RING_CAPACITY lines across stdout+stderr, for surfacing in
    /// error messages. Populated by the drain threads spawned in
    /// spawn_backend -- reading this never blocks on the child process.
    recent_log: Arc<Mutex<VecDeque<String>>>,
}

impl Drop for BackendHandle {
    fn drop(&mut self) {
        // Best-effort: also explicitly killed from the CloseRequested handler
        // in lib.rs so this fires even if the process exits some other way
        // (window closed, panic, etc.) without leaving an orphaned uvicorn
        // process behind -- a real annoyance during dev iteration otherwise.
        //
        // Neither path covers the app being force-killed (Task Manager "End
        // task", `taskkill /F`, a hard crash) -- Windows tears the parent
        // down without ever running Drop or Tauri's RunEvent handlers, so
        // the child keeps running and holding its port. The correct fix is
        // a Windows Job Object with JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
        // (child dies whenever the job's last handle closes, even on a hard
        // kill of the parent), which needs an explicit `windows` crate
        // dependency -- flagged for a decision rather than added here.
        let _ = self.child.kill();
    }
}

impl BackendHandle {
    /// Snapshot of the most recent backend stdout/stderr lines. Previously
    /// captured but never read outside spawn_backend's own error path --
    /// a runtime error after startup was invisible in a packaged (console-
    /// less) release build. See commands::get_backend_logs.
    pub fn recent_log_lines(&self) -> Vec<String> {
        self.recent_log.lock().unwrap().iter().cloned().collect()
    }
}

fn free_port() -> Result<u16, String> {
    let listener = TcpListener::bind("127.0.0.1:0").map_err(|e| e.to_string())?;
    listener.local_addr().map(|a| a.port()).map_err(|e| e.to_string())
    // listener drops here, freeing the port; spawn_backend below binds it
    // again almost immediately, so the race window against something else
    // grabbing it first is small in practice, same tradeoff every "ask the
    // OS for a free port, then hand it to a child process" approach makes.
}

/// (program, args, working_dir) for launching the backend. See the module
/// doc comment for why release mode spawns a bundled sidecar directly
/// instead of going through tauri-plugin-shell.
fn resolve_backend_command(port: u16) -> Result<(String, Vec<String>, std::path::PathBuf), String> {
    if cfg!(debug_assertions) {
        let manifest_dir = std::path::PathBuf::from(env!("CARGO_MANIFEST_DIR"));
        let repo_root = manifest_dir
            .parent()
            .ok_or("could not resolve repo root from CARGO_MANIFEST_DIR")?
            .to_path_buf();
        let python = repo_root.join(".venv-win").join("Scripts").join("python.exe");
        if !python.exists() {
            return Err(format!(
                "dev backend venv not found at {} -- run: python -m venv .venv-win && \
                 .venv-win\\Scripts\\pip install -r requirements.txt",
                python.display()
            ));
        }
        Ok((
            python.to_string_lossy().into_owned(),
            vec![
                "-m".into(), "uvicorn".into(), "api.main:app".into(),
                "--host".into(), "127.0.0.1".into(),
                "--port".into(), port.to_string(),
            ],
            repo_root,
        ))
    } else {
        // Tauri's externalBin bundling drops sidecars next to the app's own
        // exe (installer-relative on every platform, not a "resources"
        // subfolder) -- current_exe()'s parent is exactly that directory,
        // both when installed and when run straight out of target/release/
        // (cargo build --release without going through `tauri build`).
        let exe_dir = std::env::current_exe()
            .map_err(|e| format!("could not resolve current_exe: {e}"))?
            .parent()
            .ok_or("current_exe() has no parent directory")?
            .to_path_buf();
        let sidecar_name = format!(
            "txgraph-backend-{}{}",
            env!("SIDECAR_TARGET_TRIPLE"),
            std::env::consts::EXE_SUFFIX
        );
        let sidecar = exe_dir.join(&sidecar_name);
        if !sidecar.exists() {
            return Err(format!(
                "backend sidecar not found at {} -- run: python scripts/build_backend_sidecar.py \
                 (desktop-app packaging, Part 1) before building a release binary",
                sidecar.display()
            ));
        }
        Ok((
            sidecar.to_string_lossy().into_owned(),
            vec![port.to_string()],
            exe_dir,
        ))
    }
}

/// Continuously drains a child output stream into the shared ring buffer.
/// Without this, uvicorn's per-request access log eventually fills the OS
/// pipe buffer and blocks the child's own write() call -- silently hanging
/// the whole backend under any real, sustained traffic (an ingestion run
/// logging for 20+ minutes is exactly this case). Must run for the entire
/// life of the child, not just during the initial readiness wait.
fn drain_into_ring<R: std::io::Read + Send + 'static>(reader: R, ring: Arc<Mutex<VecDeque<String>>>, prefix: &'static str) {
    let mut lines = BufReader::new(reader).lines();
    std::thread::spawn(move || {
        while let Some(Ok(line)) = lines.next() {
            println!("[backend {prefix}] {line}"); // surfaced in `cargo tauri dev`'s own console for live visibility
            let mut buf = ring.lock().unwrap();
            if buf.len() >= LOG_RING_CAPACITY {
                buf.pop_front();
            }
            buf.push_back(format!("[{prefix}] {line}"));
        }
    });
}

fn wait_for_health(port: u16, timeout: Duration) -> Result<(), String> {
    let client = reqwest::blocking::Client::builder()
        .timeout(Duration::from_millis(500))
        .build()
        .map_err(|e| e.to_string())?;
    let url = format!("http://127.0.0.1:{port}/health");
    let deadline = Instant::now() + timeout;
    let mut last_err = String::from("timed out before first attempt");
    while Instant::now() < deadline {
        match client.get(&url).send() {
            Ok(resp) if resp.status().is_success() => return Ok(()),
            Ok(resp) => last_err = format!("HTTP {}", resp.status()),
            Err(e) => last_err = e.to_string(),
        }
        std::thread::sleep(Duration::from_millis(150));
    }
    Err(format!("backend never became healthy at {url}: {last_err}"))
}

/// Spawns the backend, injecting `api_key` and a fresh per-launch internal
/// token as env vars (never as CLI args -- those are visible to any local
/// process that can list this process's command line). Blocks until
/// /health responds or the timeout elapses.
pub fn spawn_backend(api_key: &str) -> Result<BackendHandle, String> {
    let port = free_port()?;
    let internal_token = uuid::Uuid::new_v4().to_string();
    let (program, args, cwd) = resolve_backend_command(port)?;

    let mut cmd = Command::new(&program);
    cmd.args(&args)
        .current_dir(&cwd)
        .env("TXGRAPH_ETHERSCAN_API_KEY", api_key)
        .env("TXGRAPH_INTERNAL_TOKEN", &internal_token)
        .env("TXGRAPH_API_MODE", std::env::var("TXGRAPH_API_MODE").unwrap_or_else(|_| "mock".into()))
        .stdout(Stdio::piped())
        .stderr(Stdio::piped());
    #[cfg(windows)]
    cmd.creation_flags(CREATE_NO_WINDOW);

    let mut child = cmd.spawn().map_err(|e| format!("failed to spawn backend ({program}): {e}"))?;

    let recent_log: Arc<Mutex<VecDeque<String>>> = Arc::new(Mutex::new(VecDeque::with_capacity(LOG_RING_CAPACITY)));
    let stdout: ChildStdout = child.stdout.take().expect("stdout was piped");
    let stderr: ChildStderr = child.stderr.take().expect("stderr was piped");
    drain_into_ring(stdout, recent_log.clone(), "stdout");
    drain_into_ring(stderr, recent_log.clone(), "stderr");

    if let Err(e) = wait_for_health(port, Duration::from_secs(20)) {
        let log_tail = recent_log.lock().unwrap().iter().cloned().collect::<Vec<_>>().join("\n");
        let _ = child.kill();
        return Err(format!("{e}\n--- recent backend output ---\n{log_tail}"));
    }

    Ok(BackendHandle { child, port, internal_token, recent_log })
}

pub fn shutdown(handle: &mut BackendHandle) {
    let _ = handle.child.kill();
    let _ = handle.child.wait();
}
