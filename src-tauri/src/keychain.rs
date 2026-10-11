//! The only storage for the two secrets the app holds: the Etherscan API key
//! and the Postgres password. Never a config file, never localStorage, never
//! sent to the webview -- see api/main.py's comment on _runtime_state for the
//! rest of the handoff story.

const SERVICE: &str = "com.framecheck.app";
const ACCOUNT: &str = "etherscan_api_key";
/// The database connection settings, as JSON. The non-secret fields could
/// live in a config file, but splitting them from the password would mean two
/// stores to keep in step for no gain -- the keychain holds the lot.
const DB_ACCOUNT: &str = "postgres_connection";

fn entry() -> Result<keyring::Entry, String> {
    keyring::Entry::new(SERVICE, ACCOUNT).map_err(|e| e.to_string())
}

fn db_entry() -> Result<keyring::Entry, String> {
    keyring::Entry::new(SERVICE, DB_ACCOUNT).map_err(|e| e.to_string())
}

/// The saved connection settings as the JSON string they were stored as, or
/// None if the user has never saved any (in which case the backend keeps
/// using its environment, exactly as before this existed).
pub fn get_db_settings() -> Option<String> {
    db_entry().ok()?.get_password().ok()
}

pub fn set_db_settings(json: &str) -> Result<(), String> {
    db_entry()?.set_password(json).map_err(|e| e.to_string())
}

/// Returns the stored key, or None if nothing's been saved yet. Callers in
/// this crate use this to hand the key to the backend child at spawn/swap
/// time -- never to display it back in the UI (see commands::has_api_key,
/// which intentionally returns only a bool).
pub fn get_key() -> Option<String> {
    entry().ok()?.get_password().ok()
}

pub fn set_key(key: &str) -> Result<(), String> {
    entry()?.set_password(key).map_err(|e| e.to_string())
}

pub fn clear_key() -> Result<(), String> {
    let e = entry()?;
    match e.delete_credential() {
        Ok(()) => Ok(()),
        // Already absent is not an error -- Clear on a never-saved key is a no-op.
        Err(keyring::Error::NoEntry) => Ok(()),
        Err(err) => Err(err.to_string()),
    }
}
