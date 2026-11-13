"""the window must not hang or stay white (field, Windows 16 GB laptop, CPU only: the first search worked, opening a found
file froze the window, searching several times left it white, only a restart helped). The engine starts once at a time and
never twice, an engine that ends or stops answering is started again, a page process that fails is drawn again, a file is
opened off the window's thread, the page cancels what it no longer needs and says when it is slow, and errors are said apart
from a stopped engine"""
import json
import re
import socket
import subprocess
import sys
import time
import types
from pathlib import Path

import search

ROOT = Path(__file__).resolve().parents[1]
APP = ROOT / "app" / "src-tauri"
LIB = (APP / "src" / "lib.rs").read_text(encoding="utf-8")
MAIN = (ROOT / "app" / "src" / "main.ts").read_text(encoding="utf-8")
PAGE = (ROOT / "findr" / "core" / "page.html").read_text(encoding="utf-8")


def _fn(name, src=LIB):
    """the source of one Rust function, up to the next item at column 0"""
    i = src.index(f"fn {name}(")
    j = re.search(r"\n(#\[|fn |static |struct |impl |const |/// )", src[i + 3:])
    return src[i:i + 3 + (j.start() if j else len(src))]


def _attr(name):
    i = LIB.index(f"fn {name}(")
    return LIB[max(0, LIB.rfind("#[tauri::command", 0, i)):i]


# ---------- the app ----------

def test_one_engine_start_at_a_time_and_none_dropped():
    body = _fn("start_engine")
    assert "STARTING.lock()" in body.split("let alive")[0]   # the guard is taken before anything is looked at
    assert "keep(&engine, child);" in body and "*engine.0.lock().unwrap() = Some(child)" not in LIB
    keep = _fn("keep")
    assert ".replace(child)" in keep and "end_engine(&mut o)" in keep   # one already there is ended, not dropped


def test_running_engine_must_answer_to_be_reused():
    body = _fn("start_engine")
    assert "alive && *ENGINE_PHOTO.lock().unwrap() == Some(photo) && engine_answers()" in body
    assert "/healthz" in _fn("engine_answers") and "connect_timeout" in _fn("engine_answers")
    # the engine's lock is not held across the ping (the watcher and a stop would wait on it)
    assert body.index("let alive = {") < body.index("engine_answers()") and "};\n    if alive" in body


def test_engine_start_has_a_deadline():
    body = _fn("start_engine")
    assert "const ENGINE_START_SECS: u64 = 180;" in LIB and "rx.recv_timeout(std::time::Duration::from_secs(ENGINE_START_SECS))" in body
    assert "RecvTimeoutError::Timeout" in body and "3분 안에" in body and "within 3 minutes" in body
    assert "engine.prev.log" in body   # the log of the engine that ended is kept


def test_engine_is_tracked_and_ends_with_the_app():
    body = _fn("start_engine")
    assert 'c.env("FINDR_PARENT", std::process::id().to_string());' in body
    assert "own_group(&mut c);" in body and "track(child.id());" in body
    assert "untrack(pid);" in _fn("end_engine")
    stop = _fn("stop")
    assert stop.index(".take();") < stop.index("end_engine")   # out of the lock before the kill


def test_engine_watcher_sends_the_window_back():
    w = _fn("watch_engine")
    assert "WATCHING.swap(true" in w and "try_wait()" in w and 'go_app(&app, "restart")' in w and "on_search_page(&app)" in w
    assert "watch_engine(app);" in _fn("start_engine")


def test_open_path_off_the_window_thread():
    assert "#[tauri::command(async)]" in _attr("open_path")
    body = _fn("open_path")
    assert "thread::spawn" in body and "Com::init()" in body and "open_now(&app, &path, reveal)" in body
    assert "COINIT_APARTMENTTHREADED" in LIB


def test_upgrade_switch_and_quit_off_the_window_thread():
    assert "#[tauri::command(async)]" in _attr("set_upgrade")
    st = _fn("stop_upgrade")
    assert st.index("UPGRADING.lock().unwrap().take();") < st.index("kill_tree(c.id());")
    q = _fn("quit")
    assert "QUITTING.swap(true" in q and "thread::spawn" in q and "stop_runs();" in q and "w.hide()" in q
    assert '"quit" => quit(app),' in LIB and "quit(&app);" in _fn("quit_app")


def test_page_process_failure_is_recovered_on_windows():
    w = _fn("watch_webview")
    assert "add_ProcessFailed" in w and "COREWEBVIEW2_PROCESS_FAILED_KIND_RENDER_PROCESS_EXITED" in w
    assert "COREWEBVIEW2_PROCESS_FAILED_KIND_BROWSER_PROCESS_EXITED" in w and "with_webview" in w
    assert "#[cfg(windows)]\n            watch_webview(app.handle());" in LIB
    r = _fn("recover_window")
    assert "g.len() >= 3" in r and "request_restart()" in r and 'go_app(&app, "search")' in r


def test_windows_crates_are_the_ones_already_built():
    """webview2-com and windows at the versions wry and the opener use: no second copy to build"""
    toml = (APP / "Cargo.toml").read_text(encoding="utf-8")
    lock = (APP / "Cargo.lock").read_text(encoding="utf-8")
    assert "[target.'cfg(windows)'.dependencies]" in toml
    for crate, want in (("webview2-com", '"0.39"'), ("windows", '"0.62"')):
        assert re.search(rf'^{crate} = .*{re.escape(want)}', toml, re.M), crate
        vers = re.findall(rf'name = "{crate}"\nversion = "([^"]+)"', lock)
        assert len(vers) == 1 and vers[0].startswith(want.strip('"') + "."), (crate, vers)


def test_restart_search_is_the_pages_only_other_command():
    assert 'go_app(&app, "restart");' in _fn("restart_search")
    page = json.loads((APP / "capabilities" / "search-page.json").read_text(encoding="utf-8"))
    assert page["permissions"] == ["allow-open-path", "allow-restart-search"]


# ---------- the app's screens ----------

def test_no_screen_is_run_twice():
    """an address change runs route() by itself: calling it as well started the engine twice"""
    assert "location.hash = 'search'; route()" not in MAIN
    assert "location.hash = 'search'; if (location.hash === '#search') route()" not in MAIN
    assert "const goHash = (to: string) => { if (location.hash === '#' + to) route(); else location.hash = to }" in MAIN


def test_restart_screen_and_screen_timers():
    assert "return search(h === 'restart')" in MAIN
    s = MAIN[MAIN.index("async function search("):]
    assert "if (restart) await invoke('stop_engine')" in s and "다시 켜고 있습니다" in s and "being started again" in s
    assert "if (mine !== screen) return" in s
    assert "timers.forEach(t => clearTimeout(t))" in MAIN and "screen++" in MAIN
    ix = MAIN[MAIN.index("async function indexing("):MAIN.index("async function search(")]
    assert "timers.push(tipTimer)" in ix and "timers.push(poll)" in ix and "timers.push(setTimeout(tickDown, 1000))" in ix
    assert "if (stay || moved || mine !== screen) return" in ix


# ---------- the search page ----------

def test_page_sends_one_question_at_a_time():
    assert "if(e.repeat)return;if(e.isComposing||e.keyCode===229)return;go()" in PAGE
    g = PAGE[PAGE.index("async function go(){"):PAGE.index("function failed(")]
    assert "if(ctl&&asking===qq+'\\n'+mode)return;" in g and "if(ctl)ctl.abort();" in g
    assert g.count("{signal:c.signal}") == 2 and "refine(qq,r,c.signal)" in g
    assert "finally{clearTimeout(slow);clearTimeout(dead);if(ctl===c){ctl=null;asking=''}}" in g


def test_page_says_slow_and_gives_up():
    assert "const SLOW_MS=5000, GIVE_UP_MS=90000;" in PAGE
    g = PAGE[PAGE.index("async function go(){"):PAGE.index("function failed(")]
    assert "아직 찾는 중이에요" in g and "Still searching" in g and "late=true;c.abort()" in g
    assert "failed(late?'late':'gone')" in g and "failed('error',r.error)" in g


def test_page_tells_error_from_unreachable_and_can_restart():
    f = PAGE[PAGE.index("function failed("):]
    for ko, en in (("검색 엔진에 연결하지 못했습니다", "Couldn't reach the search engine"), ("90초 동안 답이 없어", "No answer for 90 seconds"),
                   ("검색 중 오류가 났습니다", "Something went wrong during the search"), ("검색 엔진 다시 켜기", "Restart the search engine")):
        assert ko in f and en in f, ko
    assert "APP&&kind!=='error'?'<button data-restart" in f and "invoke('restart_search')" in f and "data-retry" in f


def test_page_opens_a_file_once():
    assert "const opening=new Set();" in PAGE and "if(opening.has(key))return;" in PAGE
    assert PAGE.count("once(") >= 5 and "1500-(Date.now()-t0)" in PAGE and "여는 중…" in PAGE and "Opening…" in PAGE


def test_page_progress_one_look_at_a_time():
    assert "async function tick(){if(looking)return;looking=true;" in PAGE and "looking=false; timer=setTimeout(tick" in PAGE


# ---------- the engine ----------

def test_client_gone():
    a, b = socket.socketpair()
    try:
        assert not search.client_gone(a)   # open, nothing sent: the page still waits
        b.close()
        assert search.client_gone(a)   # closed from the page's side
    finally:
        a.close()


def test_errors_come_back_as_json():
    sent = []
    h = types.SimpleNamespace()
    h.reply = lambda code, body, ct: sent.append((code, json.loads(body), ct))

    def boom():
        raise KeyError("x")
    search.answer_errors(h, boom)
    assert sent and sent[0][0] == 500 and sent[0][1]["kind"] == "error" and sent[0][1]["error"] and sent[0][1]["detail"] == "KeyError"

    sent.clear()

    def half():   # the answer had begun: no second one on top of it
        h._replied = True
        raise ValueError
    search.answer_errors(h, half)
    assert not sent

    def gone():
        raise BrokenPipeError
    search.answer_errors(h, gone)
    assert not sent


def test_handler_runs_through_the_error_guard():
    src = Path(search.__file__).read_text(encoding="utf-8")
    assert "answer_errors(self, self._get)" in src and "answer_errors(self, self._post)" in src
    assert "def _get(self):" in src and "def _post(self):" in src and "self._replied = True" in src


def test_queued_question_rechecks_the_cache_and_skips_a_gone_page():
    src = Path(search.__file__).read_text(encoding="utf-8")
    i = src.index('_ST.d["lock_wait"] = time.time() - t_req')
    after = src[i:i + 1200]
    assert "client_gone(self.connection)" in after and "RESULTS.get(rkey)" in after and 'res["cached"] = True' in after
    assert "watch_parent(httpd.shutdown)" in src


def test_parent_check_and_watch(tmp_path):
    assert search.parent_check(0) is None
    p = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
    try:
        alive = search.parent_check(p.pid)
        assert alive is not None and alive()
        stopped = []
        search.watch_parent(lambda: stopped.append(1), every=0.05, alive=alive, hard_exit=lambda: None)
        time.sleep(0.2)
        assert not stopped
        p.kill(); p.wait()
        for _ in range(100):
            if stopped:
                break
            time.sleep(0.05)
        assert stopped and not alive()
    finally:
        if p.poll() is None:
            p.kill()
