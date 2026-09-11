//! Tauri commands invoked from the webview via window.__TAURI__.core.invoke
//! (withGlobalTauri, no @tauri-apps/api npm package -- this repo has no
//! Node/npm anywhere, deliberately, see README).

use crate::backend::BackendHandle;
use crate::keychain;
use serde::Serialize;
use std::sync::Mutex;

pub struct AppState(pub Mutex<BackendHandle>);

#[tauri::command]
pub fn get_api_base(state: tauri::State<AppState>) -> String {
    let handle = state.0.lock().unwrap();
    format!("http://127.0.0.1:{}", handle.port)
}

/// Recent backend stdout/stderr lines (oldest first), for a future "view
/// logs" panel -- a runtime backend error is otherwise invisible once the
/// app is packaged (no console in a release build).
#[tauri::command]
pub fn get_backend_logs(state: tauri::State<AppState>) -> Vec<String> {
    state.0.lock().unwrap().recent_log_lines()
}

/// Deliberately a bool, not the key -- Settings never reads the stored value
/// back out to render it, per the "write-only" requirement. The UI shows
/// "•••• saved" from this alone.
#[tauri::command]
pub fn has_api_key() -> bool {
    keychain::get_key().map(|k| !k.trim().is_empty()).unwrap_or(false)
}

fn hot_swap(state: &tauri::State<AppState>, new_key: &str) -> Result<(), String> {
    let (port, token) = {
        let handle = state.0.lock().unwrap();
        (handle.port, handle.internal_token.clone())
    };
    let client = reqwest::blocking::Client::new();
    let resp = client
        .post(format!("http://127.0.0.1:{port}/internal/api-key"))
        .header("X-Internal-Token", token)
        .json(&serde_json::json!({ "api_key": new_key }))
        .send()
        .map_err(|e| format!("could not reach backend to apply the new key: {e}"))?;
    if !resp.status().is_success() {
        return Err(format!("backend rejected the key update: HTTP {}", resp.status()));
    }
    Ok(())
}

#[tauri::command]
pub fn set_api_key(new_key: String, state: tauri::State<AppState>) -> Result<(), String> {
    let new_key = new_key.trim().to_string();
    if new_key.is_empty() {
        return Err("key cannot be empty -- use clear_api_key to remove it".into());
    }
    keychain::set_key(&new_key)?;
    hot_swap(&state, &new_key)
}

#[tauri::command]
pub fn clear_api_key(state: tauri::State<AppState>) -> Result<(), String> {
    keychain::clear_key()?;
    hot_swap(&state, "")
}

#[derive(Serialize)]
pub struct TestResult {
    pub ok: bool,
    pub message: String,
}

/// Rust-owned, one cheap real call, no backend involvement -- works even
/// during first-run onboarding while the FastAPI child may still be
/// booting. Pass `candidate_key` to test an unsaved value while the user is
/// typing; omit it to test whatever's currently in the keychain.
#[tauri::command]
pub fn test_api_key(candidate_key: Option<String>) -> Result<TestResult, String> {
    let key = match candidate_key.map(|k| k.trim().to_string()).filter(|k| !k.is_empty()) {
        Some(k) => k,
        None => keychain::get_key()
            .filter(|k| !k.trim().is_empty())
            .ok_or_else(|| "no key saved yet, and none provided to test".to_string())?,
    };

    let client = reqwest::blocking::Client::builder()
        .timeout(std::time::Duration::from_secs(8))
        .build()
        .map_err(|e| e.to_string())?;

    let resp = match client
        .get("https://api.etherscan.io/v2/api")
        .query(&[("chainid", "1"), ("module", "stats"), ("action", "ethsupply"), ("apikey", &key)])
        .send()
    {
        Ok(r) => r,
        Err(e) if e.is_timeout() => {
            return Ok(TestResult { ok: false, message: "No network — the request timed out.".into() })
        }
        Err(e) if e.is_connect() => {
            return Ok(TestResult { ok: false, message: "No network — could not reach Etherscan.".into() })
        }
        Err(e) => return Ok(TestResult { ok: false, message: format!("Request failed: {e}") }),
    };

    let status = resp.status();
    let body: serde_json::Value = resp.json().unwrap_or_else(|_| serde_json::json!({}));
    let es_status = body.get("status").and_then(|v| v.as_str()).unwrap_or("");
    let es_message = body.get("message").and_then(|v| v.as_str()).unwrap_or("").to_string();

    if status.as_u16() == 429 || es_message.to_lowercase().contains("rate limit") {
        return Ok(TestResult { ok: false, message: "Rate limited by Etherscan — try again in a moment.".into() });
    }
    if es_status == "1" {
        return Ok(TestResult { ok: true, message: "Connected — key is valid.".into() });
    }
    if es_message.to_lowercase().contains("invalid api key") {
        return Ok(TestResult { ok: false, message: "Invalid API key.".into() });
    }
    Ok(TestResult { ok: false, message: format!("Unexpected response from Etherscan: {es_message}") })
}
