mod backend;
mod commands;
mod keychain;

use commands::AppState;
use std::sync::Mutex;
use tauri::Manager;

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
            let handle = backend::spawn_backend(&saved_key)
                .map_err(|e| format!("backend failed to start: {e}"))?;
            log::info!("backend ready on port {}", handle.port);
            app.manage(AppState(Mutex::new(handle)));

            Ok(())
        })
        .invoke_handler(tauri::generate_handler![
            commands::get_api_base,
            commands::has_api_key,
            commands::set_api_key,
            commands::clear_api_key,
            commands::test_api_key,
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
