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

// one indexing run at a time: the background refresh and a run started from the app's screens
static INDEXING: std::sync::atomic::AtomicBool = std::sync::atomic::AtomicBool::new(false);
static REFRESHER: std::sync::atomic::AtomicBool = std::sync::atomic::AtomicBool::new(false);

fn take_index_turn() {
    use std::sync::atomic::Ordering;
    while INDEXING.compare_exchange(false, true, Ordering::SeqCst, Ordering::SeqCst).is_err() {
        thread::sleep(std::time::Duration::from_secs(2));
    }
}

fn give_index_turn() {
    INDEXING.store(false, std::sync::atomic::Ordering::SeqCst);
}

/// the engine at low priority, so background indexing never slows the person's own work
fn low_priority(prog: impl AsRef<std::ffi::OsStr>) -> Command {
    #[cfg(windows)]
    {
        use std::os::windows::process::CommandExt;
        let mut c = command(prog);
        c.creation_flags(0x0800_0000 | 0x0000_4000); // no window, below normal priority
        c
    }
    #[cfg(not(windows))]
    {
        let mut c = command("nice");
        c.args(["-n", "10"]).arg(prog.as_ref());
        c
    }
}

/// running on battery: background indexing waits for the charger (Windows, macOS, Linux)
fn on_battery() -> bool {
    #[cfg(windows)]
    {
        // BatteryStatus 2 = on AC; no battery at all (a desktop) prints nothing
        let out = command("powershell").args(["-NoProfile", "-Command", "(Get-CimInstance Win32_Battery).BatteryStatus"]).output();
        if let Ok(o) = out {
            let s = String::from_utf8_lossy(&o.stdout).trim().to_string();
            return !s.is_empty() && s != "2";
        }
        false
    }
    #[cfg(target_os = "macos")]
    {
        Command::new("pmset").args(["-g", "batt"]).output()
            .map(|o| String::from_utf8_lossy(&o.stdout).contains("Battery Power")).unwrap_or(false)
    }
    #[cfg(all(unix, not(target_os = "macos")))]
    {
        let ac = std::fs::read_dir("/sys/class/power_supply").map(|d| d.flatten()
            .filter(|e| std::fs::read_to_string(e.path().join("type")).map(|t| t.trim() == "Mains").unwrap_or(false))
            .any(|e| std::fs::read_to_string(e.path().join("online")).map(|t| t.trim() == "1").unwrap_or(false))).unwrap_or(true);
        let has_mains = std::fs::read_dir("/sys/class/power_supply").map(|d| d.flatten()
            .any(|e| std::fs::read_to_string(e.path().join("type")).map(|t| t.trim() == "Mains").unwrap_or(false))).unwrap_or(false);
        has_mains && !ac
    }
}

/// seconds since the person last touched the keyboard or mouse; None where it cannot be read
fn idle_seconds() -> Option<u64> {
    #[cfg(windows)]
    {
        #[repr(C)]
        struct LastInput {
            size: u32,
            time: u32,
        }
        #[link(name = "user32")]
        extern "system" {
            fn GetLastInputInfo(p: *mut LastInput) -> i32;
        }
        extern "system" {
            fn GetTickCount() -> u32;
        }
        let mut li = LastInput { size: 8, time: 0 };
        if unsafe { GetLastInputInfo(&mut li) } == 0 {
            return None;
        }
        Some((unsafe { GetTickCount() }.wrapping_sub(li.time) / 1000) as u64)
    }
    #[cfg(target_os = "macos")]
    {
        let o = Command::new("ioreg").args(["-c", "IOHIDSystem", "-d", "4"]).output().ok()?;
        let s = String::from_utf8_lossy(&o.stdout);
        let l = s.lines().find(|l| l.contains("\"HIDIdleTime\""))?;
        l.rsplit('=').next()?.trim().parse::<u64>().ok().map(|ns| ns / 1_000_000_000)
    }
    #[cfg(all(unix, not(target_os = "macos")))]
    {
        let o = Command::new("xprintidle").output().ok()?;
        String::from_utf8_lossy(&o.stdout).trim().parse::<u64>().ok().map(|ms| ms / 1000)
    }
}

// the idle-time upgrade's process: stopped at once when the person starts an indexing run
static UPGRADING: Mutex<Option<Child>> = Mutex::new(None);

fn stop_upgrade() {
    if let Some(mut c) = UPGRADING.lock().unwrap().take() {
        let _ = c.kill();
        let _ = c.wait();
    }
}

/// a PC that started on the small meaning model moves to the larger one while nobody uses it (plugged in, 5 minutes
/// without input), 10 minutes at a time; search keeps working with the small model until the switch
fn start_upgrader() {
    thread::spawn(|| {
        let mut last_run = std::time::Instant::now();
        thread::sleep(std::time::Duration::from_secs(900));
        loop {
            let h = home();
            let idle = match idle_seconds() {
                Some(s) => s >= 300,
                None => last_run.elapsed().as_secs() >= 3000, // cannot tell: at most one short run an hour
            };
            if h.join(".deep-done").exists() && !h.join(".upgrade-done").exists() && !h.join("upgrade_off").exists() && idle && !on_battery() {
                take_index_turn();
                let half = std::thread::available_parallelism().map(|n| (n.get() / 2).max(1)).unwrap_or(2);
                let mut c = low_priority(python());
                c.env("FINDR_THREADS", half.to_string()).env("OMP_NUM_THREADS", half.to_string());
                c.args(["-m", "findr", "upgrade", "--minutes", "10"]);
                let log = std::fs::OpenOptions::new().create(true).append(true).open(h.join("logs").join("upgrade.log"));
                let code = match log.map(|f| c.stdout(f).stderr(Stdio::null()).stdin(Stdio::null()).spawn()) {
                    Ok(Ok(child)) => {
                        *UPGRADING.lock().unwrap() = Some(child);
                        loop {
                            thread::sleep(std::time::Duration::from_secs(2));
                            let mut g = UPGRADING.lock().unwrap();
                            match g.as_mut().map(|c| c.try_wait()) {
                                Some(Ok(Some(st))) => {
                                    g.take();
                                    break st.code();
                                }
                                Some(Ok(None)) => continue,
                                _ => {
                                    g.take();
                                    break None; // stopped for an indexing run
                                }
                            }
                        }
                    }
                    _ => None,
                };
                give_index_turn();
                last_run = std::time::Instant::now();
                if code == Some(0) || code == Some(1) {
                    let _ = std::fs::write(h.join(".upgrade-done"), VERSION); // switched, or nothing to do on this PC
                }
            }
            thread::sleep(std::time::Duration::from_secs(600));
        }
    });
}

/// the idle-time upgrade for the settings screen: {"on", "done", "reason", "progress"}
#[tauri::command]
fn upgrade_state() -> String {
    let h = home();
    let p: serde_json::Value = std::fs::read_to_string(h.join("upgrade.json")).ok().and_then(|s| serde_json::from_str(&s).ok()).unwrap_or(serde_json::Value::Null);
    serde_json::json!({
        "on": !h.join("upgrade_off").exists(),
        "done": h.join(".upgrade-done").exists(),
        "reason": std::fs::read_to_string(h.join("upgrade_off")).unwrap_or_default(),
        "progress": p,
    }).to_string()
}

#[tauri::command]
fn set_upgrade(on: bool) -> Result<(), String> {
    let h = home();
    if on {
        let _ = std::fs::remove_file(h.join("upgrade_off"));
        let _ = std::fs::remove_file(h.join(".upgrade-done")); // look again (another index, more memory)
        Ok(())
    } else {
        stop_upgrade();
        std::fs::write(h.join("upgrade_off"), "꺼짐").map_err(|e| e.to_string())
    }
}

/// after the search page is up: changed files now and every hour, and once the later pieces of long documents
fn start_refresher() {
    use std::sync::atomic::Ordering;
    if REFRESHER.swap(true, Ordering::SeqCst) {
        return;
    }
    start_upgrader();
    thread::spawn(|| {
        let started = std::time::Instant::now();
        let mut last_refresh: Option<std::time::Instant> = None;
        loop {
        let folders = load_folders();
        if !folders.is_empty() && home().join("index.db").exists() && !on_battery() {
            // changed files: at once, then every hour
            let refresh_due = last_refresh.map(|t| t.elapsed().as_secs() >= 3600).unwrap_or(true);
            // the deep run waits for idle time, as the model upgrade does: started right after the page opened, its 28 minutes
            // made the first search 27.9 s and left 0.5 GB free on an 8 GB laptop (#66 rerun)
            let idle = match idle_seconds() {
                Some(s) => s >= 300,
                None => started.elapsed().as_secs() >= 3000, // cannot tell: not in the first 50 minutes
            };
            for deep in [false, true] {
                // the deep run (rest of long documents, scans on a CPU, the opening-only reads of the first pass) every 6 hours:
                // files added since the last one get the same care
                let fresh = std::fs::metadata(home().join(".deep-done")).and_then(|m| m.modified()).ok()
                    .and_then(|t| t.elapsed().ok()).map(|e| e.as_secs() < 6 * 3600).unwrap_or(false);
                if (deep && (fresh || !idle)) || (!deep && !refresh_due) {
                    continue;
                }
                if !deep {
                    last_refresh = Some(std::time::Instant::now());
                }
                take_index_turn();
                let log = std::fs::File::create(home().join("logs").join(if deep { "deep.log" } else { "refresh.log" }));
                let mut c = low_priority(python());
                let half = std::thread::available_parallelism().map(|n| (n.get() / 2).max(1)).unwrap_or(2);
                c.env("FINDR_THREADS", half.to_string()).env("OMP_NUM_THREADS", half.to_string());   // leave cores for the person
                c.args(["-m", "findr", "index"]).args(&folders);
                if deep {
                    c.arg("--deep");
                }
                let ok = match log {
                    Ok(f) => c.stdout(f).stderr(Stdio::null()).stdin(Stdio::null()).status().map(|s| s.success()).unwrap_or(false),
                    Err(_) => false,
                };
                give_index_turn();
                if deep && ok {
                    let _ = std::fs::write(home().join(".deep-done"), VERSION);
                }
            }
        }
        thread::sleep(std::time::Duration::from_secs(600)); // look every 10 minutes: idle time for the deep run is caught soon
        }
    });
}

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
    c.env("PYTHONUTF8", "1").env("PYTHONUNBUFFERED", "1").env("FINDR_HOME", home())
        .env("UV_NATIVE_TLS", "1");   // company networks that inspect TLS: trust the system's certificates
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
        // 0. Windows: the Visual C++ runtime that Python 3.12 and PyTorch need (14.40 or later, #3)
        if let Err(m) = ensure_vc_runtime(&app) {
            return done(false, &m);
        }
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
            return done(false, "구성 요소를 받거나 불러오지 못했습니다. 아래 '자세한 기록'의 마지막 줄을 확인하고 다시 시도해 주세요.");
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

/// Windows: Microsoft Visual C++ runtime 14.40+ (an older one breaks Python 3.12 and PyTorch). Installs the official
/// redistributable when it is missing or old; Windows asks the person once for permission.
fn ensure_vc_runtime(app: &AppHandle) -> Result<(), String> {
    let say = |line: &str| { let _ = app.emit("setup", Line { step: "vcrt".into(), line: line.into() }); };
    if !cfg!(windows) {
        say("필요 없음");
        return Ok(());
    }
    let version = || -> (u32, u32) {
        let out = command("powershell").args(["-NoProfile", "-Command",
            "[System.Diagnostics.FileVersionInfo]::GetVersionInfo(\"$env:WINDIR\\System32\\msvcp140.dll\").FileVersion"]).output();
        let s = out.map(|o| String::from_utf8_lossy(&o.stdout).trim().to_string()).unwrap_or_default();
        let mut it = s.split(|c: char| c == '.' || c == ',' || c == ' ').filter_map(|x| x.parse::<u32>().ok());
        (it.next().unwrap_or(0), it.next().unwrap_or(0))
    };
    let (ma, mi) = version();
    if ma > 14 || (ma == 14 && mi >= 40) {
        say(&format!("Visual C++ 런타임 {ma}.{mi}: 준비됨"));
        return Ok(());
    }
    say(if ma == 0 { "Visual C++ 런타임이 없어 설치합니다 (권한 확인 창이 뜨면 '예')" } else { "Visual C++ 런타임이 오래되어 업데이트합니다 (권한 확인 창이 뜨면 '예')" });
    let exe = std::env::temp_dir().join("vc_redist.x64.exe");
    let script = format!(
        "Invoke-WebRequest -UseBasicParsing -Uri https://aka.ms/vs/17/release/vc_redist.x64.exe -OutFile '{0}';          Start-Process -FilePath '{0}' -ArgumentList '/install','/quiet','/norestart' -Verb RunAs -Wait",
        exe.display());
    let _ = command("powershell").args(["-NoProfile", "-Command", &script]).status();
    let (ma, mi) = version();
    if ma > 14 || (ma == 14 && mi >= 40) {
        say(&format!("Visual C++ 런타임 {ma}.{mi}: 설치됨"));
        Ok(())
    } else {
        Err("Windows 구성 요소(Visual C++ 런타임)를 설치하지 못했습니다. https://aka.ms/vs/17/release/vc_redist.x64.exe 를 받아 설치한 뒤 다시 시도해 주세요.".into())
    }
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

/// where indexing stands (progress.json written by the indexer), or "" before it starts
#[tauri::command]
fn read_progress() -> String {
    std::fs::read_to_string(home().join("progress.json")).unwrap_or_default()
}

/// pause or resume indexing: the indexer holds between steps while the file exists (a forgotten pause lapses after an hour)
#[tauri::command]
fn set_pause(on: bool) -> Result<(), String> {
    let f = home().join("pause");
    if on { std::fs::write(f, "1").map_err(|e| e.to_string()) } else { let _ = std::fs::remove_file(f); Ok(()) }
}

/// index the chosen folders in order; progress as `index` events, then `index-done`
#[tauri::command]
fn start_index(app: AppHandle, folders: Vec<String>, deep: Option<bool>) {
    thread::spawn(move || {
        let _ = app.emit("index", Line { step: "index".into(), line: "앞선 색인 작업이 끝나기를 기다리는 중".into() });
        stop_upgrade(); // the person's own run comes first (the upgrade keeps what it computed)
        let _ = std::fs::remove_file(home().join("progress.json")); // a fresh picture for this run
        let _ = std::fs::remove_file(home().join("pause"));
        take_index_turn();
        let mut c = command(python());
        c.args(["-m", "findr", "index"]).args(&folders);
        if deep.unwrap_or(false) {
            c.arg("--deep"); // the later pieces of long documents too
        }
        let ok = stream(&app, "index", "index", c);
        give_index_turn();
        if deep.unwrap_or(false) && ok {
            let _ = std::fs::write(home().join(".deep-done"), VERSION);
        }
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
    start_refresher();
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
            app_state, setup_runtime, list_roots, start_index, start_engine, stop_engine, open_path, open_data_dir, save_folders, load_folders,
            upgrade_state, set_upgrade, read_progress, set_pause
        ])
        .build(tauri::generate_context!())
        .expect("findr 창을 여는 중 오류가 났습니다")
        .run(|app, e| {
            if let tauri::RunEvent::Exit = e {
                stop(&app.state::<Engine>());
            }
        });
}
