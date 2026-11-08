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
  // an update that could not finish (offline, a new dependency) keeps the installed engine usable for this session
  let later = false; try { later = sessionStorage.getItem('findr.updateLater') === '1' } catch { /* no storage */ }
  if (!s.ready && !(s.update && later)) return h === 'setup' || s.update ? setup(s) : welcome(s)   // a newer engine in the app: update without asking
  if (h === 'settings') return settings(s)
  if ((h === 'index' || h === 'deep') && s.folders.length) return indexing(s.folders, h === 'deep')
  if (h === 'folders' || !s.indexed) return folders(s)
  return search()
}
window.addEventListener('hashchange', route)

// ---------- first run ----------
function welcome(s: AppState) {
  const size = s.gpu === 'nvidia' ? '약 6~13GB' : '약 3~4GB'   // a Mac starts on the small meaning model too (the large one comes later, in idle time)
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
  // an update on a PC that already has an index and folders: back to search (the folder screen again was a needless question,
  // field 0.2.13); the background refresh picks up changes as before
  const keep = s.update && s.indexed && s.folders.length > 0
  view.innerHTML = `
    <h1>${s.update ? '새 버전을 준비하는 중' : '준비하는 중'}</h1>
    <p class="lead">창을 닫지 말고 기다려 주세요. 끝나면 ${keep ? '검색 화면으로 돌아갑니다' : '색인할 폴더를 고르는 화면으로 넘어갑니다'}.</p>
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
    if (e.payload.ok) {
      if (cur) mark(cur, 'ok', '완료')
      const to = keep ? 'search' : 'folders'
      if (location.hash === '#' + to) route(); else location.hash = to
      return
    }
    if (cur) mark(cur, 'fail', '실패')
    document.getElementById('msg')!.innerHTML = `<div class="notice err">${esc(e.payload.message)}</div>
      <div class="actions"><button class="primary" id="retry">다시 시도</button>${s.update ? '<button id="later">지금은 이전 버전으로 검색</button>' : ''}</div>`
    document.getElementById('retry')!.onclick = () => { route() }
    document.getElementById('later')?.addEventListener('click', () => {
      try { sessionStorage.setItem('findr.updateLater', '1') } catch { /* no storage */ }
      location.hash = 'search'; route()
    })
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
    view.querySelector('.card')!.innerHTML = `<div class="notice err">폴더 목록을 읽지 못했습니다: ${esc(String(e))}</div>
      <div class="actions"><button class="primary" id="rr">다시 시도</button>${s.indexed ? '<button id="bk">검색으로</button>' : ''}</div>`;
    document.getElementById('rr')!.onclick = () => { route() }
    document.getElementById('bk')?.addEventListener('click', () => { location.hash = 'search' }); return
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
// People wait more easily when they see progress that is honest, can already do something useful, and stay in control.
const TIPS = ['지난달 받은 견적서', '작년 여름 바다에서 찍은 사진', '최종본 말고 그 직전 판', '회의 전에 고친 발표 자료', '카톡으로 받은 계약서 스캔', '출장비 한도가 적힌 규정']
const CAN: [string, string, string][] = [   // stage, what the person can do once it is ready, what is happening meanwhile
  ['names', '파일 이름으로 찾기', '파일 목록을 만드는 중'],
  ['namevec', '비슷한 말로 이름 찾기', '이름의 뜻을 파악하는 중'],
  ['content', '문서 속 글자로 찾기', '문서를 읽는 중'],
  ['meaning', '기억나는 대로 물어보기', '내용의 뜻을 파악하는 중'],
  ['ocr', '스캔·스크린샷 속 글자 찾기', '스캔 글자를 읽는 중'],
]
type Progress = { stage?: string; done?: number; total?: number; minutes_left?: number | null; files?: number | null; paused?: boolean
  stages?: Record<string, string>; t?: number }
const ICON: Record<string, string> = { done: '✓', now: '●', later: '◷', wait: '○' }
const STATE: Record<string, string> = { done: '준비됨', now: '진행 중', later: '쉬는 시간에', wait: '대기' }

async function indexing(list: string[], deep = false) {
  view.innerHTML = `<h1>내 파일을 살펴보는 중</h1>
    <p class="lead">이 PC 안에서만 처리해요. 파일 목록이 만들어지면 바로 검색할 수 있어요.</p>
    <div class="card hero" aria-live="polite">
      <div class="big"><span id="nfiles">0</span><span class="unit">개 파일을 찾았어요</span></div>
      <div class="muted where" id="where">폴더를 둘러보는 중</div>
      <div class="bar" role="progressbar" aria-label="전체 진행" aria-valuemin="0" aria-valuemax="100"><div id="bar"></div></div>
      <div class="row between muted"><span id="pct">0%</span><span id="eta"></span></div>
    </div>
    <h2>지금 할 수 있는 것</h2>
    <div class="card"><ul class="ladder">${CAN.map(([id, can, doing]) => `
      <li id="ix-${id}" class="wait"><span class="ic" aria-hidden="true">${ICON.wait}</span>
        <div><div class="can">${can}</div><div class="doing" id="d-${id}">앞 단계가 끝나면 시작해요</div></div><span class="st">${STATE.wait}</span></li>`).join('')}</ul></div>
    <div class="card tip"><span class="muted" id="tiplab">준비되면 이렇게 물어보세요</span><p id="tip"></p></div>
    <div class="actions"><button class="primary" id="go" disabled>검색 시작하기</button><button id="pause">잠시 멈추기</button></div>
    <p class="muted auto" id="auto"></p>
    <details><summary>자세한 기록</summary><pre id="log"></pre></details>`
  const log: string[] = []
  let found = 0, engine: Promise<string> | null = null, moved = false, ready = false, stay = false, countdown = 0, P: Progress = {}
  const $ = (id: string) => document.getElementById(id)!
  const set = (id: string, t: string) => { const e = document.getElementById(id); if (e) e.textContent = t }
  let ti = 0; const tip = () => set('tip', '“' + TIPS[ti++ % TIPS.length] + '”'); tip()
  const tipTimer = setInterval(tip, 6000)
  const goSearch = async () => {   // the engine was started in advance: no second wait
    if (moved) return; moved = true; clearInterval(tipTimer); clearInterval(poll)
    try { const url = await (engine || invoke<string>('start_engine', { photo: photoOn() })); location.href = url }
    catch (e) { location.hash = 'search' }
  }
  const render = () => {
    const sg = P.stages || {}
    let done = 0
    for (const [id, , doing] of CAN) {
      const v = sg[id] || 'wait', li = $('ix-' + id)
      li.className = v; li.querySelector('.ic')!.textContent = ICON[v]
      li.querySelector('.st')!.textContent = v === 'now' && P.paused ? '멈춤' : STATE[v]
      let d = v === 'wait' ? '앞 단계가 끝나면 시작해요' : doing
      if (v === 'now' && P.stage === id && P.total) d += ` · ${Number(P.done).toLocaleString()}/${Number(P.total).toLocaleString()}`
      if (v === 'later') d = 'PC를 쓰지 않을 때 조금씩 읽어요'
      if (v === 'done') d = '지금 할 수 있어요'
      set('d-' + id, d)
      if (v === 'done' || v === 'later') done += 1
      else if (v === 'now' && P.stage === id && P.total) done += Math.min(1, (P.done || 0) / P.total)
    }
    const pct = Math.round(100 * done / CAN.length)
    $('bar').style.width = pct + '%'; $('bar').parentElement!.setAttribute('aria-valuenow', String(pct))
    set('pct', P.paused ? `${pct}% · 잠시 멈춤` : `${pct}%`)
    set('eta', P.paused ? '1시간 뒤에는 저절로 이어서 해요' : P.minutes_left != null ? (P.minutes_left < 1 ? '이 단계는 곧 끝나요' : `이 단계 약 ${P.minutes_left}분`) : '')
    if (P.files) found = Math.max(found, P.files)
    set('nfiles', found.toLocaleString())
    set('pause', P.paused ? '이어서 하기' : '잠시 멈추기')
    if (sg.names === 'done' && !moved) {   // names are in: searching is useful now
      ($('go') as HTMLButtonElement).disabled = false; set('tiplab', '지금 이렇게 물어보세요')
      if (ready && !stay && !countdown) { countdown = 5; tickDown() }
    }
  }
  const tickDown = () => {
    if (stay || moved) return
    $('auto').innerHTML = `${countdown}초 뒤 검색 화면으로 넘어가요. 살펴보기는 계속돼요. <button class="link" id="stay">여기 머물기</button>`
    $('stay').onclick = () => { stay = true; $('auto').textContent = '여기서 지켜볼게요. 준비되면 언제든 ‘검색 시작하기’를 누르세요.' }
    if (countdown-- <= 0) return goSearch()
    setTimeout(tickDown, 1000)
  }
  const poll = setInterval(async () => {
    try { const t = await invoke<string>('read_progress'); if (t) { P = JSON.parse(t); render() } } catch { /* not written yet */ }
  }, 700)
  unlisten.push(await listen<Line>('index', e => {
    const l = e.payload.line
    log.push(l); if (log.length > 300) log.shift(); set('log', log.join('\n'))
    let m
    if (!engine && l.startsWith('FINDR_ROOT')) {   // the index exists from the first folder on: start the engine now, in parallel
      engine = invoke<string>('start_engine', { photo: photoOn() })
      engine.then(() => { ready = true; render() }).catch(() => {})
    }
    if ((m = l.match(/^FINDR_ROOT (\{.*\})/))) { const r = JSON.parse(m[1]); set('where', `둘러보는 중: ${r.path}`) }
    else if ((m = l.match(/^FINDR_SCAN new\/changed (\d+)/))) { found += +m[1]; set('nfiles', found.toLocaleString()) }
    else if ((m = l.match(/^\s+(\d+) files \d+s/))) { set('nfiles', (found + +m[1]).toLocaleString()) }
    else if (l.startsWith('FINDR_STAGE content')) set('where', '파일 목록 완성. 이제 문서를 읽고 있어요')
  }))
  unlisten.push(await listen<Done>('index-done', e => {
    if (!e.payload.ok) { set('where', e.payload.message); ($('go') as HTMLButtonElement).disabled = false; return }
    P.stages = Object.fromEntries(CAN.map(([id]) => [id, (P.stages || {})[id] === 'later' ? 'later' : 'done'])); P.minutes_left = null; render()
    set('where', '모두 살펴봤어요'); if (!stay) goSearch()
  }))
  $('go').onclick = goSearch
  $('pause').onclick = async () => { const on = !P.paused; await invoke('set_pause', { on }); P.paused = on; render() }
  invoke('start_index', { folders: list, deep })
}

// ---------- search ----------
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
    document.getElementById('later')?.addEventListener('click', () => {
      try { sessionStorage.setItem('findr.updateLater', '1') } catch { /* no storage */ }
      location.hash = 'search'; route()
    })
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
    <div id="upg"></div>
    <h2>사진</h2>
    <div class="card"><label class="switch"><input type="checkbox" id="photo" ${photoOn() ? 'checked' : ''} ${s.gpu !== 'nvidia' ? 'disabled' : ''}>
      <span>사진을 찍힌 내용으로 찾기</span></label>
      <p class="muted" style="margin:8px 0 0">${s.gpu === 'nvidia' ? 'GPU 메모리를 7GB 정도 더 씁니다. 끄면 사진은 이름과 날짜로 찾습니다.' : '이 PC에서는 사진을 이름과 날짜로 찾습니다 (NVIDIA GPU 필요).'}</p></div>
    <h2>AI 도구에 연결</h2>
    <div class="card"><p>Claude 같은 AI 도구가 findr로 이 PC의 파일을 찾게 할 수 있습니다. "지난달 견적서 찾아서 요약해 줘"처럼 말하면, AI가 findr에 물어 파일을 찾고 그 내용을 읽습니다.</p>
      <p class="muted" style="margin:0 0 10px">찾은 파일의 이름과 내용 일부가 그 AI 도구로 전달됩니다. 클라우드 AI라면 그 회사로 보내질 수 있습니다. AI 도구가 findr를 쓸 때마다 묻도록 설정해 두면, 허용한 것만 전달됩니다.</p>
      <div class="mcp"><div class="muted">Claude Code: 터미널에 붙여 넣기</div><pre id="mcp-code"></pre><button data-copy="mcp-code">복사</button></div>
      <div class="mcp"><div class="muted">Claude Desktop: 설정 → 개발자 → 구성 편집에 추가</div><pre id="mcp-desk"></pre><button data-copy="mcp-desk">복사</button></div></div>
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
  upgradeCard()
  // the two lines to paste: this PC's own findr (the app's Python in the data folder) speaking MCP
  const win = navigator.userAgent.includes('Windows'), sep = win ? '\\' : '/'
  const py = s.home + sep + 'runtime' + sep + (win ? 'Scripts' + sep + 'python.exe' : 'bin' + sep + 'python')
  document.getElementById('mcp-code')!.textContent = `claude mcp add findr -- "${py}" -m findr mcp`
  document.getElementById('mcp-desk')!.textContent = JSON.stringify({ mcpServers: { findr: { command: py, args: ['-m', 'findr', 'mcp'] } } }, null, 2)
  view.querySelectorAll<HTMLButtonElement>('[data-copy]').forEach(b => b.onclick = async () => {
    try { await navigator.clipboard.writeText(document.getElementById(b.dataset.copy!)!.textContent || ''); b.textContent = '복사됨' }
    catch { b.textContent = '직접 선택해 복사하세요' }
    setTimeout(() => { b.textContent = '복사' }, 2000)
  })
}

// PCs without a GPU start on the small meaning model; the larger one is computed while nobody uses the PC
async function upgradeCard() {
  const box = document.getElementById('upg')
  if (!box) return
  let u: { on: boolean, done: boolean, reason: string, progress: { done?: number, total?: number, hours_left?: number, switched?: boolean } | null }
  try { u = JSON.parse(await invoke<string>('upgrade_state')) } catch { return }
  const p = u.progress
  if (u.done && !(p && p.switched)) return   // a PC that already uses the larger model (or cannot): nothing to show
  const pct = p && p.total ? Math.floor(100 * (p.done || 0) / p.total) : 0
  const status = p && p.switched ? '더 정확한 의미 모델로 바꾸었습니다.'
    : !u.on ? (u.reason && u.reason !== '꺼짐' ? `하지 않음: ${esc(u.reason)}` : '꺼져 있습니다. 지금 모델로 계속 찾습니다.')
    : p && p.total ? `${pct}% 계산함${p.hours_left ? ` · 쉬는 시간 약 ${Math.ceil(p.hours_left)}시간 남음` : ''}`
    : 'PC를 5분 이상 쓰지 않고 전원이 연결되어 있을 때 시작합니다.'
  box.innerHTML = `<h2>검색 정확도</h2>
    <div class="card"><label class="switch"><input type="checkbox" id="upgon" ${u.on ? 'checked' : ''} ${p && p.switched ? 'disabled' : ''}>
      <span>쉬는 시간에 더 정확한 의미 모델로 바꾸기</span></label>
      <p class="muted" style="margin:8px 0 0">처음에는 빠른 작은 모델로 색인했습니다. PC를 쓰지 않는 동안 10분씩 조금씩 계산하고, 다 끝나면 한 번에 바뀝니다.
        그동안 검색은 지금처럼 됩니다. 모델을 한 번 받습니다(약 1.2GB).</p>
      <p style="margin:8px 0 0">${status}</p></div>`
  document.getElementById('upgon')!.onchange = async ev => {
    await invoke('set_upgrade', { on: (ev.target as HTMLInputElement).checked }); upgradeCard()
  }
}

route()
