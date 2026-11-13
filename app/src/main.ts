// findr desktop screens: first-run setup, folders, indexing, settings. The search page itself comes from the local engine.
import { invoke } from '@tauri-apps/api/core'
import { listen, type UnlistenFn } from '@tauri-apps/api/event'
import { open as pickFolder } from '@tauri-apps/plugin-dialog'

type AppState = { home: string; ready: boolean; indexed: boolean; gpu: string; version: string; folders: string[]; autotest: boolean; update: boolean; setup_running?: boolean }
type Root = { order: number; label: string; path: string }
type Line = { step: string; line: string }
type Done = { ok: boolean; message: string }

const view = document.getElementById('view')!
const esc = (s: string) => s.replace(/[&<>"']/g, c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]!))
// language: the saved choice (Settings), else Korean for a Korean system and English for everyone else
type Lang = 'ko' | 'en'
const pickLang = (): Lang => { try { const v = localStorage.getItem('findr.lang'); if (v === 'ko' || v === 'en') return v } catch { /* no storage */ } return navigator.language.startsWith('ko') ? 'ko' : 'en' }
let LANG = pickLang()
document.documentElement.lang = LANG
const tellLang = () => { invoke('set_lang', { lang: LANG }).catch(() => {}) }   // the app's own messages and menu follow the same language
// a choice made on the search page (another origin, its own storage) is kept by the app in the data folder: it wins here,
// else this screen's own choice is told to the app. (Before, opening this screen turned an English menu back to Korean.)
invoke<string>('saved_lang_choice').then(v => {
  let kept = false
  if ((v === 'ko' || v === 'en') && v !== LANG) {
    try { localStorage.setItem('findr.lang', v); kept = localStorage.getItem('findr.lang') === v } catch { /* private storage off */ }
  }
  if (kept) location.reload()   // only when kept: with storage off a reload would come back to the same place, again and again
  else if (v !== LANG) tellLang()
}).catch(() => tellLang())
const L = (ko: string, en: string) => LANG === 'ko' ? ko : en   // tables below are functions so that a language change redraws them
const GPU = (): Record<string, string> => ({ nvidia: 'NVIDIA GPU', apple: L('Apple 칩', 'Apple silicon'), cpu: L('GPU 없음 (CPU)', 'No GPU (CPU)') })
let unlisten: UnlistenFn[] = []
const clear = () => { unlisten.forEach(u => u()); unlisten = [] }
const photoOn = () => { try { return localStorage.getItem('findr.photo') !== 'off' } catch { return true } }

async function route() {
  clear()
  const s = await invoke<AppState>('app_state')
  const h = location.hash.slice(1)
  // an update that could not finish (offline, a new dependency) keeps the installed engine usable for this session
  let later = false; try { later = sessionStorage.getItem('findr.updateLater') === '1' } catch { /* no storage */ }
  // a setup is running (the menu loaded this page anew in the middle of it): back to its screen, whatever was asked.
  // Before, this showed the welcome screen, and '준비 시작' there started a second setup beside the first (field, Windows)
  if (s.setup_running) return setup(s)
  if (!s.ready && !(s.update && later)) return h === 'setup' || s.update ? setup(s) : welcome(s)   // a newer engine in the app: update without asking
  if (h === 'settings') return settings(s)
  if ((h === 'index' || h === 'deep') && s.folders.length) return indexing(s.folders, h === 'deep')
  if (h === 'folders' || !s.indexed) return folders(s)
  return search()
}
window.addEventListener('hashchange', route)

// ---------- first run ----------
function welcome(s: AppState) {
  const size = s.gpu === 'nvidia' ? L('약 6~13GB', 'about 6–13 GB') : L('약 3~4GB', 'about 3–4 GB')   // a Mac starts on the small meaning model too (the large one comes later, in idle time)
  view.innerHTML = `
    <h1>findr</h1>
    <p class="lead">${L('기억나는 대로 물으면 내 PC에서 파일을 찾아 줍니다. 파일 내용과 질문은 이 PC 밖으로 나가지 않습니다.', 'Describe a file the way you remember it, and findr finds it on your PC. Your files and questions never leave this PC.')}</p>
    <div class="card">
      <p>${L('처음 한 번, 검색에 필요한 구성 요소를 이 PC에 맞게 준비합니다.', 'A one-time setup downloads what search needs, matched to this PC.')}</p>
      <dl>
        <dt>${L('이 PC', 'This PC')}</dt><dd>${esc(GPU()[s.gpu] || s.gpu)}</dd>
        <dt>${L('받는 용량', 'Download size')}</dt><dd>${size} ${L('(인터넷 속도에 따라 5~20분)', '(5–20 min, depending on your connection)')}</dd>
        <dt>${L('저장 위치', 'Stored in')}</dt><dd>${esc(s.home)}</dd>
      </dl>
      <p class="muted" style="margin-top:12px">${L('다른 프로그램이나 시스템 설정은 바꾸지 않습니다. 위 폴더를 지우면 모두 사라집니다.', 'No other programs or system settings are changed. Deleting the folder above removes everything.')}</p>
    </div>
    <div class="actions"><button class="primary" id="go">${L('준비 시작', 'Start setup')}</button></div>`
  document.getElementById('go')!.onclick = () => { location.hash = 'setup' }
  if (s.autotest) setTimeout(() => document.getElementById('go')?.click(), 3000)
}

const STEPS = (): [string, string][] => [['vcrt', L('Windows 구성 요소', 'Windows components')], ['env', L('파이썬 환경', 'Python environment')], ['torch', L('연산 라이브러리', 'Compute library')], ['engine', L('검색 엔진', 'Search engine')], ['models', L('모델 받기', 'Model download')]]

async function setup(s: AppState) {
  // an update on a PC that already has an index and folders: back to search (the folder screen again was a needless question,
  // field 0.2.13); the background refresh picks up changes as before
  const keep = s.update && s.indexed && s.folders.length > 0
  view.innerHTML = `
    <h1>${s.update ? L('새 버전을 준비하는 중', 'Getting the new version ready') : L('준비하는 중', 'Setting up')}</h1>
    <p class="lead">${L('창을 닫지 말고 기다려 주세요. 끝나면', 'Please keep this window open. When this finishes,')} ${keep ? L('검색 화면으로 돌아갑니다', 'you will go back to search') : L('색인할 폴더를 고르는 화면으로 넘어갑니다', 'you will choose the folders to index')}.</p>
    <div class="card"><ul class="steps">${STEPS().map(([id, name]) => `
      <li id="st-${id}"><span class="state">${L('대기', 'Waiting')}</span><div><div class="step-name">${name}</div><div class="step-line"></div></div></li>`).join('')}
    </ul></div>
    <div id="msg"></div>
    <details><summary>${L('자세한 기록', 'Details')}</summary><pre id="log"></pre></details>`
  const log: string[] = []
  let cur = ''
  const mark = (id: string, cls: string, text: string) => {
    const li = document.getElementById('st-' + id); if (!li) return
    const st = li.querySelector('.state')!; st.className = 'state ' + cls; st.textContent = text
  }
  const show = ({ step, line }: Line) => {
    if (step !== cur) { if (cur) mark(cur, 'ok', L('완료', 'Done')); cur = step; mark(step, 'run', L('진행 중', 'In progress')) }
    const li = document.getElementById('st-' + step)
    if (li) li.querySelector('.step-line')!.textContent = line
    log.push(line); if (log.length > 300) log.shift()
    document.getElementById('log')!.textContent = log.join('\n')
  }
  unlisten.push(await listen<Line>('setup', e => show(e.payload)))
  const finish = (d: Done) => {
    if (d.ok) {
      if (cur) mark(cur, 'ok', L('완료', 'Done'))
      const to = keep ? 'search' : 'folders'
      if (location.hash === '#' + to) route(); else location.hash = to
      return
    }
    if (cur) mark(cur, 'fail', L('실패', 'Failed'))
    document.getElementById('msg')!.innerHTML = `<div class="notice err">${esc(d.message)}</div>
      <div class="actions"><button class="primary" id="retry">${L('다시 시도', 'Try again')}</button>${s.update ? `<button id="later">${L('지금은 이전 버전으로 검색', 'Search with the previous version for now')}</button>` : ''}</div>`
    // a new run from the setup screen itself (after a menu visit the address can be #search, which leads to the welcome screen)
    document.getElementById('retry')!.onclick = () => { if (location.hash === '#setup') route(); else location.hash = 'setup' }
    document.getElementById('later')?.addEventListener('click', () => {
      try { sessionStorage.setItem('findr.updateLater', '1') } catch { /* no storage */ }
      location.hash = 'search'; route()
    })
  }
  unlisten.push(await listen<Done>('setup-done', e => finish(e.payload)))
  // joining a setup already running: what it said before this screen was drawn, then the rest as it comes; never a second one
  let now: { running: boolean; lines: Line[]; done: Done | null } = { running: false, lines: [], done: null }
  try { now = await invoke('setup_status') } catch { /* cannot ask: start as before */ }
  if (now.running || (s.setup_running && now.done)) {
    now.lines.forEach(show); if (!cur) { mark('vcrt', 'run', L('진행 중', 'In progress')); cur = 'vcrt' }
    if (!now.running && now.done) finish(now.done)   // it ended while this screen was being drawn: its result, not a new run
    return
  }
  mark('vcrt', 'run', L('진행 중', 'In progress')); cur = 'vcrt'
  invoke('setup_runtime')
}

// ---------- folders ----------
async function folders(s: AppState) {
  view.innerHTML = `<h1>${L('어디를 찾을까요', 'Where should findr look?')}</h1>
    <p class="lead">${L('처음에는 PC 전체에서 내 파일이 있는 폴더를 고릅니다. 시스템·프로그램 폴더와 다른 사용자 계정은 자동으로 빠집니다.', 'To start, findr picks the folders across this PC that hold your files. System and program folders and other user accounts are left out automatically.')}</p>
    <div class="card"><div class="row"><div class="spinner"></div><span class="muted">${L('폴더를 살펴보는 중입니다', 'Looking through your folders')}</span></div></div>`
  let roots: Root[] = []
  try { roots = JSON.parse(await invoke<string>('list_roots')) } catch (e) {
    view.querySelector('.card')!.innerHTML = `<div class="notice err">${L('폴더 목록을 읽지 못했습니다:', 'Could not read the folder list:')} ${esc(String(e))}</div>
      <div class="actions"><button class="primary" id="rr">${L('다시 시도', 'Try again')}</button>${s.indexed ? `<button id="bk">${L('검색으로', 'Back to search')}</button>` : ''}</div>`;
    document.getElementById('rr')!.onclick = () => { route() }
    document.getElementById('bk')?.addEventListener('click', () => { location.hash = 'search' }); return
  }
  const chosen = new Set(s.folders.length ? s.folders : roots.map(r => r.path))
  const extra = s.folders.filter(f => !roots.some(r => r.path === f)).map(f => ({ order: 9, label: L('추가한 폴더', 'Added folder'), path: f }))
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
      <button class="primary" id="start">${L('색인 시작', 'Start indexing')}</button><button id="add">${L('폴더 추가', 'Add folder')}</button>
      ${s.indexed ? `<button id="back">${L('취소', 'Cancel')}</button>` : ''}</div>
    <p class="muted" style="margin-top:14px">${L('찾을 가능성이 높은 폴더부터 처리합니다. 색인 중에도 검색할 수 있습니다.', 'The folders you are most likely to search come first. You can search while indexing runs.')}</p>`)
  const sync = () => { (document.getElementById('start') as HTMLButtonElement).disabled = chosen.size === 0 }
  draw(); sync()
  if (s.autotest) setTimeout(() => document.getElementById('start')?.click(), 5000)
  document.getElementById('add')!.onclick = async () => {
    const p = await pickFolder({ directory: true, multiple: false, title: L('추가할 폴더', 'Choose a folder to add') })
    if (typeof p === 'string' && !all.some(r => r.path === p)) { all.push({ order: 9, label: L('추가한 폴더', 'Added folder'), path: p }); chosen.add(p); draw(); sync() }
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
const TIPS = () => [L('지난달 받은 견적서', 'the quote I got last month'), L('작년 여름 바다에서 찍은 사진', 'photos from the beach last summer'), L('최종본 말고 그 직전 판', 'the version right before the final one'), L('회의 전에 고친 발표 자료', 'the slides I edited before the meeting'), L('카톡으로 받은 계약서 스캔', 'the scanned contract I got over chat'), L('출장비 한도가 적힌 규정', 'the policy with the travel expense limit')]
const CAN = (): [string, string, string][] => [   // stage, what the person can do once it is ready, what is happening meanwhile
  ['names', L('파일 이름으로 찾기', 'Find by file name'), L('파일 목록을 만드는 중', 'Listing your files')],
  ['namevec', L('비슷한 말로 이름 찾기', 'Find names by similar words'), L('이름의 뜻을 파악하는 중', 'Learning what the names mean')],
  ['content', L('문서 속 글자로 찾기', 'Find by text inside documents'), L('문서를 읽는 중', 'Reading documents')],
  ['meaning', L('기억나는 대로 물어보기', 'Ask the way you remember it'), L('내용의 뜻을 파악하는 중', 'Learning what the contents mean')],
  ['ocr', L('스캔·스크린샷 속 글자 찾기', 'Find text in scans and screenshots'), L('스캔 글자를 읽는 중', 'Reading text in scans')],
]
type Progress = { stage?: string; done?: number; total?: number; minutes_left?: number | null; files?: number | null; paused?: boolean; pausing?: boolean
  stages?: Record<string, string>; t?: number; started?: number; scanned?: number; rate?: number; folder?: string }
// '약 3분', '1분 안에', '약 1시간 20분': the time left of a stage, said plainly
const left = (m: number) => m < 1 ? L('1분 안에 끝나요', 'less than a minute left') : m < 60 ? L(`약 ${Math.round(m)}분 남았어요`, `about ${Math.round(m)} min left`) : L(`약 ${Math.floor(m / 60)}시간 ${Math.round(m % 60)}분 남았어요`, `about ${Math.floor(m / 60)} h ${Math.round(m % 60)} min left`)
const clock = (s: number) => s < 60 ? L(`${Math.floor(s)}초`, `${Math.floor(s)} s`) : s < 3600 ? L(`${Math.floor(s / 60)}분 ${Math.floor(s % 60)}초`, `${Math.floor(s / 60)} min ${Math.floor(s % 60)} s`) : L(`${Math.floor(s / 3600)}시간 ${Math.floor(s % 3600 / 60)}분`, `${Math.floor(s / 3600)} h ${Math.floor(s % 3600 / 60)} min`)
const ICON: Record<string, string> = { done: '✓', now: '●', later: '◷', wait: '○' }
const STATE = (): Record<string, string> => ({ done: L('준비됨', 'Ready'), now: L('진행 중', 'In progress'), later: L('쉬는 시간에', 'When idle'), wait: L('대기', 'Waiting') })

async function indexing(list: string[], deep = false) {
  view.innerHTML = `<h1>${L('내 파일을 살펴보는 중', 'Looking through your files')}</h1>
    <p class="lead">${L('이 PC 안에서만 처리해요. 파일 목록이 만들어지면 바로 검색할 수 있어요.', 'Everything stays on this PC. You can search as soon as the file list is ready.')}</p>
    <div class="card hero" aria-live="polite">
      <div class="big"><span id="nfiles">0</span><span class="unit">${L('개 파일을 찾았어요', 'files found')}</span></div>
      <div class="muted where" id="where">${L('폴더를 둘러보는 중', 'Looking through folders')}</div>
      <div class="bar" role="progressbar" aria-label="${L('전체 진행', 'Overall progress')}" aria-valuemin="0" aria-valuemax="100"><div id="bar"></div></div>
      <div class="row between muted"><span id="pct">0%</span><span id="eta"></span></div>
      <div class="muted elapsed" id="elapsed"></div>
    </div>
    <h2>${L('지금 할 수 있는 것', 'What you can do now')}</h2>
    <div class="card"><ul class="ladder">${CAN().map(([id, can, doing]) => `
      <li id="ix-${id}" class="wait"><span class="ic" aria-hidden="true">${ICON.wait}</span>
        <div><div class="can">${can}</div><div class="doing" id="d-${id}">${L('앞 단계가 끝나면 시작해요', 'Starts after the previous step')}</div></div><span class="st">${STATE().wait}</span></li>`).join('')}</ul></div>
    <div class="card tip"><span class="muted" id="tiplab">${L('준비되면 이렇게 물어보세요', 'Once it is ready, try asking')}</span><p id="tip"></p></div>
    <div class="actions"><button class="primary" id="go" disabled>${L('검색 시작하기', 'Start searching')}</button><button id="pause">${L('잠시 멈추기', 'Pause')}</button></div>
    <p class="muted auto" id="auto"></p>
    <details><summary>${L('자세한 기록', 'Details')}</summary><pre id="log"></pre></details>`
  const log: string[] = []
  let found = 0, engine: Promise<string> | null = null, moved = false, ready = false, stay = false, countdown = 0, P: Progress = {}, pollErr = false
  const $ = (id: string) => document.getElementById(id)!
  const set = (id: string, t: string) => { const e = document.getElementById(id); if (e) e.textContent = t }
  let ti = 0; const tip = () => set('tip', '“' + TIPS()[ti++ % TIPS().length] + '”'); tip()
  const tipTimer = setInterval(tip, 6000)
  const goSearch = async () => {   // the engine was started in advance: no second wait
    if (moved) return; moved = true; clearInterval(tipTimer); clearInterval(poll)
    try { const url = await (engine || invoke<string>('start_engine', { photo: photoOn() })); location.href = url }
    catch (e) { location.hash = 'search' }
  }
  const render = () => {
    const sg = P.stages || {}
    let done = 0
    for (const [id, , doing] of CAN()) {
      const v = sg[id] || 'wait', li = $('ix-' + id)
      li.className = v; li.querySelector('.ic')!.textContent = ICON[v]
      li.querySelector('.st')!.textContent = v === 'now' && P.paused ? L('멈춤', 'Paused') : STATE()[v]
      let d = v === 'wait' ? L('앞 단계가 끝나면 시작해요', 'Starts after the previous step') : doing
      if (v === 'now' && P.stage === id && id === 'names' && P.scanned)   // the total is unknown until the walk ends: what was looked at, how fast
        d += L(` · ${Number(P.scanned).toLocaleString()}개 확인${P.rate ? ` · 초당 ${Math.round(P.rate).toLocaleString()}개` : ''}`, ` · ${Number(P.scanned).toLocaleString()} checked${P.rate ? ` · ${Math.round(P.rate).toLocaleString()}/s` : ''}`)
      else if (v === 'now' && P.stage === id && P.total) d += ` · ${Number(P.done).toLocaleString()}/${Number(P.total).toLocaleString()}${P.minutes_left != null ? ' · ' + left(P.minutes_left) : ''}`
      if (v === 'later') d = L('PC를 쓰지 않을 때 조금씩 읽어요', 'Read a little at a time while the PC is idle')
      if (v === 'done') d = L('지금 할 수 있어요', 'Available now')
      set('d-' + id, d)
      if (v === 'done' || v === 'later') done += 1
      else if (v === 'now' && P.stage === id && P.total) done += Math.min(1, (P.done || 0) / P.total)
    }
    const pct = Math.round(100 * done / CAN().length)
    $('bar').style.width = pct + '%'; $('bar').parentElement!.setAttribute('aria-valuenow', String(pct))
    // the name scan cannot know its end: a moving bar instead of a percentage that sits at 0
    $('bar').parentElement!.classList.toggle('busy', P.stage === 'names' && sg.names === 'now' && !P.paused)
    if (P.stage === 'names' && P.folder) set('where', L(`둘러보는 중: ${P.folder}`, `Looking in: ${P.folder}`))
    if (P.started) set('elapsed', L(`시작한 지 ${clock(Date.now() / 1000 - P.started)}`, `${clock(Date.now() / 1000 - P.started)} elapsed`) + (P.t && Date.now() / 1000 - P.t > 20 && !P.paused ? L(' · 큰 파일을 처리하는 중이에요', ' · working on a large file') : ''))
    set('pct', P.pausing ? L(`${pct}% · 멈추는 중…`, `${pct}% · pausing…`) : P.paused ? L(`${pct}% · 잠시 멈춤`, `${pct}% · paused`) : `${pct}%`)
    set('eta', P.pausing ? L('읽고 있던 파일을 마치고 멈춰요', 'Stopping after the file it is reading') : P.paused ? L('1시간 뒤에는 저절로 이어서 해요', 'Resumes on its own in an hour') : P.minutes_left != null ? L(`이 단계 ${left(P.minutes_left)}`, `This step: ${left(P.minutes_left)}`) : P.stage === 'names' ? L('파일 목록이 끝나면 남은 시간을 알려 드려요', 'Time left appears once the file list is done') : '')
    if (P.files) found = Math.max(found, P.files)
    if (P.scanned) found = Math.max(found, P.scanned)
    set('nfiles', found.toLocaleString())
    set('pause', P.paused ? L('이어서 하기', 'Resume') : L('잠시 멈추기', 'Pause'))
    if (sg.names === 'done' && !moved) {   // names are in: searching is useful now
      ($('go') as HTMLButtonElement).disabled = false; set('tiplab', L('지금 이렇게 물어보세요', 'Try asking like this now'))
      if (ready && !stay && !countdown) { countdown = 5; tickDown() }
    }
  }
  const tickDown = () => {
    if (stay || moved) return
    $('auto').innerHTML = L(`${countdown}초 뒤 검색 화면으로 넘어가요. 살펴보기는 계속돼요. <button class="link" id="stay">여기 머물기</button>`, `Opening search in ${countdown} s. Indexing keeps going. <button class="link" id="stay">Stay here</button>`)
    $('stay').onclick = () => { stay = true; $('auto').textContent = L('여기서 지켜볼게요. 준비되면 언제든 ‘검색 시작하기’를 누르세요.', 'Staying here. Click “Start searching” whenever you are ready.') }
    if (countdown-- <= 0) return goSearch()
    setTimeout(tickDown, 1000)
  }
  const poll = setInterval(async () => {
    // a refused or failing command is said in the detailed log once: swallowed, it left the bar at 0% for the whole first index
    try { const t = await invoke<string>('read_progress'); if (t) { P = JSON.parse(t); render() } }
    catch (e) { if (!pollErr) { pollErr = true; log.push('read_progress: ' + String(e)); set('log', log.join('\n')) } }
  }, 700)
  unlisten.push(await listen<Line>('index', e => {
    const l = e.payload.line
    log.push(l); if (log.length > 300) log.shift(); set('log', log.join('\n'))
    let m
    if (!engine && l.startsWith('FINDR_ROOT')) {   // the index exists from the first folder on: start the engine now, in parallel
      engine = invoke<string>('start_engine', { photo: photoOn() })
      engine.then(() => { ready = true; render() }).catch(() => {})
    }
    if ((m = l.match(/^FINDR_ROOT (\{.*\})/))) { const r = JSON.parse(m[1]); set('where', L(`둘러보는 중: ${r.path}`, `Looking in: ${r.path}`)) }
    else if ((m = l.match(/^FINDR_SCAN new\/changed (\d+)/))) { found += +m[1]; set('nfiles', found.toLocaleString()) }
    else if ((m = l.match(/^\s+(\d+) files \d+s/))) { set('nfiles', (found + +m[1]).toLocaleString()) }
    else if (l.startsWith('FINDR_STAGE content')) set('where', L('파일 목록 완성. 이제 문서를 읽고 있어요', 'File list done. Now reading documents'))
  }))
  unlisten.push(await listen<Done>('index-done', e => {
    if (!e.payload.ok) { set('where', e.payload.message); ($('go') as HTMLButtonElement).disabled = false; return }
    P.stages = Object.fromEntries(CAN().map(([id]) => [id, (P.stages || {})[id] === 'later' ? 'later' : 'done'])); P.minutes_left = null; render()
    set('where', L('모두 살펴봤어요', 'All done')); if (!stay) goSearch()
  }))
  $('go').onclick = goSearch
  $('pause').onclick = async () => { const on = !P.paused; await invoke('set_pause', { on }); P.paused = on; P.pausing = on; render() }
  invoke('start_index', { folders: list, deep })
}

// ---------- search ----------
async function search() {
  view.innerHTML = `<h1>${L('검색을 준비하는 중', 'Getting search ready')}</h1>
    <div class="card"><div class="row"><div class="spinner"></div><span>${L('검색 엔진을 켜고 있습니다. 처음에는 1분 정도 걸립니다.', 'Starting the search engine. The first start takes about a minute.')}</span></div></div>`
  try {
    const url = await invoke<string>('start_engine', { photo: photoOn() })
    const s = await invoke<AppState>('app_state')
    location.href = s.autotest ? url + '&q=' + encodeURIComponent('고객사 미팅용 견적서') : url
  } catch (e) {
    view.innerHTML = `<h1>${L('검색 엔진을 켜지 못했습니다', 'Could not start the search engine')}</h1><div class="notice err">${esc(String(e))}</div>
      <div class="actions"><button class="primary" id="retry">${L('다시 시도', 'Try again')}</button><button id="set">${L('설정', 'Settings')}</button></div>`
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
  view.innerHTML = `<h1>${L('폴더와 설정', 'Folders and settings')}</h1>
    <p class="lead">findr ${esc(s.version)}</p>
    <div class="card"><dl>
      <dt>${L('이 PC', 'This PC')}</dt><dd>${esc(GPU()[s.gpu] || s.gpu)}</dd>
      <dt>${L('데이터 폴더', 'Data folder')}</dt><dd>${esc(s.home)}</dd>
      <dt>${L('색인 폴더', 'Indexed folders')}</dt><dd>${s.folders.length ? s.folders.map(esc).join('<br>') : L('PC 전체의 내 파일', 'Your files across this PC')}</dd></dl></div>
    <h2>${L('색인', 'Indexing')}</h2>
    <div class="card"><p>${L('새로 생기거나 바뀐 파일만 다시 읽습니다.', 'Only new or changed files are read again.')}</p>
      <div class="actions" style="margin-top:6px"><button class="primary" id="re">${L('바뀐 파일 반영', 'Update changed files')}</button><button id="fold">${L('폴더 바꾸기', 'Change folders')}</button></div>
      <p class="muted" style="margin:14px 0 6px">${L('findr가 열려 있는 동안 바뀐 파일은 10분마다(배터리일 때는 이름만), 긴 문서의 뒷부분은 처음 한 번 낮은 우선순위로 알아서 반영합니다.', 'While findr is open, changed files are picked up every 10 minutes (names only on battery), and the later parts of long documents are added once, at low priority.')}
        ${L('지금 바로 하고 싶을 때만 아래를 누르세요.', 'Use the button below only if you want it done right now.')}</p>
      <div class="actions" style="margin-top:0"><button id="deep">${L('깊은 내용까지 계산', 'Index full contents now')}</button></div></div>
    <div id="upg"></div>
    <h2>${L('사진', 'Photos')}</h2>
    <div class="card"><label class="switch"><input type="checkbox" id="photo" ${photoOn() ? 'checked' : ''} ${s.gpu !== 'nvidia' ? 'disabled' : ''}>
      <span>${L('사진을 찍힌 내용으로 찾기', 'Find photos by what is in them')}</span></label>
      <p class="muted" style="margin:8px 0 0">${s.gpu === 'nvidia' ? L('GPU 메모리를 7GB 정도 더 씁니다. 끄면 사진은 이름과 날짜로 찾습니다.', 'Uses about 7 GB more GPU memory. When off, photos are found by name and date.') : L('이 PC에서는 사진을 이름과 날짜로 찾습니다 (NVIDIA GPU 필요).', 'On this PC, photos are found by name and date (needs an NVIDIA GPU).')}</p></div>
    <h2>${L('AI 도구에 연결', 'Connect to AI tools')}</h2>
    <div class="card"><p>${L('Claude 같은 AI 도구가 findr로 이 PC의 파일을 찾게 할 수 있습니다. "지난달 견적서 찾아서 요약해 줘"처럼 말하면, AI가 findr에 물어 파일을 찾고 그 내용을 읽습니다.', 'AI tools such as Claude can use findr to find files on this PC. Say something like "find the quote from last month and summarize it", and the AI asks findr for the file and reads it.')}</p>
      <p class="muted" style="margin:0 0 10px">${L('찾은 파일의 이름과 내용 일부가 그 AI 도구로 전달됩니다. 클라우드 AI라면 그 회사로 보내질 수 있습니다. AI 도구가 findr를 쓸 때마다 묻도록 설정해 두면, 허용한 것만 전달됩니다.', 'The names of the files found and parts of their contents are passed to that AI tool. With a cloud AI, they may be sent to its provider. If you set the AI tool to ask each time it uses findr, only what you approve is passed on.')}</p>
      <div class="mcp"><div class="muted">${L('Claude Code: 터미널에 붙여 넣기', 'Claude Code: paste into a terminal')}</div><pre id="mcp-code"></pre><button data-copy="mcp-code">${L('복사', 'Copy')}</button></div>
      <div class="mcp"><div class="muted">${L('Claude Desktop: 설정 → 개발자 → 구성 편집에 추가', 'Claude Desktop: add under Settings → Developer → Edit Config')}</div><pre id="mcp-desk"></pre><button data-copy="mcp-desk">${L('복사', 'Copy')}</button></div></div>
    <h2>${L('언어', 'Language')}</h2>
    <div class="card"><select id="lang" aria-label="${L('언어', 'Language')}"><option value="ko" ${LANG === 'ko' ? 'selected' : ''}>한국어</option><option value="en" ${LANG === 'en' ? 'selected' : ''}>English</option></select></div>
    <h2>${L('데이터', 'Data')}</h2>
    <div class="card"><p>${L('색인, 설정, findr가 배운 것은 모두 데이터 폴더에 있습니다.', 'The index, settings, and everything findr has learned are in the data folder.')}</p>
      <div class="actions" style="margin-top:6px"><button id="data">${L('데이터 폴더 열기', 'Open data folder')}</button></div></div>
    <h2>${L('findr 지우기', 'Remove findr')}</h2>
    <div class="card"><p>${L('색인, 내려받은 모델, 검색 엔진, 설정, 기록을 이 PC에서 모두 지웁니다. 내 파일은 건드리지 않습니다.', 'Deletes the index, downloaded models, search engine, settings, and history from this PC. Your own files are not touched.')}</p>
      <div class="actions" style="margin-top:6px"><button id="un">${L('findr 데이터 지우기…', 'Delete findr data…')}</button></div><div id="unbox"></div></div>
    <div class="actions"><button class="primary" id="back">${L('검색으로 돌아가기', 'Back to search')}</button></div>`
  document.getElementById('re')!.onclick = () => { invoke('stop_engine'); location.hash = 'index' }
  document.getElementById('deep')!.onclick = () => { invoke('stop_engine'); location.hash = 'deep' }
  document.getElementById('fold')!.onclick = () => { location.hash = 'folders' }
  document.getElementById('data')!.onclick = () => invoke('open_data_dir')
  document.getElementById('un')!.onclick = () => {   // two steps: nothing is removed by one click
    const box = document.getElementById('unbox')!
    box.innerHTML = `<div class="notice err">${L(`데이터 폴더(${s.home})를 지웁니다. 다시 쓰려면 모델을 다시 받고 처음부터 색인해야 합니다.`, `This deletes the data folder (${s.home}). To use findr again, you will have to download the models and index from the start.`)}
      <div class="actions" style="margin-top:8px"><button class="primary" id="unyes">${L('지우기', 'Delete')}</button><button id="unno">${L('취소', 'Cancel')}</button></div></div>`
    document.getElementById('unno')!.onclick = () => { box.innerHTML = '' }
    document.getElementById('unyes')!.onclick = async () => {
      box.innerHTML = `<div class="notice">${L('지우는 중이에요…', 'Deleting…')}</div>`
      try {
        const where = await invoke<string>('uninstall_data')
        const win = navigator.userAgent.includes('Windows'), mac = navigator.userAgent.includes('Mac')
        const how = win ? L('Windows 설정 → 앱 → 설치된 앱에서 findr를 제거하세요(앱 파일은 그때 지워져요).', 'uninstall findr in Windows Settings → Apps → Installed apps (the app files are removed then).') : mac ? L('응용 프로그램 폴더의 findr를 휴지통으로 옮기세요.', 'move findr from the Applications folder to the Trash.') : L('설치한 방법(패키지 관리자, AppImage 파일)대로 findr를 지우세요.', 'remove findr the way you installed it (package manager or AppImage file).')
        box.innerHTML = `<div class="notice">${L(`데이터를 모두 지웠어요 (${where}). 마지막으로 앱을 지우려면:`, `All data is deleted (${where}). To remove the app itself:`)} ${how}
          <div class="actions" style="margin-top:8px"><button class="primary" id="unquit">${L('앱 닫기', 'Close app')}</button></div></div>`
        document.getElementById('unquit')!.onclick = () => invoke('quit_app')
      } catch (e) { box.innerHTML = `<div class="notice err">${String(e)}</div>` }
    }
  }
  document.getElementById('photo')!.onchange = ev => {
    try { localStorage.setItem('findr.photo', (ev.target as HTMLInputElement).checked ? 'on' : 'off') } catch { /* private storage off */ }
  }
  document.getElementById('back')!.onclick = () => { location.hash = 'search'; if (location.hash === '#search') route() }
  document.getElementById('lang')!.onchange = ev => {   // saved, then the screen is drawn again in the chosen language
    LANG = (ev.target as HTMLSelectElement).value === 'ko' ? 'ko' : 'en'; document.documentElement.lang = LANG
    try { localStorage.setItem('findr.lang', LANG) } catch { /* private storage off */ }
    tellLang()
    route()
  }
  upgradeCard()
  // the two lines to paste: this PC's own findr (the app's Python in the data folder) speaking MCP
  const win = navigator.userAgent.includes('Windows'), sep = win ? '\\' : '/'
  const py = s.home + sep + 'runtime' + sep + (win ? 'Scripts' + sep + 'python.exe' : 'bin' + sep + 'python')
  document.getElementById('mcp-code')!.textContent = `claude mcp add findr -- "${py}" -m findr mcp`
  document.getElementById('mcp-desk')!.textContent = JSON.stringify({ mcpServers: { findr: { command: py, args: ['-m', 'findr', 'mcp'] } } }, null, 2)
  view.querySelectorAll<HTMLButtonElement>('[data-copy]').forEach(b => b.onclick = async () => {
    try { await navigator.clipboard.writeText(document.getElementById(b.dataset.copy!)!.textContent || ''); b.textContent = L('복사됨', 'Copied') }
    catch { b.textContent = L('직접 선택해 복사하세요', 'Select the text and copy it') }
    setTimeout(() => { b.textContent = L('복사', 'Copy') }, 2000)
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
  const status = p && p.switched ? L('더 정확한 의미 모델로 바꾸었습니다.', 'Switched to the more accurate model.')
    : !u.on ? (u.reason && u.reason !== '꺼짐' ? L(`하지 않음: ${esc(u.reason)}`, `Not running: ${esc(u.reason)}`) : L('꺼져 있습니다. 지금 모델로 계속 찾습니다.', 'Off. Search keeps using the current model.'))
    : p && p.total ? L(`${pct}% 계산함${p.hours_left ? ` · 쉬는 시간 약 ${Math.ceil(p.hours_left)}시간 남음` : ''}`, `${pct}% done${p.hours_left ? ` · about ${Math.ceil(p.hours_left)} h of idle time left` : ''}`)
    : L('PC를 5분 이상 쓰지 않고 전원이 연결되어 있을 때 시작합니다.', 'Starts when the PC has been idle for 5 minutes and is plugged in.')
  box.innerHTML = `<h2>${L('검색 정확도', 'Search accuracy')}</h2>
    <div class="card"><label class="switch"><input type="checkbox" id="upgon" ${u.on ? 'checked' : ''} ${p && p.switched ? 'disabled' : ''}>
      <span>${L('쉬는 시간에 더 정확한 의미 모델로 바꾸기', 'Switch to a more accurate model during idle time')}</span></label>
      <p class="muted" style="margin:8px 0 0">${L('처음에는 빠른 작은 모델로 색인했습니다. PC를 쓰지 않는 동안 10분씩 조금씩 계산하고, 다 끝나면 한 번에 바뀝니다.', 'Indexing started with a small, fast model. The more accurate one is computed 10 minutes at a time while the PC is idle, and findr switches over once it is all done.')}
        ${L('그동안 검색은 지금처럼 됩니다. 모델을 한 번 받습니다(약 1.2GB).', 'Search works as it does now in the meantime. The model is downloaded once (about 1.2 GB).')}</p>
      <p style="margin:8px 0 0">${status}</p></div>`
  document.getElementById('upgon')!.onchange = async ev => {
    await invoke('set_upgrade', { on: (ev.target as HTMLInputElement).checked }); upgradeCard()
  }
}

route()
