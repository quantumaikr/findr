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
/// the pinned transformers wheel from its second home, for `uv pip install --override` (keep in step with pyproject.toml)
const ALT_DEPS: &str = "transformers @ https://github.com/quantumaikr/findr/releases/download/deps/transformers-5.23.0.dev0-py3-none-any.whl#sha256=a8b00f7db929ea8cc11c7422585f0ea99d72804e14b3978fb3dd3d9fed5a1ef6\n";

struct Engine(Mutex<Option<Child>>);
static ENGINE_PHOTO: Mutex<Option<bool>> = Mutex::new(None);   // the running engine's photo setting

// one indexing run at a time: the background refresh and a run started from the app's screens
static INDEXING: std::sync::atomic::AtomicBool = std::sync::atomic::AtomicBool::new(false);
static REFRESHER: std::sync::atomic::AtomicBool = std::sync::atomic::AtomicBool::new(false);
// the person asked for an indexing run: background runs give way (a refresh or a deep run could hold the turn for hours on a
// CPU laptop while the person's '바뀐 파일 반영' waited, and an upgrade waiting for the turn could take it first)
static PERSON: std::sync::atomic::AtomicBool = std::sync::atomic::AtomicBool::new(false);
static BG_RUN: Mutex<Option<u32>> = Mutex::new(None);   // the background refresh or deep run now going

// the language of the messages and the menu made here: the system's at the start, then what the screens say (`set_lang`)
static KOREAN: std::sync::atomic::AtomicBool = std::sync::atomic::AtomicBool::new(false);
// the screens have said the language: from then on the programs started here are told it (FINDR_LANG). Before that they
// read the system's language themselves (on Windows the display language, which is not read here)
static LANG_SAID: std::sync::atomic::AtomicBool = std::sync::atomic::AtomicBool::new(false);

/// Korean for a Korean system, English for everyone else (LC_ALL, LC_MESSAGES, LANG; a PC that sets none is told by the screens)
fn system_korean() -> bool {
    ["LC_ALL", "LC_MESSAGES", "LANG"].iter().filter_map(|k| std::env::var(k).ok()).find(|v| !v.is_empty())
        .map(|v| v.to_lowercase().starts_with("ko")).unwrap_or(false)
}

/// the message in the app's language
fn l(ko: &str, en: &str) -> String {
    if KOREAN.load(std::sync::atomic::Ordering::Relaxed) { ko.to_string() } else { en.to_string() }
}

/// the screens tell the language the person chose ('ko' or 'en'); the menu is made again in it
#[tauri::command]
fn set_lang(app: AppHandle, lang: String) {
    let ko = lang == "ko";
    let _ = std::fs::write(home().join("lang"), if ko { "ko" } else { "en" }); // the search page and the next start see it
    use_lang(&app, ko);
}

/// the language chosen on either screen: the app screen tells it (set_lang), the search page is served by the engine and
/// cannot, so the engine writes the file `lang` and the app looks at it every few seconds (watch_lang)
fn use_lang(app: &AppHandle, ko: bool) {
    LANG_SAID.store(true, std::sync::atomic::Ordering::Relaxed);
    if KOREAN.swap(ko, std::sync::atomic::Ordering::Relaxed) != ko {
        let _ = set_app_menu(app);
    }
}

/// the language last chosen on either screen ('ko', 'en', or '' before any choice): the app screen starts in it
#[tauri::command]
fn saved_lang_choice() -> String {
    match saved_lang() {
        Some(true) => "ko".into(),
        Some(false) => "en".into(),
        None => String::new(),
    }
}

fn saved_lang() -> Option<bool> {
    match std::fs::read_to_string(home().join("lang")).ok()?.trim() {
        "ko" => Some(true),
        "en" => Some(false),
        _ => None,
    }
}

fn watch_lang(app: AppHandle) {
    thread::spawn(move || {
        let mut last = saved_lang();
        loop {
            thread::sleep(std::time::Duration::from_secs(3));
            let now = saved_lang();
            if now != last {
                if let Some(ko) = now {
                    use_lang(&app, ko);
                }
                last = now;
            }
        }
    });
}

fn set_app_menu(h: &AppHandle) -> tauri::Result<()> {
    let search = MenuItem::with_id(h, "search", l("검색", "Search"), true, Some("CmdOrCtrl+F"))?;
    let settings = MenuItem::with_id(h, "settings", l("폴더와 설정", "Folders and Settings"), true, Some("CmdOrCtrl+,"))?;
    let data = MenuItem::with_id(h, "data", l("데이터 폴더 열기", "Open Data Folder"), true, None::<&str>)?;
    let quit = MenuItem::with_id(h, "quit", l("끝내기", "Quit"), true, Some("CmdOrCtrl+Q"))?;
    let sep = PredefinedMenuItem::separator(h)?;
    let file = Submenu::with_items(h, "findr", true, &[&search, &settings, &data, &sep, &quit])?;
    let edit = Submenu::with_items(h, l("편집", "Edit"), true, &[
        &PredefinedMenuItem::undo(h, None)?, &PredefinedMenuItem::redo(h, None)?, &PredefinedMenuItem::separator(h)?,
        &PredefinedMenuItem::cut(h, None)?, &PredefinedMenuItem::copy(h, None)?, &PredefinedMenuItem::paste(h, None)?,
        &PredefinedMenuItem::select_all(h, None)?,
    ])?;
    h.set_menu(Menu::with_items(h, &[&file, &edit])?)?;
    Ok(())
}

fn person_waiting() -> bool {
    PERSON.load(std::sync::atomic::Ordering::SeqCst)
}

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
        // BatteryStatus per battery: 1 discharging, 4 low, 5 critical = on battery; 2 AC, 3 full, 6-9 charging, 11 partly
        // charged (reported on AC too). Only "2" counted as AC before: two batteries ("2\r\n2"), a full or a charging battery
        // kept background indexing off on a plugged-in laptop. No battery at all (a desktop) prints nothing.
        let out = command("powershell").args(["-NoProfile", "-Command", "(Get-CimInstance Win32_Battery).BatteryStatus"]).output();
        if let Ok(o) = out {
            let codes: Vec<u32> = String::from_utf8_lossy(&o.stdout).split_whitespace().filter_map(|x| x.parse().ok()).collect();
            return !codes.is_empty() && codes.iter().all(|c| matches!(c, 1 | 4 | 5));
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
    // out of the lock first: the kill waits on taskkill, and the upgrader's own loop takes the lock every 2 s
    let taken = UPGRADING.lock().unwrap().take();
    if let Some(mut c) = taken {
        kill_tree(c.id()); // with the index.py it started: killing the upgrade alone left that running
        let _ = c.kill();
        let _ = c.wait();
    }
}

/// a PC that started on the small meaning model moves to the larger one while nobody uses it (plugged in, 5 minutes
/// without input), 10 minutes at a time; search keeps working with the small model until the switch
/// the upgrade is off because the larger model is slow on this PC (cli.py _too_slow): not the person's choice. The run
/// is still asked in idle time, since it may read each file's first piece alone (the second meaning lane)
fn slow_off(h: &std::path::Path) -> bool {
    std::fs::read_to_string(h.join("upgrade_off")).map(|s| s.starts_with("이 PC에서") || s.starts_with("이 기기에서")).unwrap_or(false)
}

fn start_upgrader() {
    thread::spawn(|| {
        let mut last_run = std::time::Instant::now();
        let mut filled: Option<std::time::Instant> = None; // every first piece was there: looked at again a day later (new files)
        {
            // judged too slow by an earlier version, which then stopped asking: asked once more by this one
            let h = home();
            if slow_off(&h) && std::fs::read_to_string(h.join(".upgrade-done")).map(|v| v != VERSION).unwrap_or(false) {
                let _ = std::fs::remove_file(h.join(".upgrade-done"));
            }
        }
        thread::sleep(std::time::Duration::from_secs(900));
        loop {
            let h = home();
            let idle = match idle_seconds() {
                Some(s) => s >= 300,
                None => last_run.elapsed().as_secs() >= 3000, // cannot tell: at most one short run an hour
            };
            let rested = filled.map(|t| t.elapsed().as_secs() >= 86400).unwrap_or(true);
            if h.join(".deep-done").exists() && !h.join(".upgrade-done").exists() && (!h.join("upgrade_off").exists() || slow_off(&h)) && rested && idle && !on_battery() {
                take_index_turn();
                if person_waiting() || on_battery() {
                    give_index_turn(); // waited for the turn: the person's run first, and still on mains power
                    thread::sleep(std::time::Duration::from_secs(600));
                    continue;
                }
                let half = std::thread::available_parallelism().map(|n| (n.get() / 2).max(1)).unwrap_or(2);
                let mut c = low_priority(python());
                c.env("FINDR_THREADS", half.to_string()).env("OMP_NUM_THREADS", half.to_string());
                // the person came back (keyboard or mouse in the last minute): the run stops and keeps what it computed
                c.env("FINDR_STOP_ON_INPUT", "60");
                c.args(["-m", "findr", "upgrade", "--minutes", "10"]);
                let log = std::fs::OpenOptions::new().create(true).append(true).open(h.join("logs").join("upgrade.log"));
                own_group(&mut c);
                let code = match log.map(|f| c.stdout(f).stderr(Stdio::null()).stdin(Stdio::null()).spawn()) {
                    Ok(Ok(child)) => {
                        let pid = child.id();
                        track(pid);
                        *UPGRADING.lock().unwrap() = Some(child);
                        let code = loop {
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
                        };
                        untrack(pid);
                        code
                    }
                    _ => None,
                };
                give_index_turn();
                last_run = std::time::Instant::now();
                // 0 switched, 3 not for this PC: done. Anything else (no index yet, disk full, a dropped download, an error) is
                // tried again later: exit code 1 used to end the upgrade for good and hide its switch in the settings
                if code == Some(0) || code == Some(3) {
                    let _ = std::fs::write(h.join(".upgrade-done"), VERSION);
                }
                if code == Some(6) {
                    filled = Some(std::time::Instant::now());
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

#[tauri::command(async)]   // turning it off stops a running upgrade: a taskkill, not on the window's thread
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

/// after the search page is up: changed files now and every 10 minutes (#106; names only on battery), at once when the
/// person presses '지금 반영' on the search page (the `refresh_now` file), and once the later pieces of long documents
fn start_refresher() {
    use std::sync::atomic::Ordering;
    if REFRESHER.swap(true, Ordering::SeqCst) {
        return;
    }
    start_upgrader();
    thread::spawn(|| {
        let started = std::time::Instant::now();
        let mut last_refresh: Option<std::time::Instant> = None;
        let mut last_look: Option<std::time::Instant> = None;
        let asked = || home().join("refresh_now").exists();
        loop {
        // a cheap look every 5 seconds for '지금 반영'; the rest (battery, idle time) every 10 minutes
        let now_asked = asked();
        if !now_asked && last_look.map(|t| t.elapsed().as_secs() < 600).unwrap_or(false) {
            thread::sleep(std::time::Duration::from_secs(5));
            continue;
        }
        last_look = Some(std::time::Instant::now());
        let folders = load_folders();
        if !folders.is_empty() && home().join("index.db").exists() {
            // on battery new files still come in by name (an hour without them, or none at all unplugged, read as broken);
            // their content, the meaning vectors and the deep run wait for the charger. '지금 반영' reads them in full.
            let battery = on_battery();
            // the hourly refresh missed files saved minutes ago; with #104 a run that finds nothing new costs ~3 s
            let refresh_due = now_asked || last_refresh.map(|t| t.elapsed().as_secs() >= 600).unwrap_or(true);
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
                if (deep && (battery || fresh || !idle || asked())) || (!deep && !refresh_due) {
                    continue;
                }
                if !deep {
                    last_refresh = Some(std::time::Instant::now());
                }
                let names_only = !deep && battery && !now_asked;
                if now_asked && !deep {
                    stop_upgrade(); // the person asked: the model upgrade gives way (it keeps what it computed)
                }
                take_index_turn();
                if person_waiting() {
                    give_index_turn(); // the person's run first; this one comes round again in 10 minutes
                    continue;
                }
                if !deep {
                    let _ = std::fs::remove_file(home().join("refresh_now")); // taken: a press during this run asks again
                }
                let log = std::fs::File::create(home().join("logs").join(if deep { "deep.log" } else { "refresh.log" }));
                let mut c = low_priority(python());
                let half = std::thread::available_parallelism().map(|n| (n.get() / 2).max(1)).unwrap_or(2);
                c.env("FINDR_THREADS", half.to_string()).env("OMP_NUM_THREADS", half.to_string());   // leave cores for the person
                c.env("FINDR_SCOPE", "user").args(["-m", "findr", "index"]).args(&folders);   // the person's files: removed folders leave the index
                if deep {
                    c.arg("--deep");
                }
                if names_only {
                    c.arg("--names-only");
                }
                own_group(&mut c);
                let ok = match log.map(|f| c.stdout(f).stderr(Stdio::null()).stdin(Stdio::null()).spawn()) {
                    Ok(Ok(mut child)) => {
                        track(child.id());
                        *BG_RUN.lock().unwrap() = Some(child.id());
                        // a deep run takes hours: '지금 반영' pressed meanwhile stops it (it keeps what it read and runs again later)
                        let ok = loop {
                            match child.try_wait() {
                                Ok(Some(s)) => break s.success(),
                                Ok(None) => {
                                    if deep && asked() {
                                        kill_tree(child.id());
                                        let _ = child.kill();
                                        let _ = child.wait();
                                        break false;
                                    }
                                    thread::sleep(std::time::Duration::from_secs(2));
                                }
                                Err(_) => break false,
                            }
                        };
                        *BG_RUN.lock().unwrap() = None;
                        untrack(child.id());
                        ok
                    }
                    _ => false,
                };
                give_index_turn();
                if deep && ok {
                    let _ = std::fs::write(home().join(".deep-done"), VERSION);
                }
                if !deep && ok {
                    mark_refreshed();
                }
            }
        }
        // always a pause: a press left waiting behind the person's own run must not take and give the turn in a tight loop
        // (the person's run polls every 2 s and would never get it)
        thread::sleep(std::time::Duration::from_secs(5));
        }
    });
}

/// when new files were last taken in, for the search page's '마지막 반영 HH:MM' (#106)
fn mark_refreshed() {
    let t = std::time::SystemTime::now().duration_since(std::time::UNIX_EPOCH).map(|d| d.as_secs()).unwrap_or(0);
    let _ = std::fs::write(home().join("last_refresh"), t.to_string());
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
    // asked once per app run: app_state ran nvidia-smi on every screen change
    static GPU: std::sync::OnceLock<&'static str> = std::sync::OnceLock::new();
    GPU.get_or_init(|| {
        if cfg!(target_os = "macos") {
            return "apple";
        }
        let ok = command("nvidia-smi").arg("-L").stdout(Stdio::null()).stderr(Stdio::null()).status().map(|s| s.success()).unwrap_or(false);
        if ok { "nvidia" } else { "cpu" }
    })
}

// every child process: no console window on Windows, UTF-8 and unbuffered output for the engine
fn command(prog: impl AsRef<std::ffi::OsStr>) -> Command {
    let mut c = Command::new(prog);
    c.env("PYTHONUTF8", "1").env("PYTHONUNBUFFERED", "1").env("FINDR_HOME", home())
        .env("UV_NATIVE_TLS", "1");   // company networks that inspect TLS: trust the system's certificates
    if LANG_SAID.load(std::sync::atomic::Ordering::Relaxed) {
        c.env("FINDR_LANG", if KOREAN.load(std::sync::atomic::Ordering::Relaxed) { "ko" } else { "en" });
    }
    #[cfg(windows)]
    {
        use std::os::windows::process::CommandExt;
        c.creation_flags(0x0800_0000); // CREATE_NO_WINDOW
    }
    c
}

// ---------- the app's own runs: indexing, refresh, deep run, upgrade, installs ----------
// Each runs in its own process group and is remembered here and in runs.pid: it stops with the app, and a run that a killed
// app left behind stops at the next start. Field (Mac): quitting during indexing left the indexer running, the next start
// added one more, three wrote to one index at once and the search engine reached 20 GB with 52 s answers.
static RUNS: Mutex<Vec<u32>> = Mutex::new(Vec::new());

fn own_group(c: &mut Command) {
    #[cfg(unix)]
    {
        use std::os::unix::process::CommandExt;
        c.process_group(0);
    }
    #[cfg(not(unix))]
    let _ = c;
}

fn runs_file() -> PathBuf {
    home().join("runs.pid")
}

fn save_runs(v: &[u32]) {
    let _ = std::fs::write(runs_file(), v.iter().map(|p| p.to_string()).collect::<Vec<_>>().join("\n"));
}

fn track(pid: u32) {
    let mut g = RUNS.lock().unwrap();
    g.push(pid);
    save_runs(&g);
}

fn untrack(pid: u32) {
    let mut g = RUNS.lock().unwrap();
    g.retain(|p| *p != pid);
    save_runs(&g);
}

/// end a run and everything it started (the indexer starts index.py, the engine its helper servers)
fn kill_tree(pid: u32) {
    #[cfg(windows)]
    {
        let _ = command("taskkill").args(["/PID", &pid.to_string(), "/T", "/F"]).status();
    }
    #[cfg(not(windows))]
    {
        let grp = format!("-{pid}");
        let _ = Command::new("kill").args(["-TERM", "--", &grp]).status();
        for _ in 0..30 {
            if !Command::new("kill").args(["-0", "--", &grp]).stderr(Stdio::null()).status().map(|s| s.success()).unwrap_or(false) {
                return;
            }
            thread::sleep(std::time::Duration::from_millis(100));
        }
        let _ = Command::new("kill").args(["-KILL", "--", &grp]).status();
    }
}

/// with the app: every run it started
fn stop_runs() {
    let pids: Vec<u32> = RUNS.lock().unwrap().drain(..).collect();
    for p in pids {
        kill_tree(p);
    }
    let _ = std::fs::remove_file(runs_file());
}

/// at start: the runs of an app that was killed. Only findr's own processes: a number can belong to another program after
/// a restart of the PC.
fn reap_left_runs() {
    let Ok(s) = std::fs::read_to_string(runs_file()) else { return };
    for pid in s.lines().filter_map(|l| l.trim().parse::<u32>().ok()) {
        if is_findr_run(pid) {
            kill_tree(pid);
        }
    }
    let _ = std::fs::remove_file(runs_file());
}

fn is_findr_run(pid: u32) -> bool {
    #[cfg(windows)]
    {
        let q = format!("(Get-CimInstance Win32_Process -Filter 'ProcessId={pid}').CommandLine");
        command("powershell").args(["-NoProfile", "-NonInteractive", "-Command", &q]).output()
            .map(|o| String::from_utf8_lossy(&o.stdout).to_lowercase().contains("findr"))   // every run names findr or its folder
            .unwrap_or(false)
    }
    #[cfg(not(windows))]
    {
        // any process of the group whose command line names findr (the group leader may be gone already)
        Command::new("pgrep").args(["-g", &pid.to_string(), "-f", "findr"]).output()
            .map(|o| o.status.success() && !o.stdout.is_empty()).unwrap_or(false)
    }
}

/// run a command, sending each output line to the window as `event`; true when it succeeded
fn stream(app: &AppHandle, event: &str, step: &str, mut cmd: Command) -> bool {
    own_group(&mut cmd);
    let child = cmd.stdout(Stdio::piped()).stderr(Stdio::piped()).stdin(Stdio::null()).spawn();
    let mut child = match child {
        Ok(c) => c,
        Err(e) => {
            send(app, event, Line { step: step.into(), line: l(&format!("실행하지 못했습니다: {e}"), &format!("Could not run: {e}")) });
            return false;
        }
    };
    let pid = child.id();
    track(pid);
    let mut joins = vec![];
    for r in [child.stdout.take().map(|s| Box::new(s) as Box<dyn std::io::Read + Send>),
              child.stderr.take().map(|s| Box::new(s) as Box<dyn std::io::Read + Send>)].into_iter().flatten() {
        let (app, event, step) = (app.clone(), event.to_string(), step.to_string());
        joins.push(thread::spawn(move || {
            for line in BufReader::new(r).lines().map_while(Result::ok) {
                let line = line.trim().to_string();
                if !line.is_empty() {
                    send(&app, &event, Line { step: step.clone(), line });
                }
            }
        }));
    }
    for j in joins {
        let _ = j.join();
    }
    let ok = child.wait().map(|s| s.success()).unwrap_or(false);
    untrack(pid);
    ok
}

#[derive(Clone, Serialize)]
struct Line {
    step: String,
    line: String,
}

// the first-run setup: one at a time, and what it has said so far. A screen drawn again in the middle of it (the menu's
// '검색' or '폴더와 설정' loads the page anew) went back to the welcome screen, and '준비 시작' there started a second setup
// beside the first: two installs into one environment, one deleting the engine files the other was installing from
// (field, Windows). Now the screen joins the setup already running and shows what it said before.
static SETUP_RUNNING: std::sync::atomic::AtomicBool = std::sync::atomic::AtomicBool::new(false);
static SETUP_LOG: Mutex<Vec<Line>> = Mutex::new(Vec::new());
static SETUP_LAST: Mutex<Option<Done>> = Mutex::new(None);   // how the last setup ended, for a screen drawn as it ended

/// send a progress line to the window; a setup line is also kept, for a screen that joins later
fn send(app: &AppHandle, event: &str, line: Line) {
    if event == "setup" {
        let mut g = SETUP_LOG.lock().unwrap();
        g.push(line.clone());
        if g.len() > 300 {
            g.remove(0);
        }
    }
    let _ = app.emit(event, line);
}

/// the setup now going, for a screen drawn in the middle of it: whether it runs and the lines it has sent
#[tauri::command]
fn setup_status() -> serde_json::Value {
    serde_json::json!({
        "running": SETUP_RUNNING.load(std::sync::atomic::Ordering::SeqCst),
        "lines": SETUP_LOG.lock().unwrap().clone(),
        "done": SETUP_LAST.lock().unwrap().clone(),
    })
}

#[derive(Clone, Serialize)]
struct Done {
    ok: bool,
    message: String,
}

// ---------- commands ----------

/// a fingerprint of the engine this app carries: when it differs from the installed one, the engine is updated
fn engine_stamp(app: &AppHandle) -> String {
    // the engine inside the app does not change while it runs: hashed once (app_state asked on every screen change)
    static STAMP: std::sync::OnceLock<String> = std::sync::OnceLock::new();
    STAMP.get_or_init(|| engine_stamp_now(app)).clone()
}

fn engine_stamp_now(app: &AppHandle) -> String {
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
    setup_running: bool,
}

// commands that wait (a process, a file walk) run off the main thread: a plain command runs on it, and the window froze
// ('응답 없음', the spinning wheel) for the minute an engine takes to start on a CPU laptop
#[tauri::command(async)]
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
        setup_running: SETUP_RUNNING.load(std::sync::atomic::Ordering::SeqCst),
    }
}

/// first run: private Python environment -> PyTorch for this GPU -> the engine -> models. Progress goes out as `setup` events.
#[tauri::command]
fn setup_runtime(app: AppHandle) {
    // one setup at a time: a second request while one runs only joins it (the screen asks setup_status for what it missed)
    if SETUP_RUNNING.compare_exchange(false, true, std::sync::atomic::Ordering::SeqCst, std::sync::atomic::Ordering::SeqCst).is_err() {
        return;
    }
    SETUP_LOG.lock().unwrap().clear();
    *SETUP_LAST.lock().unwrap() = None;
    thread::spawn(move || {
        // also when the setup ends some other way (a panic): never left 'running' for good. Only then, though: after
        // done() a '다시 시도' can start the next run before this thread is gone, and this one must not end that one's flag
        let finished = std::cell::Cell::new(false);
        struct Running<'a>(&'a std::cell::Cell<bool>);
        impl Drop for Running<'_> {
            fn drop(&mut self) {
                if !self.0.get() {
                    SETUP_RUNNING.store(false, std::sync::atomic::Ordering::SeqCst);
                }
            }
        }
        let _running = Running(&finished);
        let done = |ok: bool, message: &str| {
            finished.set(true);
            let d = Done { ok, message: message.into() };
            *SETUP_LAST.lock().unwrap() = Some(d.clone());   // kept before 'running' ends: a screen never sees neither
            SETUP_RUNNING.store(false, std::sync::atomic::Ordering::SeqCst);
            let _ = app.emit("setup-done", d);
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
            return done(false, &l("파이썬 환경을 만들지 못했습니다. 인터넷 연결을 확인하고 다시 시도해 주세요.", "Could not create the Python environment. Check the internet connection and try again."));
        }
        // 2. PyTorch for this PC
        send(&app, "setup", Line { step: "torch".into(), line: l("이 PC에 맞는 연산 라이브러리를 고르는 중", "Choosing the compute library for this PC") });
        let mut c = command(uv());
        c.args(["pip", "install", "--python"]).arg(python()).args(["torch", "torchvision"]);
        match gpu() {
            "nvidia" => { c.args(["--index-url", "https://download.pytorch.org/whl/cu128"]); }
            "cpu" => { c.args(["--index-url", "https://download.pytorch.org/whl/cpu"]); }
            _ => {}
        }
        if !stream(&app, "setup", "torch", c) {
            return done(false, &l("PyTorch를 설치하지 못했습니다. 인터넷 연결과 디스크 여유 공간을 확인해 주세요.", "Could not install PyTorch. Check the internet connection and the free disk space."));
        }
        // 3. the engine (copied out of the app first: the app folder can be read-only)
        send(&app, "setup", Line { step: "engine".into(), line: l("검색 엔진 파일을 준비하는 중", "Preparing the search engine files") });
        let src = match engine_source(&app) {
            Some(p) => p,
            None => return done(false, &l("앱 안에서 검색 엔진 파일을 찾지 못했습니다. 앱을 다시 설치해 주세요.", "The search engine files are missing from the app. Please install the app again.")),
        };
        let dst = home().join("engine-src");
        let _ = std::fs::remove_dir_all(&dst);
        if let Err(e) = copy_dir(&src, &dst) {
            return done(false, &l(&format!("엔진 파일을 준비하지 못했습니다: {e}"), &format!("Could not prepare the engine files: {e}")));
        }
        // an update needs nothing new most of the time: from uv's cache first, so an outage of the download servers does not
        // stop it (2026-11-08: GitHub answered 504 for a dependency wheel and the engine stayed on the old version)
        let mut off = command(uv());
        off.args(["pip", "install", "--offline", "--reinstall-package", "findr-ai", "--python"]).arg(python()).arg(&dst);
        let mut c = command(uv());
        c.args(["pip", "install", "--reinstall-package", "findr-ai", "--python"]).arg(python()).arg(&dst);
        // a second source for the one pinned wheel the engine does not get from PyPI: the same file (same hash) on GitHub,
        // used only when the first source (Hugging Face, in pyproject.toml) fails
        let alt = home().join("deps-alt.txt");
        let _ = std::fs::write(&alt, ALT_DEPS);
        let mut c2 = command(uv());
        c2.env("UV_OVERRIDE", &alt)
            .args(["pip", "install", "--reinstall-package", "findr-ai", "--python"]).arg(python()).arg(&dst);
        if !stream(&app, "setup", "engine", off) && !stream(&app, "setup", "engine", c)
            && !stream(&app, "setup", "engine", c2) {
            return done(false, &l("검색 엔진을 설치하지 못했습니다.", "Could not install the search engine."));
        }
        swap_onnx_runtime();
        // 4. models for this PC
        send(&app, "setup", Line { step: "models".into(), line: l("이 PC에 맞는 모델을 고르는 중", "Choosing the models for this PC") });
        let mut c = command(python());
        c.args(["-m", "findr", "setup"]);
        if !stream(&app, "setup", "models", c) {
            return done(false, &l("구성 요소를 받거나 불러오지 못했습니다. 아래 '자세한 기록'의 마지막 줄을 확인하고 다시 시도해 주세요.", "Could not download or load the components. Check the last line of 'Details' below and try again."));
        }
        let _ = std::fs::write(ready_mark(), engine_stamp(&app));
        done(true, &l("준비가 끝났습니다.", "Setup is complete."));
    });
}

/// Windows: the engine now uses onnxruntime's DirectML build (the reranker can run on an AMD or Intel GPU, gpu_dml.py). Both
/// builds install the same `onnxruntime` module folder, so an update from an older engine leaves the plain one beside it, and
/// removing it later would take shared files with it. Once the new build is in uv's cache (the install above just fetched
/// it): remove the plain one, then put the DirectML one back from the cache. Nothing happens on a fresh install.
fn swap_onnx_runtime() {
    #[cfg(windows)]
    {
        let mut chk = command(python());
        chk.args(["-c", "import importlib.metadata as m; m.version('onnxruntime')"]);
        let plain = chk.stdout(Stdio::null()).stderr(Stdio::null()).status().map(|s| s.success()).unwrap_or(false);
        if !plain {
            return;
        }
        let mut u = command(uv());
        let _ = u.args(["pip", "uninstall", "--python"]).arg(python()).arg("onnxruntime").stdout(Stdio::null()).stderr(Stdio::null()).status();
        for offline in [true, false] {
            let mut r = command(uv());
            r.args(["pip", "install", "--reinstall-package", "onnxruntime-directml", "--python"]).arg(python()).arg("onnxruntime-directml");
            if offline {
                r.arg("--offline");
            }
            if r.stdout(Stdio::null()).stderr(Stdio::null()).status().map(|s| s.success()).unwrap_or(false) {
                return;
            }
        }
    }
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
    let say = |line: &str| { send(&app, "setup", Line { step: "vcrt".into(), line: line.into() }); };
    if !cfg!(windows) {
        say(&l("필요 없음", "Not needed"));
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
        say(&l(&format!("Visual C++ 런타임 {ma}.{mi}: 준비됨"), &format!("Visual C++ runtime {ma}.{mi}: ready")));
        return Ok(());
    }
    say(&if ma == 0 {
        l("Visual C++ 런타임이 없어 설치합니다 (권한 확인 창이 뜨면 '예')", "Installing the Visual C++ runtime, which is missing (choose 'Yes' if Windows asks for permission)")
    } else {
        l("Visual C++ 런타임이 오래되어 업데이트합니다 (권한 확인 창이 뜨면 '예')", "Updating the Visual C++ runtime, which is too old (choose 'Yes' if Windows asks for permission)")
    });
    let exe = std::env::temp_dir().join("vc_redist.x64.exe");
    let script = format!(
        "Invoke-WebRequest -UseBasicParsing -Uri https://aka.ms/vs/17/release/vc_redist.x64.exe -OutFile '{0}';          Start-Process -FilePath '{0}' -ArgumentList '/install','/quiet','/norestart' -Verb RunAs -Wait",
        exe.display().to_string().replace('\'', "''")); // a user folder with an apostrophe (O'Brien) broke the quoting
    let _ = command("powershell").args(["-NoProfile", "-Command", &script]).status();
    let (ma, mi) = version();
    if ma > 14 || (ma == 14 && mi >= 40) {
        say(&l(&format!("Visual C++ 런타임 {ma}.{mi}: 설치됨"), &format!("Visual C++ runtime {ma}.{mi}: installed")));
        Ok(())
    } else {
        Err(l("Windows 구성 요소(Visual C++ 런타임)를 설치하지 못했습니다. https://aka.ms/vs/17/release/vc_redist.x64.exe 를 받아 설치한 뒤 다시 시도해 주세요.", "Could not install a Windows component (the Visual C++ runtime). Download and install https://aka.ms/vs/17/release/vc_redist.x64.exe , then try again."))
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
#[tauri::command(async)]
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
    let s = std::fs::read_to_string(home().join("progress.json")).unwrap_or_default();
    // paused is the pause file itself: a paused indexer writes no progress, so the screen said '진행 중' after a restart
    match serde_json::from_str::<serde_json::Value>(&s) {
        Ok(serde_json::Value::Object(mut m)) => {
            // pausing: asked, and the indexer has not said it holds yet (it finishes the file it is reading: up to 14 s, #66-10)
            let asked = home().join("pause").exists();
            let held = m.get("paused").and_then(|v| v.as_bool()).unwrap_or(false);
            m.insert("pausing".into(), serde_json::Value::Bool(asked && !held));
            m.insert("paused".into(), serde_json::Value::Bool(asked));
            serde_json::Value::Object(m).to_string()
        }
        _ => s,
    }
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
        let _ = app.emit("index", Line { step: "index".into(), line: l("앞선 색인 작업이 끝나기를 기다리는 중", "Waiting for the earlier indexing run to finish") });
        PERSON.store(true, std::sync::atomic::Ordering::SeqCst);
        stop_upgrade(); // the person's own run comes first (the upgrade keeps what it computed)
        if let Some(pid) = *BG_RUN.lock().unwrap() {
            kill_tree(pid); // a background refresh or deep run gives way; it runs again later and keeps what it read
        }
        let _ = std::fs::remove_file(home().join("pause"));
        take_index_turn();
        PERSON.store(false, std::sync::atomic::Ordering::SeqCst);
        let _ = std::fs::remove_file(home().join("progress.json")); // a fresh picture for this run, once the turn is ours
        // below normal priority and half the cores, like the background runs: the first index runs right after setup, while
        // the person goes on with a video or a browser. At normal priority with every core it froze a 16 GB laptop (field,
        // 11-13). An otherwise idle PC still gives it the whole CPU
        let mut c = low_priority(python());
        let half = std::thread::available_parallelism().map(|n| (n.get() / 2).max(1)).unwrap_or(2);
        c.env("FINDR_THREADS", half.to_string()).env("OMP_NUM_THREADS", half.to_string());
        c.env("FINDR_SCOPE", "user").args(["-m", "findr", "index"]).args(&folders);   // the person's files: removed folders leave the index
        if deep.unwrap_or(false) {
            c.arg("--deep"); // the later pieces of long documents too
        }
        let ok = stream(&app, "index", "index", c);
        give_index_turn();
        if ok {
            mark_refreshed();
        }
        if deep.unwrap_or(false) && ok {
            let _ = std::fs::write(home().join(".deep-done"), VERSION);
        }
        let _ = app.emit("index-done", Done { ok, message: if ok { l("색인을 마쳤습니다.", "Indexing is complete.") } else { l("색인 중 오류가 있었습니다.", "There was an error during indexing.") } });
    });
}

/// one engine start at a time. A second start while the first was still waiting for its engine (about a minute on a CPU
/// laptop: the 검색 menu pressed again, '검색으로 돌아가기' running the screen twice) found no engine yet and started another
/// one beside it: two copies of the models in memory, and the copy stored last replaced the first without stopping it, so
/// that one kept the port and its memory, even after the app had quit
static STARTING: Mutex<()> = Mutex::new(());
static WATCHING: std::sync::atomic::AtomicBool = std::sync::atomic::AtomicBool::new(false);
/// how long an engine may take to say it is up. Past this the start is given up and said, rather than a spinner for good
const ENGINE_START_SECS: u64 = 180;

/// start (or restart) the engine; returns the address that signs the window in
#[tauri::command(async)]
fn start_engine(app: AppHandle, engine: State<'_, Engine>, photo: bool) -> Result<String, String> {
    let _turn = STARTING.lock().unwrap_or_else(|e| e.into_inner());   // a second start waits here, then finds this one's engine
    // the engine already answering with the same photo setting: back to search without a restart (every return from the
    // settings screen, the 검색 menu and Ctrl+F reloaded all the models). Answering, not only running: a process that is
    // still there but no longer answers (stuck, or swapping on a full PC) left the page waiting for good
    let alive = {
        let mut g = engine.0.lock().unwrap();
        g.as_mut().map(|c| matches!(c.try_wait(), Ok(None))).unwrap_or(false)
    };
    if alive && *ENGINE_PHOTO.lock().unwrap() == Some(photo) && engine_answers() {
        if let Ok(key) = std::fs::read_to_string(home().join(".password")) {
            return Ok(format!("http://127.0.0.1:{}/auth?t={}", port(), key.trim()));
        }
    }
    stop(&engine);
    *ENGINE_PHOTO.lock().unwrap() = Some(photo);
    let mut c = command(python());
    c.args(["-m", "findr", "serve", "--app", "--no-browser", "--port", &port().to_string()]);
    if !photo {
        c.arg("--no-photo");
    }
    // the engine ends itself when this app is gone (killed, crashed): it used to stay with its models in memory and its port,
    // and the next start of the app could not start one of its own
    c.env("FINDR_PARENT", std::process::id().to_string());
    own_group(&mut c);
    let logs = home().join("logs");
    let _ = std::fs::create_dir_all(&logs);
    // the last engine's log is kept: an engine that ended on its own said why there, and the restart overwrote it
    let _ = std::fs::rename(logs.join("engine.log"), logs.join("engine.prev.log"));
    let log = std::fs::File::create(logs.join("engine.log")).map_err(|e| e.to_string())?;
    let mut child = c.stdout(Stdio::piped()).stderr(log).stdin(Stdio::null()).spawn().map_err(|e| e.to_string())?;
    track(child.id());   // in runs.pid: an engine that a killed app left behind is stopped at the next start
    let out = child.stdout.take();
    let (tx, rx) = std::sync::mpsc::channel::<()>();
    if let Some(out) = out {
        // reads to the end, which also keeps the pipe drained after the ready line
        thread::spawn(move || {
            let mut tx = Some(tx);
            for line in BufReader::new(out).lines().map_while(Result::ok) {
                if line.starts_with("FINDR_APP_READY") {
                    if let Some(t) = tx.take() {
                        let _ = t.send(());
                    }
                }
            }
        });
    }
    // an engine that ends before it is up drops the sender, so this returns at once; one that hangs, at the deadline
    let wait = rx.recv_timeout(std::time::Duration::from_secs(ENGINE_START_SECS));
    keep(&engine, child);
    if let Err(e) = wait {
        stop(&engine);
        return Err(match e {
            std::sync::mpsc::RecvTimeoutError::Timeout => l("검색 엔진이 3분 안에 켜지지 않았습니다. PC가 바쁘면 다른 프로그램을 몇 개 닫고 다시 시도해 주세요. 자세한 내용은 데이터 폴더의 logs/engine.log에 있습니다.",
                                                             "The search engine did not start within 3 minutes. If the PC is busy, close a few other programs and try again. Details are in logs/engine.log in the data folder."),
            std::sync::mpsc::RecvTimeoutError::Disconnected => l("검색 엔진이 시작되지 않았습니다. 데이터 폴더의 logs/engine.log를 확인해 주세요.", "The search engine did not start. Check logs/engine.log in the data folder."),
        });
    }
    start_refresher();
    watch_engine(app);
    let key = std::fs::read_to_string(home().join(".password")).map_err(|e| e.to_string())?;
    Ok(format!("http://127.0.0.1:{}/auth?t={}", port(), key.trim()))
}

/// the engine answers its /healthz (no sign-in, no search lock): two tries, since a restart costs a minute of warm-up and a
/// busy engine can be slow to answer once
fn engine_answers() -> bool {
    use std::io::{Read, Write};
    use std::time::Duration;
    let addr = std::net::SocketAddr::from(([127, 0, 0, 1], port()));
    for _ in 0..2 {
        if let Ok(mut s) = std::net::TcpStream::connect_timeout(&addr, Duration::from_secs(3)) {
            let _ = s.set_read_timeout(Some(Duration::from_secs(4)));
            let _ = s.set_write_timeout(Some(Duration::from_secs(2)));
            let mut buf = [0u8; 64];
            if s.write_all(b"GET /healthz HTTP/1.0\r\nHost: 127.0.0.1\r\n\r\n").is_ok() {
                if let Ok(n) = s.read(&mut buf) {
                    let head = String::from_utf8_lossy(&buf[..n]).to_string();
                    if head.starts_with("HTTP/") && head.split_whitespace().nth(1) == Some("200") {
                        return true;
                    }
                }
            }
        }
        thread::sleep(Duration::from_millis(500));
    }
    false
}

/// the engine now running. One already stored is ended, never dropped: a dropped Child keeps running, untracked
fn keep(engine: &Engine, child: Child) {
    let old = engine.0.lock().unwrap().replace(child);
    if let Some(mut o) = old {
        end_engine(&mut o);
    }
}

/// an engine that ends on its own (the system stopped it for memory, a crash): the search page only said it could not
/// connect and nothing started it again. Seen here within seconds; if the window is on the search page, it goes back to
/// the app's search screen, which starts the engine again. Elsewhere (settings, indexing) the next search starts it
fn watch_engine(app: AppHandle) {
    if WATCHING.swap(true, std::sync::atomic::Ordering::SeqCst) {
        return;
    }
    thread::spawn(move || loop {
        thread::sleep(std::time::Duration::from_secs(2));
        let ended = {
            let engine = app.state::<Engine>();
            let mut g = engine.0.lock().unwrap();
            match g.as_mut().map(|c| c.try_wait()) {
                Some(Ok(Some(st))) => g.take().map(|c| (c.id(), st)),
                _ => None,
            }
        };
        let Some((pid, st)) = ended else { continue };
        untrack(pid);
        if let Ok(mut f) = std::fs::OpenOptions::new().append(true).open(home().join("logs").join("engine.log")) {
            use std::io::Write;
            let _ = writeln!(f, "FINDR_APP engine ended on its own ({st}); started again from the search screen");
        }
        if on_search_page(&app) {
            go_app(&app, "restart");
        }
    });
}

/// the window shows the engine's search page (not one of the app's own screens)
fn on_search_page(app: &AppHandle) -> bool {
    app.get_webview_window("main").and_then(|w| w.url().ok())
        .map(|u| u.host_str() == Some("127.0.0.1") && u.port() == Some(port()))
        .unwrap_or(false)
}

#[tauri::command(async)]
fn stop_engine(engine: State<'_, Engine>) -> Result<(), String> {
    stop(&engine);
    Ok(())
}

fn stop(engine: &Engine) {
    let taken = engine.0.lock().unwrap().take();   // out of the lock first: the watcher and a start do not wait on the kill
    if let Some(mut child) = taken {
        end_engine(&mut child);
    }
}

fn end_engine(child: &mut Child) {
    let pid = child.id();
    // the engine runs helper servers of its own: end the whole tree, gently where the system allows
    #[cfg(windows)]
    {
        let _ = command("taskkill").args(["/PID", &pid.to_string(), "/T", "/F"]).status();
    }
    #[cfg(not(windows))]
    {
        let _ = Command::new("kill").args(["-TERM", &pid.to_string()]).status();
        for _ in 0..50 {
            if let Ok(Some(_)) = child.try_wait() {
                untrack(pid);
                return;
            }
            thread::sleep(std::time::Duration::from_millis(100));
        }
    }
    let _ = child.kill();
    let _ = child.wait();
    untrack(pid);
}

/// the search page's '검색 엔진 다시 켜기' (no answer, or none for 90 s): back to the app's search screen, which stops the engine
/// and starts it again. The page itself may only open files and come back here; it cannot start an engine
#[tauri::command(async)]
fn restart_search(app: AppHandle) {
    go_app(&app, "restart");
}

/// open a found file (or show it in its folder). Programs and scripts are only shown, never run.
/// Off the window's thread, in a thread of its own: the system's open waits for the program it hands the file to (seconds for
/// an office program on a busy PC), and a file on a network drive that is gone waits on the network. On the window's thread
/// the whole window stopped answering meanwhile (field, Windows: clicking a found file froze the window)
#[tauri::command(async)]
fn open_path(app: AppHandle, path: String, reveal: bool) -> Result<(), String> {
    let (tx, rx) = std::sync::mpsc::channel();
    thread::spawn(move || {
        #[cfg(windows)]
        let _com = Com::init();   // the system's open can hand the file to shell extensions, which need COM on this thread
        let _ = tx.send(open_now(&app, &path, reveal));
    });
    rx.recv().unwrap_or_else(|_| Err(l("열지 못했습니다.", "Could not open it.")))
}

fn open_now(app: &AppHandle, path: &str, reveal: bool) -> Result<(), String> {
    let p = PathBuf::from(path);
    if !p.exists() {
        return Err(l("파일이 없습니다. 옮겨졌거나 지워졌을 수 있습니다.", "The file is not there. It may have been moved or deleted."));
    }
    let ext = p.extension().and_then(|e| e.to_str()).unwrap_or("").to_lowercase();
    const RUNS: [&str; 18] = ["exe", "bat", "cmd", "com", "msi", "ps1", "vbs", "vbe", "js", "jse", "wsf", "scr", "lnk", "sh", "app", "command", "jar", "reg"];
    if reveal || RUNS.contains(&ext.as_str()) {
        app.opener().reveal_item_in_dir(&p).map_err(|e| e.to_string())
    } else {
        app.opener().open_path(path, None::<&str>).map_err(|e| e.to_string())
    }
}

/// COM for the thread that opens a file, ended with it
#[cfg(windows)]
struct Com(bool);

#[cfg(windows)]
impl Com {
    fn init() -> Self {
        use windows::Win32::System::Com::{CoInitializeEx, COINIT_APARTMENTTHREADED, COINIT_DISABLE_OLE1DDE};
        Com(unsafe { CoInitializeEx(None, COINIT_APARTMENTTHREADED | COINIT_DISABLE_OLE1DDE) }.is_ok())
    }
}

#[cfg(windows)]
impl Drop for Com {
    fn drop(&mut self) {
        if self.0 {
            unsafe { windows::Win32::System::Com::CoUninitialize() };
        }
    }
}

#[tauri::command]
fn open_data_dir(app: AppHandle) -> Result<(), String> {
    let _ = std::fs::create_dir_all(home());
    app.opener().open_path(home().display().to_string(), None::<&str>).map_err(|e| e.to_string())
}

/// remove everything findr keeps on this PC: the index, the downloaded models, the engine runtime, settings and logs. All of
/// it is in the data folder, so only that folder is removed; the person's own files are never touched. The app itself is
/// removed the usual way afterwards (Windows: Settings > Apps, macOS: drag it to the Trash)
#[tauri::command(async)]
fn uninstall_data(engine: State<'_, Engine>) -> Result<String, String> {
    stop(&engine);
    stop_upgrade();
    stop_runs();
    let h = home();
    let name = h.file_name().and_then(|n| n.to_str()).unwrap_or("");
    // a folder set by FINDR_HOME counts only when it holds findr's own things: FINDR_HOME=~ must never empty a home folder
    let models = std::fs::read_dir(h.join("models")).map(|mut d| d.next().is_some()).unwrap_or(false);
    let marks = models || ["index.db", "runtime", "progress.json", "folders.json"].iter().any(|m| h.join(m).exists());
    if !((name == "findr" || name == ".findr" || std::env::var("FINDR_HOME").is_ok()) && marks) {
        return Err(l(&format!("findr의 데이터 폴더가 아니라서 지우지 않았습니다: {}", h.display()), &format!("Nothing was deleted: this is not findr's data folder: {}", h.display())));
    }
    // on Windows the app is installed into this same folder (%LOCALAPPDATA%\findr): its own files stay for the system's
    // uninstaller, which removes them; removing the running app and its uninstaller here left a broken install (11/11)
    let exe = std::env::current_exe().ok();
    let app_dir = exe.as_ref().and_then(|e| e.parent()).map(|p| p.to_path_buf());
    let shared = app_dir.as_ref().map(|d| d == &h).unwrap_or(false);
    let keep: Vec<String> = if shared {
        let mut k = vec!["uninstall.exe".to_string(), "uv.exe".to_string(), "uv".to_string(), "engine".to_string()];
        if let Some(n) = exe.as_ref().and_then(|e| e.file_name()).and_then(|n| n.to_str()) {
            k.push(n.to_string());
        }
        k
    } else {
        vec![]
    };
    let mut last = String::new();
    for _ in 0..20 {   // a process stopped a moment ago can still hold a file on Windows
        if !shared {
            if !h.exists() {
                return Ok(h.display().to_string());
            }
            match std::fs::remove_dir_all(&h) {
                Ok(()) => return Ok(h.display().to_string()),
                Err(e) => last = e.to_string(),
            }
        } else {
            let mut left = 0;
            for e in std::fs::read_dir(&h).map_err(|e| e.to_string())?.flatten() {
                let n = e.file_name().to_string_lossy().to_string();
                if keep.iter().any(|k| k.eq_ignore_ascii_case(&n)) {
                    continue;
                }
                let p = e.path();
                let r = if p.is_dir() { std::fs::remove_dir_all(&p) } else { std::fs::remove_file(&p) };
                if let Err(err) = r {
                    left += 1;
                    last = err.to_string();
                }
            }
            if left == 0 {
                return Ok(h.display().to_string());
            }
        }
        thread::sleep(std::time::Duration::from_millis(500));
    }
    Err(l(&format!("일부 파일을 지우지 못했습니다 ({last}). 앱을 닫은 뒤 이 폴더를 지워 주세요: {}", h.display()), &format!("Some files could not be deleted ({last}). Close the app, then delete this folder: {}", h.display())))
}

#[tauri::command(async)]
fn quit_app(app: AppHandle) {
    quit(&app);
}

// ---------- app ----------

/// the address of the app's own screens. Read from the window at the start, but on Windows the window is still at
/// about:blank then, and the menu's '검색' and '폴더와 설정' opened about:blank#settings: a white page (field, 0.2.20).
/// Then the address Tauri serves its screens from on that OS
fn app_page(seen: Option<Url>) -> Option<Url> {
    match seen {
        Some(u) if u.scheme() != "about" && u.scheme() != "data" => Some(u),
        _ => Url::parse(if cfg!(windows) { "http://tauri.localhost/" } else { "tauri://localhost/" }).ok(),
    }
}

fn go_app(app: &AppHandle, hash: &str) {
    let seen = app.state::<AppUrl>().0.lock().unwrap().clone();
    if let (Some(w), Some(u)) = (app.get_webview_window("main"), app_page(seen)) {
        let mut u = u;
        u.set_fragment(Some(hash));
        let _ = w.navigate(u);
    }
}

/// quit from the menu or the settings screen: the engine, the upgrade and the runs are ended off the window's thread (each
/// is a taskkill, the engine's gentle stop up to 5 s elsewhere), with the window hidden at once; on the window's thread
/// the window hung on 'quit' meanwhile. Once: a second press while the first is ending things does nothing
static QUITTING: std::sync::atomic::AtomicBool = std::sync::atomic::AtomicBool::new(false);

fn quit(app: &AppHandle) {
    if QUITTING.swap(true, std::sync::atomic::Ordering::SeqCst) {
        return;
    }
    if let Some(w) = app.get_webview_window("main") {
        let _ = w.hide();
    }
    let app = app.clone();
    thread::spawn(move || {
        stop(&app.state::<Engine>());
        stop_upgrade();
        stop_runs();
        app.exit(0);
    });
}

/// the window's page process ended or stopped answering (out of memory, a crash): WebView2 leaves the window white and
/// nothing drew it again (field, Windows, 16 GB laptop: only the findr window went white, the rest of the PC kept working).
/// The page is drawn again through the app's search screen, which also starts the engine if it is gone. With the browser
/// process itself gone the window cannot draw again, and the app starts anew. At most 3 times in 5 minutes: a page that
/// fails again at once (memory still short) is not reloaded in a loop
static RECOVERED: Mutex<Vec<std::time::Instant>> = Mutex::new(Vec::new());

#[cfg_attr(not(windows), allow(dead_code))]
fn recover_window(app: &AppHandle, browser_gone: bool) {
    {
        let mut g = RECOVERED.lock().unwrap();
        g.retain(|t| t.elapsed().as_secs() < 300);
        if g.len() >= 3 {
            return;
        }
        g.push(std::time::Instant::now());
    }
    let app = app.clone();
    thread::spawn(move || {   // outside WebView2's own event: the window is not navigated from inside its callback
        thread::sleep(std::time::Duration::from_millis(500));
        if browser_gone {
            app.request_restart();
        } else {
            go_app(&app, "search");
        }
    });
}

#[cfg(windows)]
fn watch_webview(app: &AppHandle) {
    use webview2_com::Microsoft::Web::WebView2::Win32::{
        COREWEBVIEW2_PROCESS_FAILED_KIND, COREWEBVIEW2_PROCESS_FAILED_KIND_BROWSER_PROCESS_EXITED,
        COREWEBVIEW2_PROCESS_FAILED_KIND_RENDER_PROCESS_EXITED, COREWEBVIEW2_PROCESS_FAILED_KIND_RENDER_PROCESS_UNRESPONSIVE,
    };
    use webview2_com::ProcessFailedEventHandler;
    let Some(w) = app.get_webview_window("main") else { return };
    let app = app.clone();
    let _ = w.with_webview(move |wv| {
        let Ok(core) = (unsafe { wv.controller().CoreWebView2() }) else { return };
        let handler = ProcessFailedEventHandler::create(Box::new(move |_, args| {
            let Some(args) = args else { return Ok(()) };
            let mut kind = COREWEBVIEW2_PROCESS_FAILED_KIND::default();
            unsafe { args.ProcessFailedKind(&mut kind)? };
            // the GPU and helper processes come back by themselves; a frame of the page is not the page
            if kind == COREWEBVIEW2_PROCESS_FAILED_KIND_BROWSER_PROCESS_EXITED {
                recover_window(&app, true);
            } else if kind == COREWEBVIEW2_PROCESS_FAILED_KIND_RENDER_PROCESS_EXITED || kind == COREWEBVIEW2_PROCESS_FAILED_KIND_RENDER_PROCESS_UNRESPONSIVE {
                recover_window(&app, false);
            }
            Ok(())
        }));
        let mut token = 0i64;
        let _ = unsafe { core.add_ProcessFailed(&handler, &mut token) };
    });
}

#[cfg_attr(mobile, tauri::mobile_entry_point)]
pub fn run() {
    tauri::Builder::default()
        .plugin(tauri_plugin_opener::init())
        .plugin(tauri_plugin_dialog::init())
        .manage(Engine(Mutex::new(None)))
        .manage(AppUrl(Mutex::new(None)))
        .setup(|app| {
            reap_left_runs(); // an indexer that a killed app left behind would run next to this app's own
            if let Some(w) = app.get_webview_window("main") {
                *app.state::<AppUrl>().0.lock().unwrap() = w.url().ok();
            }
            match saved_lang() {
                Some(ko) => {
                    KOREAN.store(ko, std::sync::atomic::Ordering::Relaxed);
                    LANG_SAID.store(true, std::sync::atomic::Ordering::Relaxed);
                }
                None => KOREAN.store(system_korean(), std::sync::atomic::Ordering::Relaxed),
            }
            set_app_menu(app.handle())?;
            watch_lang(app.handle().clone());
            #[cfg(windows)]
            watch_webview(app.handle());
            Ok(())
        })
        .on_menu_event(|app, e| match e.id().as_ref() {
            "search" => go_app(app, "search"),
            "settings" => go_app(app, "settings"),
            "data" => { let _ = open_data_dir(app.clone()); }
            "quit" => quit(app),
            _ => {}
        })
        .invoke_handler(tauri::generate_handler![
            app_state, setup_runtime, setup_status, list_roots, start_index, start_engine, stop_engine, open_path, open_data_dir, save_folders, load_folders,
            upgrade_state, set_upgrade, read_progress, set_pause, uninstall_data, quit_app, set_lang, saved_lang_choice, restart_search
        ])
        .build(tauri::generate_context!())
        .expect("findr 창을 여는 중 오류가 났습니다")
        .run(|app, e| {
            if let tauri::RunEvent::Exit = e {
                stop(&app.state::<Engine>());
                stop_upgrade();
                stop_runs();
            }
        });
}
