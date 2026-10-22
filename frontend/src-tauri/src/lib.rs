use std::{
    collections::HashMap,
    io::{Error as IoError, ErrorKind, Read, Write},
    net::{SocketAddr, TcpListener, TcpStream},
    path::{Path, PathBuf},
    process::{Child, Command, Stdio},
    sync::Mutex,
    thread,
    time::{Duration, Instant},
};

use chrono::Utc;
use dpi::{LogicalPosition, LogicalSize};
use serde_json::{json, Value};
use tauri::{ActivationPolicy, AppHandle, Emitter, Manager, RunEvent, WindowEvent};
use tauri_plugin_autostart::ManagerExt;
use tauri_plugin_global_shortcut::ShortcutState;

#[cfg(target_os = "macos")]
use objc2::MainThreadMarker;
#[cfg(target_os = "macos")]
use objc2_app_kit::{NSApplication, NSStatusWindowLevel, NSWindow, NSWindowCollectionBehavior};

const POPOVER_WIDTH: f64 = 680.0;
const POPOVER_HEIGHT: f64 = 700.0;
const POPOVER_COMPACT_HEIGHT: f64 = 92.0;
const POPOVER_SCREEN_PADDING: f64 = 12.0;
const POPOVER_MENU_BAR_GAP: f64 = 8.0;
const BACKEND_HOST: &str = "127.0.0.1";

// Build-time defaults injected by build-release.sh from repo `.env`.
// These are used only to bootstrap a first-run `~/.recurdo/.env` for beta builds.
const BUNDLED_SUPABASE_URL: Option<&str> = option_env!("SUPABASE_URL");
const BUNDLED_SUPABASE_ANON_KEY: Option<&str> = option_env!("SUPABASE_ANON_KEY");
const BUNDLED_GEMINI_API_KEY: Option<&str> = option_env!("GEMINI_API_KEY");
const BUNDLED_ENRICHMENT_MODEL: Option<&str> = option_env!("ENRICHMENT_MODEL");

struct BackendRuntime {
    process: Mutex<Option<Child>>,
    port: Mutex<Option<u16>>,
    pending_auth_state: Mutex<Option<String>>,
}

impl Default for BackendRuntime {
    fn default() -> Self {
        Self {
            process: Mutex::new(None),
            port: Mutex::new(None),
            pending_auth_state: Mutex::new(None),
        }
    }
}

fn backend_addr(port: u16) -> SocketAddr {
    format!("{BACKEND_HOST}:{port}")
        .parse()
        .expect("valid backend address")
}

fn preferred_backend_port() -> Option<u16> {
    std::env::var("RECURDO_PORT")
        .ok()
        .and_then(|raw| raw.trim().parse::<u16>().ok())
}

fn can_bind_backend_port(port: u16) -> bool {
    TcpListener::bind((BACKEND_HOST, port)).is_ok()
}

fn choose_backend_port() -> Result<u16, String> {
    if let Some(port) = preferred_backend_port() {
        if can_bind_backend_port(port) {
            return Ok(port);
        }
    }

    let listener = TcpListener::bind((BACKEND_HOST, 0)).map_err(|e| e.to_string())?;
    let port = listener.local_addr().map_err(|e| e.to_string())?.port();
    drop(listener);
    Ok(port)
}

fn current_backend_port(app: &AppHandle) -> Result<u16, String> {
    let state = app.state::<BackendRuntime>();
    let mut guard = state.port.lock().expect("backend port lock poisoned");
    if let Some(port) = *guard {
        return Ok(port);
    }

    let port = choose_backend_port()?;
    *guard = Some(port);
    Ok(port)
}

fn backend_is_ready(port: u16) -> bool {
    let mut stream = match TcpStream::connect_timeout(&backend_addr(port), Duration::from_millis(250)) {
        Ok(stream) => stream,
        Err(_) => return false,
    };

    let _ = stream.set_read_timeout(Some(Duration::from_millis(400)));
    let _ = stream.set_write_timeout(Some(Duration::from_millis(400)));

    let request = format!(
        "GET /ready HTTP/1.1\r\nHost: {BACKEND_HOST}:{port}\r\nConnection: close\r\n\r\n"
    );
    if stream.write_all(request.as_bytes()).is_err() {
        return false;
    }

    let mut response = String::new();
    if stream.read_to_string(&mut response).is_err() {
        return false;
    }

    response.contains("\"status\":\"ok\"")
}

fn wait_for_backend(port: u16, timeout: Duration) -> bool {
    let start = Instant::now();
    while start.elapsed() < timeout {
        if backend_is_ready(port) {
            return true;
        }
        thread::sleep(Duration::from_millis(250));
    }
    backend_is_ready(port)
}

fn project_root() -> Result<PathBuf, IoError> {
    let manifest_dir = PathBuf::from(env!("CARGO_MANIFEST_DIR"));
    manifest_dir
        .parent()
        .and_then(Path::parent)
        .map(Path::to_path_buf)
        .ok_or_else(|| IoError::new(ErrorKind::NotFound, "Unable to locate project root"))
}

fn dev_python_path(root: &Path) -> PathBuf {
    root.join("venv311").join("bin").join("python")
}

fn home_recurdo_dir() -> PathBuf {
    dirs::home_dir()
        .unwrap_or_else(|| PathBuf::from("."))
        .join(".recurdo")
}

fn resolved_data_dir() -> PathBuf {
    std::env::var_os("RECURDO_DATA_DIR")
        .map(PathBuf::from)
        .unwrap_or_else(home_recurdo_dir)
}

fn resolved_session_path() -> PathBuf {
    resolved_data_dir().join("session.json")
}

fn store_backend(app: &AppHandle, backend: Child) {
    let state = app.state::<BackendRuntime>();
    let mut guard = state.process.lock().expect("backend state lock poisoned");
    *guard = Some(backend);
}

fn clear_backend_port(app: &AppHandle) {
    let state = app.state::<BackendRuntime>();
    let mut guard = state.port.lock().expect("backend port lock poisoned");
    *guard = None;
}

fn tracked_backend_is_running(app: &AppHandle) -> bool {
    let state = app.state::<BackendRuntime>();
    let mut guard = state.process.lock().expect("backend state lock poisoned");
    let Some(child) = guard.as_mut() else {
        return false;
    };

    match child.try_wait() {
        Ok(None) => true,
        Ok(Some(_)) | Err(_) => {
            *guard = None;
            false
        }
    }
}

fn launch_dev_backend(port: u16) -> Result<Child, Box<dyn std::error::Error>> {
    let root = project_root()?;
    let python = dev_python_path(&root);
    let data_dir = resolved_data_dir();
    std::fs::create_dir_all(&data_dir)?;
    if !python.exists() {
        return Err(Box::new(IoError::new(
            ErrorKind::NotFound,
            format!("Missing Python interpreter at {}", python.display()),
        )));
    }

    let child = Command::new(python)
        .arg("-m")
        .arg("uvicorn")
        .arg("backend.main:app")
        .arg("--host")
        .arg(BACKEND_HOST)
        .arg("--port")
        .arg(port.to_string())
        .current_dir(root)
        .env("RECURDO_DATA_DIR", &data_dir)
        .env("RECURDO_ENV_FILE", data_dir.join(".env"))
        .env("RECURDO_PORT", port.to_string())
        .stdin(Stdio::null())
        .stdout(Stdio::inherit())
        .stderr(Stdio::inherit())
        .spawn()?;

    Ok(child)
}

fn bundled_backend_path(app: &AppHandle) -> Result<PathBuf, String> {
    let resource_dir = app.path().resource_dir().ok();
    let executable_dir = std::env::current_exe()
        .map_err(|e| e.to_string())?
        .parent()
        .map(Path::to_path_buf)
        .ok_or_else(|| "missing executable directory".to_string())?;

    let mut candidates: Vec<PathBuf> = Vec::new();
    if let Some(path) = resource_dir.as_ref() {
        candidates.push(
            path.join("backend")
                .join("recurdo-backend")
                .join("recurdo-backend"),
        );
        candidates.push(path.join("recurdo-backend"));
        if let Some(parent) = path.parent() {
            candidates.push(
                parent
                    .join("Resources")
                    .join("backend")
                    .join("recurdo-backend")
                    .join("recurdo-backend"),
            );
            candidates.push(parent.join("MacOS").join("recurdo-backend"));
        }
    }
    candidates.push(
        executable_dir
            .join("..")
            .join("Resources")
            .join("backend")
            .join("recurdo-backend")
            .join("recurdo-backend"),
    );
    candidates.push(executable_dir.join("recurdo-backend"));
    if let Some(parent) = executable_dir.parent() {
        candidates.push(
            parent
                .join("Resources")
                .join("Contents")
                .join("MacOS")
                .join("recurdo-backend"),
        );
    }

    candidates
        .iter()
        .find(|path| path.exists())
        .cloned()
        .ok_or_else(|| {
            format!(
                "Backend binary not found in bundle. Looked in: {}",
                candidates
                    .iter()
                    .map(|path| path.display().to_string())
                    .collect::<Vec<_>>()
                    .join(", ")
            )
        })
}

fn open_backend_log_stdio(path: &Path) -> Result<Stdio, String> {
    let file = std::fs::OpenOptions::new()
        .create(true)
        .append(true)
        .open(path)
        .map_err(|e| format!("Unable to open backend log {}: {}", path.display(), e))?;
    Ok(Stdio::from(file))
}

fn bootstrap_user_env_file(data_dir: &Path) -> Result<(), String> {
    let env_path = data_dir.join(".env");
    if env_path.exists() {
        return Ok(());
    }

    let mut lines: Vec<String> = Vec::new();
    if let Some(url) = BUNDLED_SUPABASE_URL {
        let trimmed = url.trim();
        if !trimmed.is_empty() {
            lines.push(format!("SUPABASE_URL={trimmed}"));
        }
    }
    if let Some(key) = BUNDLED_SUPABASE_ANON_KEY {
        let trimmed = key.trim();
        if !trimmed.is_empty() {
            lines.push(format!("SUPABASE_ANON_KEY={trimmed}"));
        }
    }
    if let Some(model) = BUNDLED_ENRICHMENT_MODEL {
        let trimmed = model.trim();
        if !trimmed.is_empty() {
            lines.push(format!("ENRICHMENT_MODEL={trimmed}"));
        }
    }
    if let Some(key) = BUNDLED_GEMINI_API_KEY {
        let trimmed = key.trim();
        if !trimmed.is_empty() {
            lines.push(format!("GEMINI_API_KEY={trimmed}"));
        }
    }

    // If the build didn't include any defaults, don't create an empty file.
    if lines.is_empty() {
        return Ok(());
    }

    let content = format!("{}\n", lines.join("\n"));
    std::fs::write(&env_path, content).map_err(|e| format!("Unable to write env file: {e}"))?;
    Ok(())
}

fn start_backend(app: &AppHandle, port: u16) -> Result<Child, String> {
    let binary_path = bundled_backend_path(app)?;
    let launch_dir = binary_path
        .parent()
        .map(Path::to_path_buf)
        .unwrap_or_else(resolved_data_dir);
    let data_dir = resolved_data_dir();
    let log_dir = data_dir.join("logs");
    let stdout_log = log_dir.join("backend.stdout.log");
    let stderr_log = log_dir.join("backend.stderr.log");
    let session_path = resolved_session_path();
    std::fs::create_dir_all(&data_dir).map_err(|e| e.to_string())?;
    std::fs::create_dir_all(&log_dir).map_err(|e| e.to_string())?;
    bootstrap_user_env_file(&data_dir)?;

    println!("[Recurdo] data_dir:    {:?}", data_dir);
    println!("[Recurdo] launch_dir:  {:?}", launch_dir);
    println!("[Recurdo] backend_bin: {:?}", binary_path);
    println!("[Recurdo] session:     {:?}", session_path);
    println!("[Recurdo] stdout_log:  {:?}", stdout_log);
    println!("[Recurdo] stderr_log:  {:?}", stderr_log);
    println!("[Recurdo] backend_url: http://{BACKEND_HOST}:{port}");

    let child = Command::new(&binary_path)
        .current_dir(&launch_dir)
        .env("RECURDO_DATA_DIR", &data_dir)
        .env("RECURDO_PORT", port.to_string())
        .env("RECURDO_ENV_FILE", data_dir.join(".env"))
        .stdin(Stdio::null())
        .stdout(open_backend_log_stdio(&stdout_log)?)
        .stderr(open_backend_log_stdio(&stderr_log)?)
        .spawn()
        .map_err(|e| format!("Failed to start backend: {}", e))?;

    println!("[Recurdo] Backend started with PID {}", child.id());
    Ok(child)
}

fn ensure_backend_running(app: &AppHandle) -> Result<(), Box<dyn std::error::Error>> {
    let port = current_backend_port(app).map_err(|err| IoError::new(ErrorKind::AddrNotAvailable, err))?;

    if backend_is_ready(port) {
        return Ok(());
    }

    let tracked_running = tracked_backend_is_running(app);
    if tracked_running && wait_for_backend(port, Duration::from_secs(2)) {
        return Ok(());
    }
    if tracked_running {
        return Ok(());
    }

    eprintln!(
        "Backend on http://{BACKEND_HOST}:{port} is unavailable; restarting bundled backend."
    );
    shutdown_backend(app);
    clear_backend_port(app);

    let port = current_backend_port(app).map_err(|err| IoError::new(ErrorKind::AddrNotAvailable, err))?;
    if backend_is_ready(port) {
        return Ok(());
    }

    let child = match start_backend(app, port) {
        Ok(child) => child,
        Err(err) if cfg!(debug_assertions) => {
            eprintln!(
                "Bundled backend unavailable in debug ({}). Falling back to local dev backend.",
                err
            );
            launch_dev_backend(port)?
        }
        Err(err) => {
            return Err(Box::new(IoError::new(ErrorKind::NotFound, err)));
        }
    };

    store_backend(app, child);

    // NOTE: This command is invoked from the WebView (JS) with a tight timeout.
    // Waiting several seconds here can cause the bridge call to time out, which
    // makes the UI fall back to a default port and then spin forever. We only
    // do a short warm-up probe and let the UI poll /ready.
    if !wait_for_backend(port, Duration::from_millis(600)) {
        eprintln!(
            "Backend is still starting on http://{BACKEND_HOST}:{port}; continuing app launch and letting the UI retry."
        );
    }

    Ok(())
}

fn shutdown_backend(app: &AppHandle) {
    let state = app.state::<BackendRuntime>();
    let mut guard = state.process.lock().expect("backend state lock poisoned");
    if let Some(mut backend) = guard.take() {
        let _ = backend.kill();
        let _ = backend.wait();
    }
}

fn session_path() -> PathBuf {
    resolved_session_path()
}

// ── Hotkey persistence ────────────────────────────────────────────────────────

const DEFAULT_HOTKEY: &str = "CommandOrControl+Shift+Space";
const HOTKEY_FALLBACKS: &[&str] = &[
    "CommandOrControl+Shift+Space",
    "CommandOrControl+Shift+R",
    "CommandOrControl+Option+Space",
];

fn hotkey_path() -> PathBuf {
    resolved_data_dir().join("hotkey.json")
}

fn load_hotkey() -> String {
    let path = hotkey_path();
    if path.exists() {
        if let Ok(raw) = std::fs::read_to_string(&path) {
            if let Ok(v) = serde_json::from_str::<serde_json::Value>(&raw) {
                if let Some(s) = v.get("shortcut").and_then(|s| s.as_str()) {
                    if !s.is_empty() {
                        return s.to_string();
                    }
                }
            }
        }
    }
    DEFAULT_HOTKEY.to_string()
}

fn save_hotkey(shortcut: &str) -> Result<(), String> {
    let data = json!({ "shortcut": shortcut });
    std::fs::write(
        hotkey_path(),
        serde_json::to_string_pretty(&data).map_err(|e| e.to_string())?,
    )
    .map_err(|e| e.to_string())
}

fn shortcut_display_label(shortcut: &str) -> String {
    shortcut
        .replace("CommandOrControl", "⌘")
        .replace("Command", "⌘")
        .replace("Control", "⌃")
        .replace("Shift", "⇧")
        .replace("Option", "⌥")
        .replace("Alt", "⌥")
        .replace('+', "")
        .replace("Space", "Space")
}

#[tauri::command]
fn get_hotkey_label() -> String {
    shortcut_display_label(&load_hotkey())
}

#[tauri::command]
fn set_hotkey(app: AppHandle, shortcut: String) -> Result<String, String> {
    use tauri_plugin_global_shortcut::GlobalShortcutExt;

    let manager = app.global_shortcut();

    // Unregister all existing shortcuts
    manager.unregister_all().map_err(|e| e.to_string())?;

    // Try to register the requested shortcut
    let target = shortcut.trim().to_string();
    let result = manager.register(target.as_str());

    if result.is_err() {
        // Restore default and report failure
        let _ = manager.register(DEFAULT_HOTKEY);
        let _ = save_hotkey(DEFAULT_HOTKEY);
        return Err(format!(
            "Could not register '{target}'. Restored default ({DEFAULT_HOTKEY})."
        ));
    }

    save_hotkey(&target)?;
    Ok(shortcut_display_label(&target))
}

// ── End hotkey ────────────────────────────────────────────────────────────────

fn save_session_to_disk(token: &str, email: &str, refresh_token: &str) -> Result<(), String> {
    let dir = resolved_data_dir();
    std::fs::create_dir_all(&dir).map_err(|e| e.to_string())?;
    let data = json!({
        "token": token,
        "refresh_token": refresh_token,
        "email": email,
        "saved_at": Utc::now().to_rfc3339(),
    });
    std::fs::write(
        session_path(),
        serde_json::to_string_pretty(&data).map_err(|e| e.to_string())?,
    )
    .map_err(|e| e.to_string())
}

fn generate_auth_state() -> String {
    let nanos = Utc::now()
        .timestamp_nanos_opt()
        .unwrap_or_else(|| Utc::now().timestamp_micros() * 1000);
    format!("auth-{}-{nanos}", std::process::id())
}

fn begin_auth_flow(app: &AppHandle) -> String {
    let state = generate_auth_state();
    let runtime = app.state::<BackendRuntime>();
    let mut pending = runtime
        .pending_auth_state
        .lock()
        .expect("pending auth state lock poisoned");
    *pending = Some(state.clone());
    state
}

fn clear_pending_auth_flow(app: &AppHandle) {
    let runtime = app.state::<BackendRuntime>();
    let mut pending = runtime
        .pending_auth_state
        .lock()
        .expect("pending auth state lock poisoned");
    *pending = None;
}

fn consume_pending_auth_flow(app: &AppHandle, expected_state: &str) -> bool {
    let runtime = app.state::<BackendRuntime>();
    let mut pending = runtime
        .pending_auth_state
        .lock()
        .expect("pending auth state lock poisoned");

    match pending.as_deref() {
        Some(current) if !expected_state.is_empty() && current == expected_state => {
            *pending = None;
            true
        }
        _ => false,
    }
}

fn parse_callback_params(url: &str) -> HashMap<String, String> {
    let parts = url
        .split(['?', '#'])
        .skip(1)
        .flat_map(|segment| segment.split('&'));

    parts
        .filter_map(|pair| {
            let mut kv = pair.splitn(2, '=');
            let key = kv.next()?;
            let value = kv.next()?;
            Some((key.to_string(), value.to_string()))
        })
        .collect()
}

fn resolve_popover_monitor(
    app: &AppHandle,
    window: &tauri::WebviewWindow,
) -> Result<tauri::Monitor, String> {
    if let Ok(cursor_position) = app.cursor_position() {
        if let Ok(Some(monitor)) = app.monitor_from_point(cursor_position.x, cursor_position.y) {
            return Ok(monitor);
        }
    }

    if let Ok(Some(monitor)) = window.current_monitor() {
        return Ok(monitor);
    }

    window
        .primary_monitor()
        .map_err(|e| e.to_string())?
        .ok_or_else(|| "no monitor".to_string())
}

fn place_popover(
    app: &AppHandle,
    window: &tauri::WebviewWindow,
    height: f64,
) -> Result<(), String> {
    let monitor = resolve_popover_monitor(app, window)?;

    let screen_size = monitor.size();
    let screen_position = monitor.position();
    let scale = monitor.scale_factor();
    let screen_width = screen_size.width as f64 / scale;
    let screen_height = screen_size.height as f64 / scale;
    let origin_x = screen_position.x as f64 / scale;
    let origin_y = screen_position.y as f64 / scale;
    let popover_width = POPOVER_WIDTH;
    let popover_height = height;
    // macOS menu bar is 24pt on older displays, 38pt on notched MacBook Pros.
    // Use 36pt as a safe default that clears the notch on all current hardware.
    let menu_bar_height = 36.0;
    let padding = POPOVER_SCREEN_PADDING;

    let x = origin_x + ((screen_width - popover_width) / 2.0).max(padding);
    let max_y = origin_y + screen_height - popover_height - padding;
    let preferred_y = origin_y + menu_bar_height + POPOVER_MENU_BAR_GAP;
    let y = preferred_y.clamp(origin_y + menu_bar_height + POPOVER_MENU_BAR_GAP, max_y);

    window
        .set_size(LogicalSize::new(popover_width, popover_height))
        .map_err(|e| e.to_string())?;
    window
        .set_position(LogicalPosition::new(x, y))
        .map_err(|e| e.to_string())?;

    Ok(())
}

#[cfg(target_os = "macos")]
fn configure_popover_for_fullscreen(window: &tauri::WebviewWindow) -> Result<(), String> {
    let ns_window = window.ns_window().map_err(|e| e.to_string())?;
    let ns_window: &NSWindow = unsafe { &*ns_window.cast() };

    let behavior = NSWindowCollectionBehavior::CanJoinAllSpaces
        | NSWindowCollectionBehavior::FullScreenAuxiliary
        | NSWindowCollectionBehavior::Stationary
        | NSWindowCollectionBehavior::CanJoinAllApplications;

    ns_window.setCollectionBehavior(behavior);
    ns_window.setLevel(NSStatusWindowLevel);

    Ok(())
}

fn show_popover_window(app: &AppHandle) -> Result<(), String> {
    let window = app
        .get_webview_window("popover")
        .ok_or_else(|| "popover window not found".to_string())?;

    #[cfg(target_os = "macos")]
    {
        window
            .set_visible_on_all_workspaces(true)
            .map_err(|e| e.to_string())?;
        configure_popover_for_fullscreen(&window)?;
    }

    place_popover(app, &window, POPOVER_COMPACT_HEIGHT)?;
    window.show().map_err(|e| e.to_string())?;
    window.unminimize().map_err(|e| e.to_string())?;
    window.set_focus().map_err(|e| e.to_string())?;
    force_front_window(&window);
    Ok(())
}

fn hide_popover_window(app: &AppHandle) -> Result<(), String> {
    let window = app
        .get_webview_window("popover")
        .ok_or_else(|| "popover window not found".to_string())?;
    window.hide().map_err(|e| e.to_string())
}

fn toggle_popover_window(app: &AppHandle) -> Result<(), String> {
    let window = app
        .get_webview_window("popover")
        .ok_or_else(|| "popover window not found".to_string())?;

    match window.is_visible() {
        Ok(true) => hide_popover_window(app),
        _ => show_popover_window(app),
    }
}

fn handle_deep_link(app: &AppHandle, url: String) {
    if !url.starts_with("recurdo://auth/callback") {
        return;
    }

    let params = parse_callback_params(&url);
    let error = params
        .get("error")
        .and_then(|value| urlencoding::decode(value).ok())
        .map(|value| value.into_owned())
        .unwrap_or_default();
    let error_description = params
        .get("error_description")
        .and_then(|value| urlencoding::decode(value).ok())
        .map(|value| value.into_owned())
        .unwrap_or_default();

    if !error.is_empty() {
        clear_pending_auth_flow(app);
        let _ = app.emit(
            "auth-callback",
            json!({
                "error": error,
                "error_description": error_description,
            }),
        );
        let _ = open_settings(app.clone());
        return;
    }

    let Some(token) = params.get("access_token") else {
        return;
    };

    let decoded = urlencoding::decode(token)
        .map(|token| token.into_owned())
        .unwrap_or_else(|_| token.to_string());
    let refresh_token = params
        .get("refresh_token")
        .and_then(|value| urlencoding::decode(value).ok())
        .map(|value| value.into_owned())
        .unwrap_or_default();
    let state = params
        .get("state")
        .and_then(|value| urlencoding::decode(value).ok())
        .map(|value| value.into_owned())
        .unwrap_or_default();
    let email = params
        .get("email")
        .and_then(|value| urlencoding::decode(value).ok())
        .map(|value| value.into_owned())
        .unwrap_or_default();

    if !consume_pending_auth_flow(app, &state) {
        eprintln!("Ignoring auth callback without a matching pending auth state");
        clear_pending_auth_flow(app);
        let _ = app.emit(
            "auth-callback",
            json!({
                "error": "OAuth state parameter is invalid",
                "error_description": "The browser returned an unexpected sign-in state. Please try again.",
            }),
        );
        let _ = open_settings(app.clone());
        return;
    }

    if let Err(error) = save_session_to_disk(&decoded, &email, &refresh_token) {
        eprintln!("Failed to save auth callback session: {error}");
    }

    let _ = app.emit(
        "auth-callback",
        json!({
            "token": decoded,
            "refresh_token": refresh_token,
            "email": email,
            "state": state,
        }),
    );
    let _ = open_settings(app.clone());
}

#[tauri::command]
fn save_session(
    token: String,
    user_email: String,
    refresh_token: Option<String>,
) -> Result<(), String> {
    save_session_to_disk(
        &token,
        &user_email,
        refresh_token.as_deref().unwrap_or_default(),
    )
}

#[tauri::command]
fn load_session() -> Option<Value> {
    let path = session_path();
    let content = std::fs::read_to_string(path).ok()?;
    serde_json::from_str(&content).ok()
}

#[tauri::command]
fn clear_session() -> Result<(), String> {
    let path = session_path();
    if path.exists() {
        std::fs::remove_file(path).map_err(|e| e.to_string())?;
    }
    Ok(())
}

#[tauri::command]
fn begin_auth(app: AppHandle) -> String {
    begin_auth_flow(&app)
}

#[tauri::command]
fn clear_pending_auth(app: AppHandle) {
    clear_pending_auth_flow(&app)
}

#[tauri::command]
async fn open_url(url: String) -> Result<(), String> {
    open::that(&url).map_err(|e| e.to_string())
}

#[tauri::command]
fn get_backend_origin(app: AppHandle) -> Result<Value, String> {
    ensure_backend_running(&app).map_err(|error| error.to_string())?;
    let port = current_backend_port(&app)?;
    Ok(json!({
        "host": BACKEND_HOST,
        "port": port,
        "http_url": format!("http://{BACKEND_HOST}:{port}"),
        "ws_url": format!("ws://{BACKEND_HOST}:{port}"),
    }))
}

#[tauri::command]
fn toggle_popover(app: AppHandle) -> Result<(), String> {
    toggle_popover_window(&app)
}

#[tauri::command]
fn close_popover(app: AppHandle) -> Result<(), String> {
    hide_popover_window(&app)
}

#[tauri::command]
fn resize_overlay(app: AppHandle, height: f64) -> Result<(), String> {
    let window = app
        .get_webview_window("popover")
        .ok_or_else(|| "popover window not found".to_string())?;

    let clamped = height.clamp(70.0, POPOVER_HEIGHT);
    place_popover(&app, &window, clamped)
}

#[tauri::command]
fn hide_overlay(app: AppHandle) -> Result<(), String> {
    hide_popover_window(&app)
}

#[tauri::command]
fn show_overlay(app: AppHandle) -> Result<(), String> {
    show_popover_window(&app)
}

#[tauri::command]
fn open_settings(app: AppHandle) -> Result<(), String> {
    let _ = hide_popover_window(&app);
    let window = match app.get_webview_window("settings") {
        Some(window) => window,
        None => {
            let config = app
                .config()
                .app
                .windows
                .iter()
                .find(|window| window.label == "settings")
                .ok_or_else(|| "settings window config not found".to_string())?;
            tauri::WebviewWindowBuilder::from_config(&app, config)
                .map_err(|e| e.to_string())?
                .build()
                .map_err(|e| e.to_string())?
        }
    };
    window.show().map_err(|e| e.to_string())?;
    window.unminimize().map_err(|e| e.to_string())?;
    window.set_focus().map_err(|e| e.to_string())?;
    force_front_window(&window);
    Ok(())
}

fn has_visible_window(app: &AppHandle) -> bool {
    app.webview_windows().values().any(|window| {
        window.is_visible().unwrap_or(false)
    })
}

#[cfg(target_os = "macos")]
fn force_front_window(window: &tauri::WebviewWindow) {
    if let Some(mtm) = MainThreadMarker::new() {
        let app = NSApplication::sharedApplication(mtm);
        #[allow(deprecated)]
        app.activateIgnoringOtherApps(true);
    }

    if let Ok(ns_window) = window.ns_window() {
        let ns_window: &NSWindow = unsafe { &*ns_window.cast() };
        ns_window.orderFrontRegardless();
        ns_window.makeKeyAndOrderFront(None);
    }
}

#[cfg(not(target_os = "macos"))]
fn force_front_window(_window: &tauri::WebviewWindow) {}

#[tauri::command]
fn set_autostart(enabled: bool, app: AppHandle) -> Result<(), String> {
    if enabled {
        app.autolaunch().enable().map_err(|e| e.to_string())?;
    } else {
        app.autolaunch().disable().map_err(|e| e.to_string())?;
    }
    Ok(())
}

#[tauri::command]
fn get_autostart(app: AppHandle) -> bool {
    app.autolaunch().is_enabled().unwrap_or(false)
}

#[cfg_attr(mobile, tauri::mobile_entry_point)]
pub fn run() {
    let app = tauri::Builder::default()
        .manage(BackendRuntime::default())
        .on_window_event(|window, event| match (window.label(), event) {
            ("popover", WindowEvent::Focused(false)) => {
                // Delay hide by 120ms so that Cmd+key shortcuts (e.g. Cmd+Enter for ask,
                // Cmd+, for settings) can fire their JS keydown handler before the window
                // disappears. macOS briefly steals focus when the Cmd modifier is pressed,
                // which would otherwise dismiss the popover before the shortcut runs.
                let app = window.app_handle().clone();
                let window_clone = window.clone();
                thread::spawn(move || {
                    thread::sleep(Duration::from_millis(120));
                    // Only hide if the window is still unfocused after the grace period.
                    if !window_clone.is_focused().unwrap_or(false) {
                        let _ = window_clone.hide();
                    }
                    drop(app);
                });
            }
            ("popover", WindowEvent::CloseRequested { api, .. }) => {
                api.prevent_close();
                let _ = window.hide();
            }
            ("settings", WindowEvent::CloseRequested { api, .. }) => {
                api.prevent_close();
                let _ = window.hide();
            }
            _ => {}
        })
        .on_tray_icon_event(|app, event| {
            if let tauri::tray::TrayIconEvent::Click {
                button: tauri::tray::MouseButton::Left,
                button_state: tauri::tray::MouseButtonState::Up,
                ..
            } = event
            {
                let _ = toggle_popover_window(app);
            }
        })
        .plugin(
            tauri_plugin_log::Builder::default()
                .level(log::LevelFilter::Info)
                .build(),
        )
        .plugin(tauri_plugin_dialog::init())
        .plugin(tauri_plugin_deep_link::init())
        .plugin(tauri_plugin_process::init())
        .plugin(tauri_plugin_shell::init())
        .plugin(tauri_plugin_updater::Builder::new().build())
        .plugin(tauri_plugin_autostart::init(
            tauri_plugin_autostart::MacosLauncher::LaunchAgent,
            None,
        ))
        .plugin(
            tauri_plugin_global_shortcut::Builder::new()
                .with_handler(|app, _shortcut, event| {
                    if event.state == ShortcutState::Pressed {
                        let _ = toggle_popover_window(app);
                    }
                })
                .build(),
        )
        .invoke_handler(tauri::generate_handler![
            save_session,
            load_session,
            clear_session,
            begin_auth,
            clear_pending_auth,
            open_url,
            get_backend_origin,
            toggle_popover,
            close_popover,
            resize_overlay,
            hide_overlay,
            show_overlay,
            open_settings,
            set_autostart,
            get_autostart,
            get_hotkey_label,
            set_hotkey
        ])
        .setup(|app| {
            // Register global shortcut — try persisted key first, then fallbacks
            {
                use tauri_plugin_global_shortcut::GlobalShortcutExt;
                let preferred = load_hotkey();
                let candidates: Vec<&str> = std::iter::once(preferred.as_str())
                    .chain(HOTKEY_FALLBACKS.iter().copied())
                    .collect();
                let manager = app.global_shortcut();
                let mut registered = false;
                for candidate in candidates {
                    if manager.register(candidate).is_ok() {
                        if candidate != preferred.as_str() {
                            let _ = save_hotkey(candidate);
                        }
                        eprintln!("[Recurdo] global shortcut registered: {candidate}");
                        registered = true;
                        break;
                    }
                }
                if !registered {
                    eprintln!("[Recurdo] WARNING: could not register any global shortcut");
                }
            }

            ensure_backend_running(app.handle())?;

            // Background watchdog: restart the Python backend if it crashes
            {
                let watchdog_handle = app.handle().clone();
                thread::spawn(move || {
                    thread::sleep(Duration::from_secs(30));
                    loop {
                        let port = match current_backend_port(&watchdog_handle) {
                            Ok(p) => p,
                            Err(_) => { thread::sleep(Duration::from_secs(30)); continue; }
                        };
                        if !backend_is_ready(port) {
                            eprintln!("[Recurdo] watchdog: backend unresponsive, restarting");
                            let _ = ensure_backend_running(&watchdog_handle);
                        }
                        thread::sleep(Duration::from_secs(30));
                    }
                });
            }

            #[cfg(target_os = "macos")]
            {
                let _ = app.set_activation_policy(ActivationPolicy::Regular);

                if let Some(window) = app.get_webview_window("popover") {
                    let _ = window.set_visible_on_all_workspaces(true);
                    let _ = configure_popover_for_fullscreen(&window);
                }
            }

            {
                let launch_handle = app.handle().clone();
                thread::spawn(move || {
                    thread::sleep(Duration::from_millis(600));
                    let main_handle = launch_handle.clone();
                    if let Err(error) = launch_handle.run_on_main_thread(move || {
                        if let Err(error) = open_settings(main_handle) {
                            eprintln!("[Recurdo] failed to open settings on launch: {error}");
                        }
                    }) {
                        eprintln!("[Recurdo] failed to schedule settings window on launch: {error}");
                    }
                });
            }

            Ok(())
        })
        .build(tauri::generate_context!())
        .expect("error while building tauri application");

    app.run(|app_handle, event| {
        match event {
            #[cfg(any(target_os = "macos", target_os = "ios"))]
            RunEvent::Opened { urls } => {
                for url in urls {
                    handle_deep_link(app_handle, url.to_string());
                }
            }
            #[cfg(target_os = "macos")]
            RunEvent::Reopen {
                ..
            } => {
                if let Err(error) = open_settings(app_handle.clone()) {
                    eprintln!("[Recurdo] failed to open settings on reopen: {error}");
                }
            }
            RunEvent::ExitRequested { code, api, .. } => {
                if code.is_none() && !has_visible_window(app_handle) {
                    api.prevent_exit();
                }
            }
            RunEvent::Exit => {
                shutdown_backend(app_handle);
            }
            _ => {}
        };
    });
}
