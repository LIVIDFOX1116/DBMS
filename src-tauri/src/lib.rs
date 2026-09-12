mod backend;
mod commands;
mod gpu;
mod keychain;

use commands::AppState;
use std::sync::Mutex;
use tauri::Manager;

#[cfg(windows)]
const CREATE_NO_WINDOW: u32 = 0x08000000;

/// A backend-launch failure previously surfaced as `.expect(...)` panicking
/// inside `.build()` below -- in a release build (windows_subsystem =
/// "windows", no console) that's completely silent: the app just never
/// shows a window, with zero indication why. Writes the reason to a log
/// file (survives even if the dialog fails) and, on Windows, shows a native
/// message box so a real end user isn't left staring at nothing.
fn report_fatal_startup_error(message: &str) {
    let log_path = std::env::temp_dir().join("txgraph-desktop-startup-error.log");
    let _ = std::fs::write(&log_path, format!("TxGraph Analyzer failed to start:\n{message}\n"));

    #[cfg(windows)]
    {
        use std::os::windows::process::CommandExt;
        let escaped = message.replace('`', "``").replace('"', "`\"");
        let script = format!(
            "Add-Type -AssemblyName System.Windows.Forms; \
             [System.Windows.Forms.MessageBox]::Show(\"{escaped}`n`nDetails written to: {}\", \
             'TxGraph Analyzer failed to start', 'OK', 'Error') | Out-Null",
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
            app.manage(AppState(Mutex::new(handle)));

            Ok(())
        })
        .invoke_handler(tauri::generate_handler![
            commands::get_api_base,
            commands::get_backend_logs,
            commands::has_api_key,
            commands::set_api_key,
            commands::clear_api_key,
            commands::test_api_key,
            commands::detect_gpus,
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
