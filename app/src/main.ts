// findr desktop screens: first-run setup, folders, indexing, settings. The search page itself comes from the local engine.
import { invoke } from '@tauri-apps/api/core'
import { listen, type UnlistenFn } from '@tauri-apps/api/event'
import { open as pickFolder } from '@tauri-apps/plugin-dialog'

type AppState = { home: string; ready: boolean; indexed: boolean; gpu: string; version: string; folders: string[]; autotest: boolean; update: boolean }
type Root = { order: number; label: string; path: string }
type Line = { step: string; line: string }
type Done = { ok: boolean; message: string }

const view = document.getElementById('view')!
const esc = (s: string) => s.replace(/[&<>"']/g, c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]!))
const GPU: Record<string, string> = { nvidia: 'NVIDIA GPU', apple: 'Apple 칩', cpu: 'GPU 없음 (CPU)' }
let unlisten: UnlistenFn[] = []
const clear = () => { unlisten.forEach(u => u()); unlisten = [] }
const photoOn = () => { try { return localStorage.getItem('findr.photo') !== 'off' } catch { return true } }

async function route() {
  clear()
  const s = await invoke<AppState>('app_state')
  const h = location.hash.slice(1)
  if (!s.ready) return h === 'setup' || s.update ? setup(s) : welcome(s)   // a newer engine in the app: update without asking
  if (h === 'settings') return settings(s)
  if ((h === 'index' || h === 'deep') && s.folders.length) return indexing(s.folders, h === 'deep')
  if (h === 'folders' || !s.indexed) return folders(s)
  return search()
}
window.addEventListener('hashchange', route)

// ---------- first run ----------
function welcome(s: AppState) {
  const size = s.gpu === 'cpu' ? '약 3GB' : '약 6~13GB'
  view.innerHTML = `
    <h1>findr</h1>
    <p class="lead">기억나는 대로 물으면 내 PC에서 파일을 찾아 줍니다. 파일 내용과 질문은 이 PC 밖으로 나가지 않습니다.</p>
    <div class="card">
      <p>처음 한 번, 검색에 필요한 구성 요소를 이 PC에 맞게 준비합니다.</p>
      <dl>
        <dt>이 PC</dt><dd>${esc(GPU[s.gpu] || s.gpu)}</dd>
        <dt>받는 용량</dt><dd>${size} (인터넷 속도에 따라 5~20분)</dd>
        <dt>저장 위치</dt><dd>${esc(s.home)}</dd>
      </dl>
      <p class="muted" style="margin-top:12px">다른 프로그램이나 시스템 설정은 바꾸지 않습니다. 위 폴더를 지우면 모두 사라집니다.</p>
    </div>
    <div class="actions"><button class="primary" id="go">준비 시작</button></div>`
  document.getElementById('go')!.onclick = () => { location.hash = 'setup' }
  if (s.autotest) setTimeout(() => document.getElementById('go')?.click(), 3000)
}

const STEPS: [string, string][] = [['vcrt', 'Windows 구성 요소'], ['env', '파이썬 환경'], ['torch', '연산 라이브러리'], ['engine', '검색 엔진'], ['models', '모델 받기']]

async function setup(s: AppState) {
  view.innerHTML = `
    <h1>${s.update ? '새 버전을 준비하는 중' : '준비하는 중'}</h1>
    <p class="lead">창을 닫지 말고 기다려 주세요. 끝나면 색인할 폴더를 고르는 화면으로 넘어갑니다.</p>
    <div class="card"><ul class="steps">${STEPS.map(([id, name]) => `
      <li id="st-${id}"><span class="state">대기</span><div><div class="step-name">${name}</div><div class="step-line"></div></div></li>`).join('')}
    </ul></div>
    <div id="msg"></div>
    <details><summary>자세한 기록</summary><pre id="log"></pre></details>`
  const log: string[] = []
  let cur = ''
  const mark = (id: string, cls: string, text: string) => {
    const li = document.getElementById('st-' + id); if (!li) return
    const st = li.querySelector('.state')!; st.className = 'state ' + cls; st.textContent = text
  }
  unlisten.push(await listen<Line>('setup', e => {
    const { step, line } = e.payload
    if (step !== cur) { if (cur) mark(cur, 'ok', '완료'); cur = step; mark(step, 'run', '진행 중') }
    const li = document.getElementById('st-' + step)
    if (li) li.querySelector('.step-line')!.textContent = line
    log.push(line); if (log.length > 300) log.shift()
    document.getElementById('log')!.textContent = log.join('\n')
  }))
  unlisten.push(await listen<Done>('setup-done', e => {
    if (e.payload.ok) { if (cur) mark(cur, 'ok', '완료'); location.hash = 'folders'; return }
    if (cur) mark(cur, 'fail', '실패')
    document.getElementById('msg')!.innerHTML = `<div class="notice err">${esc(e.payload.message)}</div>
      <div class="actions"><button class="primary" id="retry">다시 시도</button></div>`
    document.getElementById('retry')!.onclick = () => { route() }
  }))
  mark('vcrt', 'run', '진행 중'); cur = 'vcrt'
  invoke('setup_runtime')
}

// ---------- folders ----------
async function folders(s: AppState) {
  view.innerHTML = `<h1>어디를 찾을까요</h1>
    <p class="lead">처음에는 PC 전체에서 내 파일이 있는 폴더를 고릅니다. 시스템·프로그램 폴더와 다른 사용자 계정은 자동으로 빠집니다.</p>
    <div class="card"><div class="row"><div class="spinner"></div><span class="muted">폴더를 살펴보는 중입니다</span></div></div>`
  let roots: Root[] = []
  try { roots = JSON.parse(await invoke<string>('list_roots')) } catch (e) {
    view.querySelector('.card')!.innerHTML = `<div class="notice err">폴더 목록을 읽지 못했습니다: ${esc(String(e))}</div>`; return
  }
  const chosen = new Set(s.folders.length ? s.folders : roots.map(r => r.path))
  const extra = s.folders.filter(f => !roots.some(r => r.path === f)).map(f => ({ order: 9, label: '추가한 폴더', path: f }))
  const all = [...roots, ...extra]
  const draw = () => {
    view.querySelector('.card')!.innerHTML = `<ul class="folders">${all.map((r, i) => `
      <li><input type="checkbox" id="f${i}" ${chosen.has(r.path) ? 'checked' : ''}>
        <label class="lab" for="f${i}">${esc(r.label)}</label><span class="path">${esc(r.path)}</span></li>`).join('')}</ul>`
    all.forEach((r, i) => {
      (document.getElementById('f' + i) as HTMLInputElement).onchange = ev => {
        (ev.target as HTMLInputElement).checked ? chosen.add(r.path) : chosen.delete(r.path); sync()
      }
    })
  }
  view.insertAdjacentHTML('beforeend', `<div class="actions">
      <button class="primary" id="start">색인 시작</button><button id="add">폴더 추가</button>
      ${s.indexed ? '<button id="back">취소</button>' : ''}</div>
    <p class="muted" style="margin-top:14px">찾을 가능성이 높은 폴더부터 처리합니다. 색인 중에도 검색할 수 있습니다.</p>`)
  const sync = () => { (document.getElementById('start') as HTMLButtonElement).disabled = chosen.size === 0 }
  draw(); sync()
  if (s.autotest) setTimeout(() => document.getElementById('start')?.click(), 5000)
  document.getElementById('add')!.onclick = async () => {
    const p = await pickFolder({ directory: true, multiple: false, title: '추가할 폴더' })
    if (typeof p === 'string' && !all.some(r => r.path === p)) { all.push({ order: 9, label: '추가한 폴더', path: p }); chosen.add(p); draw(); sync() }
  }
  document.getElementById('back')?.addEventListener('click', () => { location.hash = 'settings' })
  document.getElementById('start')!.onclick = async () => {
    const list = all.filter(r => chosen.has(r.path)).sort((a, b) => a.order - b.order).map(r => r.path)
    await invoke('save_folders', { folders: list })
    location.hash = 'index'
  }
}

// ---------- indexing ----------
async function indexing(list: string[], deep = false) {
  view.innerHTML = `<h1>색인하는 중</h1>
    <p class="lead">폴더마다 이름 → 내용 → 의미 순서로 처리합니다. 먼저 끝난 폴더부터 바로 검색할 수 있습니다.</p>
    <div class="card"><div class="row between"><span id="now">시작하는 중</span><span class="muted" id="count"></span></div>
      <div class="bar"><div id="bar"></div></div><p class="muted" id="detail" style="margin:6px 0 0"></p></div>
    <div class="actions"><button class="primary" id="go" disabled>지금 검색하기</button></div>
    <details><summary>자세한 기록</summary><pre id="log"></pre></details>`
  const log: string[] = []
  let n = list.length, done = 0, files = 0
  const set = (id: string, t: string) => { document.getElementById(id)!.textContent = t }
  unlisten.push(await listen<Line>('index', e => {
    const l = e.payload.line
    log.push(l); if (log.length > 300) log.shift(); set('log', log.join('\n'))
    let m
    if ((m = l.match(/^FINDR_ROOT (\{.*\})/))) { const r = JSON.parse(m[1]); n = r.n; set('now', `${r.i + 1}/${r.n}  ${r.path}`) }
    else if (l.startsWith('FINDR_ROOT_DONE')) {
      done++; (document.getElementById('bar') as HTMLElement).style.width = `${Math.round(100 * done / n)}%`
      ;(document.getElementById('go') as HTMLButtonElement).disabled = false
    }
    else if ((m = l.match(/^FINDR_SCAN new\/changed (\d+)/))) { files += +m[1]; set('count', `새로 찾은 파일 ${files.toLocaleString()}개`); set('detail', '파일 목록을 만드는 중') }
    else if ((m = l.match(/^FINDR_READ (\d+)/))) set('detail', `내용 읽음: ${(+m[1]).toLocaleString()}개`)
    else if (/^\s*embedded \d+\/\d+/.test(l)) set('detail', '의미를 계산하는 중 ' + l.trim().split(' ')[1])
    else if ((m = l.match(/^FINDR_EMBED (\d+)/))) set('detail', `의미 계산 완료: ${(+m[1]).toLocaleString()}개`)
  }))
  unlisten.push(await listen<Done>('index-done', e => {
    if (e.payload.ok) { location.hash = 'search'; return }
    set('detail', e.payload.message); (document.getElementById('go') as HTMLButtonElement).disabled = false
  }))
  document.getElementById('go')!.onclick = () => { location.hash = 'search' }
  invoke('start_index', { folders: list, deep })
}

// ---------- search (the engine's page) ----------
async function search() {
  view.innerHTML = `<h1>검색을 준비하는 중</h1>
    <div class="card"><div class="row"><div class="spinner"></div><span>검색 엔진을 켜고 있습니다. 처음에는 1분 정도 걸립니다.</span></div></div>`
  try {
    const url = await invoke<string>('start_engine', { photo: photoOn() })
    const s = await invoke<AppState>('app_state')
    location.href = s.autotest ? url + '&q=' + encodeURIComponent('고객사 미팅용 견적서') : url
  } catch (e) {
    view.innerHTML = `<h1>검색 엔진을 켜지 못했습니다</h1><div class="notice err">${esc(String(e))}</div>
      <div class="actions"><button class="primary" id="retry">다시 시도</button><button id="set">설정</button></div>`
    document.getElementById('retry')!.onclick = () => { route() }
    document.getElementById('set')!.onclick = () => { location.hash = 'settings' }
  }
}

// ---------- settings ----------
async function settings(s: AppState) {
  view.innerHTML = `<h1>폴더와 설정</h1>
    <p class="lead">findr ${esc(s.version)}</p>
    <div class="card"><dl>
      <dt>이 PC</dt><dd>${esc(GPU[s.gpu] || s.gpu)}</dd>
      <dt>데이터 폴더</dt><dd>${esc(s.home)}</dd>
      <dt>색인 폴더</dt><dd>${s.folders.length ? s.folders.map(esc).join('<br>') : 'PC 전체의 내 파일'}</dd></dl></div>
    <h2>색인</h2>
    <div class="card"><p>새로 생기거나 바뀐 파일만 다시 읽습니다.</p>
      <div class="actions" style="margin-top:6px"><button class="primary" id="re">바뀐 파일 반영</button><button id="fold">폴더 바꾸기</button></div>
      <p class="muted" style="margin:14px 0 6px">findr가 열려 있는 동안 바뀐 파일은 한 시간마다, 긴 문서의 뒷부분은 처음 한 번 낮은 우선순위로 알아서 반영합니다.
        지금 바로 하고 싶을 때만 아래를 누르세요.</p>
      <div class="actions" style="margin-top:0"><button id="deep">깊은 내용까지 계산</button></div></div>
    <h2>사진</h2>
    <div class="card"><label class="switch"><input type="checkbox" id="photo" ${photoOn() ? 'checked' : ''} ${s.gpu !== 'nvidia' ? 'disabled' : ''}>
      <span>사진을 찍힌 내용으로 찾기</span></label>
      <p class="muted" style="margin:8px 0 0">${s.gpu === 'nvidia' ? 'GPU 메모리를 7GB 정도 더 씁니다. 끄면 사진은 이름과 날짜로 찾습니다.' : '이 PC에서는 사진을 이름과 날짜로 찾습니다 (NVIDIA GPU 필요).'}</p></div>
    <h2>데이터</h2>
    <div class="card"><p>색인, 설정, findr가 배운 것은 모두 데이터 폴더에 있습니다.</p>
      <div class="actions" style="margin-top:6px"><button id="data">데이터 폴더 열기</button></div></div>
    <div class="actions"><button class="primary" id="back">검색으로 돌아가기</button></div>`
  document.getElementById('re')!.onclick = () => { invoke('stop_engine'); location.hash = 'index' }
  document.getElementById('deep')!.onclick = () => { invoke('stop_engine'); location.hash = 'deep' }
  document.getElementById('fold')!.onclick = () => { location.hash = 'folders' }
  document.getElementById('data')!.onclick = () => invoke('open_data_dir')
  document.getElementById('photo')!.onchange = ev => {
    try { localStorage.setItem('findr.photo', (ev.target as HTMLInputElement).checked ? 'on' : 'off') } catch { /* private storage off */ }
  }
  document.getElementById('back')!.onclick = () => { location.hash = 'search'; if (location.hash === '#search') route() }
}

route()
