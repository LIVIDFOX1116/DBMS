//! Google sign-in for the desktop shell: the loopback redirect flow.
//!
//! The browser dashboard can redirect to Google in place, but a packaged
//! desktop app cannot -- Google refuses an OAuth consent screen loaded in an
//! embedded webview ("disallowed_useragent"), precisely because an embedded
//! webview can read what the user types into it. So the desktop flow opens
//! the user's real browser and catches the redirect on a loopback port that
//! only this process is listening on.
//!
//! PKCE values are not generated here: `GET /auth/login` on the Python side
//! already produces them, and having one implementation of that is worth
//! more than saving a round trip. This module gets the challenge, hands back
//! the ID token, and never sees a client secret -- a desktop app is a public
//! client and cannot keep one.

use std::io::{BufRead, BufReader, Write};
use std::net::TcpListener;
use std::time::Duration;

/// What the browser hit our loopback listener with.
#[derive(Debug, PartialEq)]
pub enum RedirectResult {
    Code(String),
    Error(String),
}

/// Pulls `code` (or `error`) out of an HTTP request line such as
/// `GET /?code=abc&scope=... HTTP/1.1`. Returns None for anything that is
/// not the redirect we are waiting for -- browsers also request
/// `/favicon.ico` against this listener, and treating that as a failed
/// sign-in would abort the flow the moment the tab renders.
pub fn parse_redirect(request_line: &str) -> Option<RedirectResult> {
    let target = request_line.split_whitespace().nth(1)?;
    let query = target.split_once('?')?.1;
    let mut error = None;
    for pair in query.split('&') {
        let (key, value) = pair.split_once('=')?;
        match key {
            "code" => return Some(RedirectResult::Code(percent_decode(value))),
            "error" => error = Some(percent_decode(value)),
            _ => {}
        }
    }
    error.map(RedirectResult::Error)
}

/// Minimal percent-decoding: authorization codes are URL-safe but the
/// `error` and `scope` values Google returns are not always.
fn percent_decode(raw: &str) -> String {
    let bytes = raw.as_bytes();
    let mut out = Vec::with_capacity(bytes.len());
    let mut i = 0;
    while i < bytes.len() {
        match bytes[i] {
            b'%' if i + 2 < bytes.len() => {
                match u8::from_str_radix(&raw[i + 1..i + 3], 16) {
                    Ok(byte) => {
                        out.push(byte);
                        i += 3;
                    }
                    Err(_) => {
                        out.push(b'%');
                        i += 1;
                    }
                }
            }
            b'+' => {
                out.push(b' ');
                i += 1;
            }
            byte => {
                out.push(byte);
                i += 1;
            }
        }
    }
    String::from_utf8_lossy(&out).into_owned()
}

/// Builds Google's authorization URL. `redirect_uri` must be the loopback
/// address we are actually listening on, because Google checks it against
/// the code at exchange time.
pub fn authorize_url(endpoint: &str, client_id: &str, redirect_uri: &str, scope: &str, challenge: &str) -> String {
    format!(
        "{endpoint}?client_id={}&redirect_uri={}&response_type=code&scope={}&code_challenge={}&code_challenge_method=S256",
        urlencode(client_id),
        urlencode(redirect_uri),
        urlencode(scope),
        urlencode(challenge),
    )
}

fn urlencode(raw: &str) -> String {
    let mut out = String::with_capacity(raw.len());
    for byte in raw.bytes() {
        match byte {
            b'A'..=b'Z' | b'a'..=b'z' | b'0'..=b'9' | b'-' | b'_' | b'.' | b'~' => out.push(byte as char),
            _ => out.push_str(&format!("%{byte:02X}")),
        }
    }
    out
}

const DONE_PAGE: &str = "HTTP/1.1 200 OK\r\nContent-Type: text/html\r\nConnection: close\r\n\r\n\
<!doctype html><meta charset=utf-8><title>FrameCheck</title>\
<body style=\"font-family:system-ui;background:#0B1A19;color:#E6F2F0;display:grid;place-items:center;height:100vh;margin:0\">\
<div style=\"text-align:center\"><h1 style=\"font-size:18px\">Signed in to FrameCheck</h1>\
<p style=\"color:#9ab\">You can close this tab and go back to the app.</p></div>";

/// Binds a loopback listener on an OS-chosen port and returns it with the
/// redirect URI to register with Google. Port 0 rather than a fixed port:
/// a hardcoded one could already be taken, and on a shared machine another
/// process could be squatting on it waiting to receive someone's code.
pub fn bind_loopback() -> std::io::Result<(TcpListener, String)> {
    let listener = TcpListener::bind("127.0.0.1:0")?;
    let port = listener.local_addr()?.port();
    Ok((listener, format!("http://127.0.0.1:{port}")))
}

/// Waits for the one redirect request, answers it with a "you can close
/// this" page, and returns what it carried. `timeout` stops the app hanging
/// forever if the user abandons the consent screen.
pub fn wait_for_redirect(listener: &TcpListener, timeout: Duration) -> Result<RedirectResult, String> {
    listener
        .set_nonblocking(false)
        .map_err(|e| format!("could not configure the sign-in listener: {e}"))?;
    let deadline = std::time::Instant::now() + timeout;
    for stream in listener.incoming() {
        if std::time::Instant::now() > deadline {
            return Err("sign-in timed out".into());
        }
        let mut stream = match stream {
            Ok(s) => s,
            Err(e) => return Err(format!("sign-in listener failed: {e}")),
        };
        let _ = stream.set_read_timeout(Some(Duration::from_secs(10)));
        let mut line = String::new();
        if BufReader::new(&stream).read_line(&mut line).is_err() {
            continue;
        }
        match parse_redirect(&line) {
            Some(result) => {
                let _ = stream.write_all(DONE_PAGE.as_bytes());
                let _ = stream.flush();
                return Ok(result);
            }
            None => {
                // Not the redirect (favicon, preflight probe): answer and
                // keep waiting rather than failing the sign-in.
                let _ = stream.write_all(b"HTTP/1.1 404 Not Found\r\nConnection: close\r\n\r\n");
            }
        }
    }
    Err("sign-in listener closed before the redirect arrived".into())
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn extracts_the_authorization_code() {
        let line = "GET /?code=4%2F0AX4&scope=openid+email HTTP/1.1";
        assert_eq!(parse_redirect(line), Some(RedirectResult::Code("4/0AX4".into())));
    }

    #[test]
    fn extracts_a_denial() {
        let line = "GET /?error=access_denied HTTP/1.1";
        assert_eq!(parse_redirect(line), Some(RedirectResult::Error("access_denied".into())));
    }

    #[test]
    fn ignores_requests_that_are_not_the_redirect() {
        // The browser asks for this as soon as the tab renders; treating it
        // as a failed sign-in would abort the flow every time.
        assert_eq!(parse_redirect("GET /favicon.ico HTTP/1.1"), None);
        assert_eq!(parse_redirect("GET / HTTP/1.1"), None);
        assert_eq!(parse_redirect(""), None);
    }

    #[test]
    fn code_wins_over_other_parameters_regardless_of_order() {
        let line = "GET /?scope=openid&code=abc&authuser=0 HTTP/1.1";
        assert_eq!(parse_redirect(line), Some(RedirectResult::Code("abc".into())));
    }

    #[test]
    fn authorize_url_escapes_the_redirect_and_sets_s256() {
        let url = authorize_url(
            "https://accounts.google.com/o/oauth2/v2/auth",
            "client-123.apps.googleusercontent.com",
            "http://127.0.0.1:54321",
            "openid email profile",
            "chal-abc",
        );
        assert!(url.contains("redirect_uri=http%3A%2F%2F127.0.0.1%3A54321"), "{url}");
        assert!(url.contains("scope=openid%20email%20profile"), "{url}");
        assert!(url.contains("code_challenge_method=S256"), "{url}");
        assert!(url.contains("response_type=code"), "{url}");
        // A desktop app is a public client: no secret may ever appear here.
        assert!(!url.contains("client_secret"), "{url}");
    }

    #[test]
    fn loopback_binds_an_ephemeral_port_on_localhost_only() {
        let (listener, redirect) = bind_loopback().expect("bind");
        let addr = listener.local_addr().unwrap();
        assert!(addr.ip().is_loopback());
        assert_ne!(addr.port(), 0);
        assert_eq!(redirect, format!("http://127.0.0.1:{}", addr.port()));
    }

    #[test]
    fn two_sign_ins_do_not_share_a_port() {
        let (_a, first) = bind_loopback().unwrap();
        let (_b, second) = bind_loopback().unwrap();
        assert_ne!(first, second);
    }

    #[test]
    fn percent_decoding_leaves_a_lone_percent_alone() {
        assert_eq!(percent_decode("100%"), "100%");
        assert_eq!(percent_decode("a+b"), "a b");
    }
}
