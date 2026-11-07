// findr desktop: one window for setup, folders, indexing and settings; the search page itself comes from the local engine.
//
// The engine is Python. The app carries its source and a small Python manager (uv); on the first run it builds a private
// environment in the data folder, picks the PyTorch build for this PC's GPU, and downloads the models. Nothing else on the PC
// is touched, and no system Python or git is needed.
use serde::Serialize;
use std::io::{BufRead, BufReader};
use std::path::{Path, PathBuf};
use std::process::{Child, Command, Stdio};
use std::sync::Mutex;
use std::thread;
use tauri::menu::{Menu, MenuItem, PredefinedMenuItem, Submenu};
use tauri::{AppHandle, Emitter, Manager, State, Url};
use tauri_plugin_opener::OpenerExt;

const VERSION: &str = env!("CARGO_PKG_VERSION");

struct Engine(Mutex<Option<Child>>);

// the search page's port (FINDR_APP_PORT for side-by-side testing)
fn port() -> u16 {
    std::env::var("FINDR_APP_PORT").ok().and_then(|p| p.parse().ok()).unwrap_or(8096)
}
struct AppUrl(Mutex<Option<Url>>);

// ---------- places ----------

fn home() -> PathBuf {
    if let Ok(h) = std::env::var("FINDR_HOME") {
        return PathBuf::from(h);
    }
    #[cfg(windows)]
    if let Ok(l) = std::env::var("LOCALAPPDATA") {
        return PathBuf::from(l).join("findr");
    }
    let h = std::env::var("HOME").or_else(|_| std::env::var("USERPROFILE")).unwrap_or_else(|_| ".".into());
    PathBuf::from(h).join(".findr")
}

fn runtime() -> PathBuf {
    home().join("runtime")
}

fn python() -> PathBuf {
    if cfg!(windows) {
        runtime().join("Scripts").join("python.exe")
    } else {
        runtime().join("bin").join("python")
    }
}

fn ready_mark() -> PathBuf {
    runtime().join(".findr-ready")
}

fn uv() -> PathBuf {
    // bundled next to the app executable (Tauri external binary); fall back to one on PATH (development)
    let name = if cfg!(windows) { "uv.exe" } else { "uv" };
    if let Ok(exe) = std::env::current_exe() {
        if let Some(dir) = exe.parent() {
            let p = dir.join(name);
            if p.exists() {
                return p;
            }
        }
    }
    PathBuf::from(name)
}

fn gpu() -> &'static str {
    if cfg!(target_os = "macos") {
        return "apple";
    }
    let ok = command("nvidia-smi").arg("-L").stdout(Stdio::null()).stderr(Stdio::null()).status().map(|s| s.success()).unwrap_or(false);
    if ok { "nvidia" } else { "cpu" }
}

// every child process: no console window on Windows, UTF-8 and unbuffered output for the engine
fn command(prog: impl AsRef<std::ffi::OsStr>) -> Command {
    let mut c = Command::new(prog);
    c.env("PYTHONUTF8", "1").env("PYTHONUNBUFFERED", "1").env("FINDR_HOME", home());
    #[cfg(windows)]
    {
        use std::os::windows::process::CommandExt;
        c.creation_flags(0x0800_0000); // CREATE_NO_WINDOW
    }
    c
}

/// run a command, sending each output line to the window as `event`; true when it succeeded
fn stream(app: &AppHandle, event: &str, step: &str, mut cmd: Command) -> bool {
    let child = cmd.stdout(Stdio::piped()).stderr(Stdio::piped()).stdin(Stdio::null()).spawn();
    let mut child = match child {
        Ok(c) => c,
        Err(e) => {
            let _ = app.emit(event, Line { step: step.into(), line: format!("실행하지 못했습니다: {e}") });
            return false;
        }
    };
    let mut joins = vec![];
    for r in [child.stdout.take().map(|s| Box::new(s) as Box<dyn std::io::Read + Send>),
              child.stderr.take().map(|s| Box::new(s) as Box<dyn std::io::Read + Send>)].into_iter().flatten() {
        let (app, event, step) = (app.clone(), event.to_string(), step.to_string());
        joins.push(thread::spawn(move || {
            for line in BufReader::new(r).lines().map_while(Result::ok) {
                let line = line.trim().to_string();
                if !line.is_empty() {
                    let _ = app.emit(&event, Line { step: step.clone(), line });
                }
            }
        }));
    }
    for j in joins {
        let _ = j.join();
    }
    child.wait().map(|s| s.success()).unwrap_or(false)
}

#[derive(Clone, Serialize)]
struct Line {
    step: String,
    line: String,
}

#[derive(Clone, Serialize)]
struct Done {
    ok: bool,
    message: String,
}

// ---------- commands ----------

/// a fingerprint of the engine this app carries: when it differs from the installed one, the engine is updated
fn engine_stamp(app: &AppHandle) -> String {
    use std::hash::{Hash, Hasher};
    fn walk(p: &Path, h: &mut std::collections::hash_map::DefaultHasher) {
        let mut es: Vec<_> = std::fs::read_dir(p).map(|r| r.flatten().collect()).unwrap_or_default();
        es.sort_by_key(|e| e.file_name());
        for e in es {
            let n = e.file_name();
            if n == "__pycache__" { continue; }
            if e.path().is_dir() { walk(&e.path(), h); } else if let Ok(b) = std::fs::read(e.path()) { n.hash(h); b.hash(h); }
        }
    }
    let mut h = std::collections::hash_map::DefaultHasher::new();
    if let Some(src) = engine_source(app) { walk(&src, &mut h); }
    format!("{VERSION}-{:016x}", h.finish())
}

#[derive(Serialize)]
struct AppState {
    home: String,
    ready: bool,
    indexed: bool,
    gpu: String,
    version: String,
    folders: Vec<String>,
    autotest: bool,
    update: bool,
}

#[tauri::command]
fn app_state(app: AppHandle) -> AppState {
    let installed = std::fs::read_to_string(ready_mark()).unwrap_or_default();
    let current = python().exists() && !installed.is_empty() && installed.trim() == engine_stamp(&app);
    AppState {
        home: home().display().to_string(),
        ready: current,
        update: python().exists() && !installed.is_empty() && !current,   // installed before, the app now carries a newer engine
        indexed: home().join("index.db").exists(),
        gpu: gpu().into(),
        version: VERSION.into(),
        folders: load_folders(),
        // development builds only: FINDR_AUTOTEST=1 walks through the screens by itself (automated screen tests)
        autotest: cfg!(debug_assertions) && std::env::var("FINDR_AUTOTEST").is_ok(),
    }
}

/// first run: private Python environment -> PyTorch for this GPU -> the engine -> models. Progress goes out as `setup` events.
#[tauri::command]
fn setup_runtime(app: AppHandle) {
    thread::spawn(move || {
        let done = |ok: bool, message: &str| {
            let _ = app.emit("setup-done", Done { ok, message: message.into() });
        };
        let _ = std::fs::create_dir_all(home());
        // 1. environment (uv also fetches Python 3.12 itself when the PC has none)
        let mut c = command(uv());
        c.args(["venv", "--python", "3.12", "--allow-existing"]).arg(runtime());
        if !stream(&app, "setup", "env", c) {
            return done(false, "파이썬 환경을 만들지 못했습니다. 인터넷 연결을 확인하고 다시 시도해 주세요.");
        }
        // 2. PyTorch for this PC
        let _ = app.emit("setup", Line { step: "torch".into(), line: "이 PC에 맞는 연산 라이브러리를 고르는 중".into() });
        let mut c = command(uv());
        c.args(["pip", "install", "--python"]).arg(python()).args(["torch", "torchvision"]);
        match gpu() {
            "nvidia" => { c.args(["--index-url", "https://download.pytorch.org/whl/cu128"]); }
            "cpu" => { c.args(["--index-url", "https://download.pytorch.org/whl/cpu"]); }
            _ => {}
        }
        if !stream(&app, "setup", "torch", c) {
            return done(false, "PyTorch를 설치하지 못했습니다. 인터넷 연결과 디스크 여유 공간을 확인해 주세요.");
        }
        // 3. the engine (copied out of the app first: the app folder can be read-only)
        let _ = app.emit("setup", Line { step: "engine".into(), line: "검색 엔진 파일을 준비하는 중".into() });
        let src = match engine_source(&app) {
            Some(p) => p,
            None => return done(false, "앱 안에서 검색 엔진 파일을 찾지 못했습니다. 앱을 다시 설치해 주세요."),
        };
        let dst = home().join("engine-src");
        let _ = std::fs::remove_dir_all(&dst);
        if let Err(e) = copy_dir(&src, &dst) {
            return done(false, &format!("엔진 파일을 준비하지 못했습니다: {e}"));
        }
        let mut c = command(uv());
        c.args(["pip", "install", "--reinstall-package", "findr-ai", "--python"]).arg(python()).arg(&dst);
        if !stream(&app, "setup", "engine", c) {
            return done(false, "검색 엔진을 설치하지 못했습니다.");
        }
        // 4. models for this PC
        let _ = app.emit("setup", Line { step: "models".into(), line: "이 PC에 맞는 모델을 고르는 중".into() });
        let mut c = command(python());
        c.args(["-m", "findr", "setup"]);
        if !stream(&app, "setup", "models", c) {
            return done(false, "모델을 받지 못했습니다. 인터넷 연결을 확인하고 다시 시도해 주세요.");
        }
        let _ = std::fs::write(ready_mark(), engine_stamp(&app));
        done(true, "준비가 끝났습니다.");
    });
}

/// where the bundled engine source is: the resource folder of an installed app, or next to the executable in a development build
fn engine_source(app: &AppHandle) -> Option<PathBuf> {
    let mut cands = vec![];
    if let Ok(d) = app.path().resource_dir() {
        cands.push(d.join("engine"));
    }
    if let Some(d) = std::env::current_exe().ok().and_then(|e| e.parent().map(Path::to_path_buf)) {
        cands.push(d.join("engine"));
        cands.push(d.join("resources").join("engine"));
    }
    cands.into_iter().find(|p| p.join("pyproject.toml").exists())
}

fn copy_dir(src: &Path, dst: &Path) -> std::io::Result<()> {
    std::fs::create_dir_all(dst)?;
    for e in std::fs::read_dir(src)? {
        let e = e?;
        let name = e.file_name();
        if name == "__pycache__" {
            continue;
        }
        let to = dst.join(&name);
        if e.file_type()?.is_dir() {
            copy_dir(&e.path(), &to)?;
        } else {
            std::fs::copy(e.path(), to)?;
        }
    }
    Ok(())
}

/// the folders a whole-PC index covers on this PC, in the order people look in them (JSON from the engine)
#[tauri::command]
fn list_roots() -> Result<String, String> {
    let out = command(python()).args(["-m", "findr", "roots"]).output().map_err(|e| e.to_string())?;
    if !out.status.success() {
        return Err(String::from_utf8_lossy(&out.stderr).lines().last().unwrap_or("").to_string());
    }
    Ok(String::from_utf8_lossy(&out.stdout).trim().to_string())
}

#[tauri::command]
fn save_folders(folders: Vec<String>) -> Result<(), String> {
    let _ = std::fs::create_dir_all(home());
    std::fs::write(home().join("folders.json"), serde_json::to_string_pretty(&folders).unwrap()).map_err(|e| e.to_string())
}

#[tauri::command]
fn load_folders() -> Vec<String> {
    std::fs::read_to_string(home().join("folders.json")).ok().and_then(|s| serde_json::from_str(&s).ok()).unwrap_or_default()
}

/// index the chosen folders in order; progress as `index` events, then `index-done`
#[tauri::command]
fn start_index(app: AppHandle, folders: Vec<String>, deep: Option<bool>) {
    thread::spawn(move || {
        let mut c = command(python());
        c.args(["-m", "findr", "index"]).args(&folders);
        if deep.unwrap_or(false) {
            c.arg("--deep"); // the later pieces of long documents too
        }
        let ok = stream(&app, "index", "index", c);
        let _ = app.emit("index-done", Done { ok, message: if ok { "색인을 마쳤습니다.".into() } else { "색인 중 오류가 있었습니다.".into() } });
    });
}

/// start (or restart) the engine; returns the address that signs the window in
#[tauri::command]
fn start_engine(engine: State<Engine>, photo: bool) -> Result<String, String> {
    stop(&engine);
    let mut c = command(python());
    c.args(["-m", "findr", "serve", "--app", "--no-browser", "--port", &port().to_string()]);
    if !photo {
        c.arg("--no-photo");
    }
    let log = std::fs::File::create(home().join("logs").join("engine.log")).or_else(|_| {
        let _ = std::fs::create_dir_all(home().join("logs"));
        std::fs::File::create(home().join("logs").join("engine.log"))
    }).map_err(|e| e.to_string())?;
    let mut child = c.stdout(Stdio::piped()).stderr(log).stdin(Stdio::null()).spawn().map_err(|e| e.to_string())?;
    let out = child.stdout.take().ok_or("no output")?;
    let mut lines = BufReader::new(out).lines();
    let mut ready = false;
    for line in lines.by_ref().map_while(Result::ok) {
        if line.starts_with("FINDR_APP_READY") {
            ready = true;
            break;
        }
    }
    thread::spawn(move || for _ in lines.map_while(Result::ok) {}); // keep the pipe drained
    *engine.0.lock().unwrap() = Some(child);
    if !ready {
        stop(&engine);
        return Err("검색 엔진이 시작되지 않았습니다. 데이터 폴더의 logs/engine.log를 확인해 주세요.".into());
    }
    let key = std::fs::read_to_string(home().join(".password")).map_err(|e| e.to_string())?;
    Ok(format!("http://127.0.0.1:{}/auth?t={}", port(), key.trim()))
}

#[tauri::command]
fn stop_engine(engine: State<Engine>) {
    stop(&engine);
}

fn stop(engine: &Engine) {
    if let Some(mut child) = engine.0.lock().unwrap().take() {
        // the engine runs helper servers of its own: end the whole tree, gently where the system allows
        #[cfg(windows)]
        {
            let _ = command("taskkill").args(["/PID", &child.id().to_string(), "/T", "/F"]).status();
        }
        #[cfg(not(windows))]
        {
            let _ = Command::new("kill").args(["-TERM", &child.id().to_string()]).status();
            for _ in 0..50 {
                if let Ok(Some(_)) = child.try_wait() {
                    return;
                }
                thread::sleep(std::time::Duration::from_millis(100));
            }
        }
        let _ = child.kill();
        let _ = child.wait();
    }
}

/// open a found file (or show it in its folder). Programs and scripts are only shown, never run.
#[tauri::command]
fn open_path(app: AppHandle, path: String, reveal: bool) -> Result<(), String> {
    let p = PathBuf::from(&path);
    if !p.exists() {
        return Err("파일이 없습니다. 옮겨졌거나 지워졌을 수 있습니다.".into());
    }
    let ext = p.extension().and_then(|e| e.to_str()).unwrap_or("").to_lowercase();
    const RUNS: [&str; 18] = ["exe", "bat", "cmd", "com", "msi", "ps1", "vbs", "vbe", "js", "jse", "wsf", "scr", "lnk", "sh", "app", "command", "jar", "reg"];
    if reveal || RUNS.contains(&ext.as_str()) {
        app.opener().reveal_item_in_dir(&p).map_err(|e| e.to_string())
    } else {
        app.opener().open_path(path, None::<&str>).map_err(|e| e.to_string())
    }
}

#[tauri::command]
fn open_data_dir(app: AppHandle) -> Result<(), String> {
    let _ = std::fs::create_dir_all(home());
    app.opener().open_path(home().display().to_string(), None::<&str>).map_err(|e| e.to_string())
}

// ---------- app ----------

fn go_app(app: &AppHandle, hash: &str) {
    if let (Some(w), Some(u)) = (app.get_webview_window("main"), app.state::<AppUrl>().0.lock().unwrap().clone()) {
        let mut u = u;
        u.set_fragment(Some(hash));
        let _ = w.navigate(u);
    }
}

#[cfg_attr(mobile, tauri::mobile_entry_point)]
pub fn run() {
    tauri::Builder::default()
        .plugin(tauri_plugin_opener::init())
        .plugin(tauri_plugin_dialog::init())
        .manage(Engine(Mutex::new(None)))
        .manage(AppUrl(Mutex::new(None)))
        .setup(|app| {
            if let Some(w) = app.get_webview_window("main") {
                *app.state::<AppUrl>().0.lock().unwrap() = w.url().ok();
            }
            let h = app.handle();
            let search = MenuItem::with_id(h, "search", "검색", true, Some("CmdOrCtrl+F"))?;
            let settings = MenuItem::with_id(h, "settings", "폴더와 설정", true, Some("CmdOrCtrl+,"))?;
            let data = MenuItem::with_id(h, "data", "데이터 폴더 열기", true, None::<&str>)?;
            let quit = MenuItem::with_id(h, "quit", "끝내기", true, Some("CmdOrCtrl+Q"))?;
            let sep = PredefinedMenuItem::separator(h)?;
            let file = Submenu::with_items(h, "findr", true, &[&search, &settings, &data, &sep, &quit])?;
            let edit = Submenu::with_items(h, "편집", true, &[
                &PredefinedMenuItem::undo(h, None)?, &PredefinedMenuItem::redo(h, None)?, &PredefinedMenuItem::separator(h)?,
                &PredefinedMenuItem::cut(h, None)?, &PredefinedMenuItem::copy(h, None)?, &PredefinedMenuItem::paste(h, None)?,
                &PredefinedMenuItem::select_all(h, None)?,
            ])?;
            app.set_menu(Menu::with_items(h, &[&file, &edit])?)?;
            Ok(())
        })
        .on_menu_event(|app, e| match e.id().as_ref() {
            "search" => go_app(app, "search"),
            "settings" => go_app(app, "settings"),
            "data" => { let _ = open_data_dir(app.clone()); }
            "quit" => { stop(&app.state::<Engine>()); app.exit(0); }
            _ => {}
        })
        .invoke_handler(tauri::generate_handler![
            app_state, setup_runtime, list_roots, start_index, start_engine, stop_engine, open_path, open_data_dir, save_folders, load_folders
        ])
        .build(tauri::generate_context!())
        .expect("findr 창을 여는 중 오류가 났습니다")
        .run(|app, e| {
            if let tauri::RunEvent::Exit = e {
                stop(&app.state::<Engine>());
            }
        });
}
