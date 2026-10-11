mod backend;
mod commands;
// pub so `examples/print_gpu_detection.rs` can call gpu::detect() directly
// for end-to-end hardware verification without needing the desktop UI.
pub mod gpu;
mod keychain;
// pub so `examples/bench_real_graph.rs` (a real-graph counterpart to
// gpu-bench, run manually against a live backend) can call it directly
// instead of duplicating the settle/benchmark logic a second time.
pub mod layout;
// pub so the loopback redirect parsing is testable without standing up a
// browser and a Google consent screen.
pub mod oauth;

use commands::AppState;
use std::sync::Mutex;
use tauri::Manager;

#[cfg(windows)]
const CREATE_NO_WINDOW: u32 = 0x08000000;

/// Forces the title bar to this app's own dark palette instead of the user's
/// Windows accent color. tauri.conf.json's "theme": "Dark" only controls
/// WebView2/window-chrome light-vs-dark rendering (e.g. white-on-dark title
/// text) -- it does NOT stop Windows from tinting the ACTIVE window's title
/// bar with the user's accent color when "Show accent color on title bars
/// and window borders" is on (Settings > Personalization > Colors), which is
/// a separate DWM composition behavior the app theme setting never touches.
/// Confirmed live: with a non-default accent color, the focused window's
/// title bar rendered in that accent color (e.g. bright purple) against this
/// app's dark teal palette -- the exact clash "theme": "Dark" was meant to
/// prevent, still reproducing after that fix. DWMWA_CAPTION_COLOR/
/// DWMWA_TEXT_COLOR (Windows 11 22000+) force fixed colors independent of
/// the user's setting; DWMWA_USE_IMMERSIVE_DARK_MODE is set too for older
/// Windows 10 builds that lack the caption-color attributes but still
/// respect dark-mode chrome. All three are best-effort: colors are set via
/// raw DwmSetWindowAttribute calls that fail harmlessly (swallowed, not
/// propagated) on any Windows version too old to support a given attribute
/// -- there is no visual regression from calling this on such a system, it
/// just leaves the (non-accent, since older Windows also lacks the
/// accent-on-titlebar feature this exists to counteract) default title bar.
#[cfg(windows)]
fn apply_dark_titlebar(hwnd: windows::Win32::Foundation::HWND) {
    use windows::Win32::Graphics::Dwm::{
        DwmSetWindowAttribute, DWMWA_CAPTION_COLOR, DWMWA_TEXT_COLOR, DWMWA_USE_IMMERSIVE_DARK_MODE,
    };
    unsafe {
        let dark_mode: i32 = 1;
        let _ = DwmSetWindowAttribute(
            hwnd,
            DWMWA_USE_IMMERSIVE_DARK_MODE,
            &dark_mode as *const _ as *const std::ffi::c_void,
            std::mem::size_of::<i32>() as u32,
        );
        // COLORREF is 0x00BBGGRR, not RGB order. Matches dashboard.html's
        // --bg (#0D1614) and --text (#EDF6F4) CSS variables exactly, so the
        // native title bar reads as part of the same surface as the content
        // below it rather than a visually distinct strip.
        let caption_color: u32 = 0x0014160D; // --bg: #0D1614
        let _ = DwmSetWindowAttribute(
            hwnd,
            DWMWA_CAPTION_COLOR,
            &caption_color as *const _ as *const std::ffi::c_void,
            std::mem::size_of::<u32>() as u32,
        );
        let text_color: u32 = 0x00F4F6ED; // --text: #EDF6F4
        let _ = DwmSetWindowAttribute(
            hwnd,
            DWMWA_TEXT_COLOR,
            &text_color as *const _ as *const std::ffi::c_void,
            std::mem::size_of::<u32>() as u32,
        );
    }
}

/// A backend-launch failure previously surfaced as `.expect(...)` panicking
/// inside `.build()` below -- in a release build (windows_subsystem =
/// "windows", no console) that's completely silent: the app just never
/// shows a window, with zero indication why. Writes the reason to a log
/// file (survives even if the dialog fails) and, on Windows, shows a native
/// message box so a real end user isn't left staring at nothing.
fn report_fatal_startup_error(message: &str) {
    let log_path = std::env::temp_dir().join("framecheck-desktop-startup-error.log");
    let _ = std::fs::write(&log_path, format!("FrameCheck failed to start:\n{message}\n"));

    #[cfg(windows)]
    {
        use std::os::windows::process::CommandExt;
        let escaped = message.replace('`', "``").replace('"', "`\"");
        let script = format!(
            "Add-Type -AssemblyName System.Windows.Forms; \
             [System.Windows.Forms.MessageBox]::Show(\"{escaped}`n`nDetails written to: {}\", \
             'FrameCheck failed to start', 'OK', 'Error') | Out-Null",
            log_path.display()
        );
        let _ = std::process::Command::new("powershell")
            .args(["-NoProfile", "-NonInteractive", "-Command", &script])
            .creation_flags(CREATE_NO_WINDOW)
            .status();
    }
}

#[cfg_attr(mobile, tauri::mobile_entry_point)]
pub fn run() {
    tauri::Builder::default()
        .setup(|app| {
            if cfg!(debug_assertions) {
                app.handle().plugin(
                    tauri_plugin_log::Builder::default()
                        .level(log::LevelFilter::Info)
                        .build(),
                )?;
            }

            // Whatever's already saved in the OS keychain from a previous
            // run -- empty string (mock-mode-only, no live lookups) if
            // nothing's been set yet. See api/main.py's _runtime_state
            // comment for why this never touches .env.
            let saved_key = keychain::get_key().unwrap_or_default();
            let handle = match backend::spawn_backend(&saved_key) {
                Ok(h) => h,
                Err(e) => {
                    let msg = format!("backend failed to start: {e}");
                    log::error!("{msg}");
                    report_fatal_startup_error(&msg);
                    return Err(msg.into());
                }
            };
            log::info!("backend ready on port {}", handle.port);
            let port = handle.port;
            let internal_token = handle.internal_token.clone();
            app.manage(AppState(Mutex::new(handle)));

            // Connection settings saved on a previous run, handed over the
            // same loopback + internal-token route the Database view uses.
            // Done after the backend is healthy rather than as a spawn env
            // var, so the password never appears in the child's environment
            // (readable by any process that can see it) -- only in the
            // keychain and in one POST to 127.0.0.1.
            if let Some(json) = keychain::get_db_settings() {
                match serde_json::from_str::<serde_json::Value>(&json) {
                    Ok(settings) => {
                        let client = reqwest::blocking::Client::new();
                        let sent = client
                            .post(format!("http://127.0.0.1:{port}/internal/db-settings"))
                            .header("X-Internal-Token", internal_token)
                            .json(&settings)
                            .send();
                        match sent {
                            Ok(resp) if resp.status().is_success() =>
                                log::info!("applied saved database connection settings"),
                            // Not fatal: the app still starts, serves demo
                            // data, and the Database view says the
                            // connection is unset so the user can fix it.
                            Ok(resp) => log::warn!("backend rejected saved database settings: HTTP {}", resp.status()),
                            Err(e) => log::warn!("could not apply saved database settings: {e}"),
                        }
                    }
                    Err(e) => log::warn!("saved database settings are not valid JSON, ignoring: {e}"),
                }
            }

            #[cfg(windows)]
            if let Some(window) = app.get_webview_window("main") {
                if let Ok(hwnd) = window.hwnd() {
                    apply_dark_titlebar(hwnd);
                }
            }

            Ok(())
        })
        .invoke_handler(tauri::generate_handler![
            commands::get_api_base,
            commands::get_backend_logs,
            commands::has_api_key,
            commands::set_api_key,
            commands::clear_api_key,
            commands::test_api_key,
            commands::set_db_settings,
            commands::has_db_settings,
            commands::detect_gpus,
            commands::run_layout,
            commands::benchmark_layout,
            commands::google_signin,
            commands::open_multiview,
        ])
        .build(tauri::generate_context!())
        .expect("error while building tauri application")
        .run(|app_handle, event| {
            // Covers window close, Alt+F4, taskbar close, and OS shutdown --
            // not just a single window's CloseRequested, so the backend
            // child is never left orphaned regardless of how the app exits.
            if let tauri::RunEvent::ExitRequested { .. } | tauri::RunEvent::Exit = event {
                if let Some(state) = app_handle.try_state::<AppState>() {
                    let mut handle = state.0.lock().unwrap();
                    backend::shutdown(&mut handle);
                }
            }
        });
}
