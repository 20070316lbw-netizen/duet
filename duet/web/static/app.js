// ─── duet web client ─────────────────────────────────────────────────
// Single-file vanilla JS. No build step. Talks to FastAPI server.

const $  = (s, r=document) => r.querySelector(s);
const $$ = (s, r=document) => [...r.querySelectorAll(s)];

// ── Launcher ─────────────────────────────────────────────────────────
const launcher = $('#launcher');
const providerList = $('#providers');
const sessionsList = $('#sessions-list');
const startBtn = $('#start-btn');
const plannerModelInput = $('#planner-model');
const supModelInput = $('#sup-model');
const supEnabledChk = $('#sup-enabled');
const apiKeyInput = $('#api-key');

let providers = [];
let selectedProvider = null;
let selectedResume = null;

async function loadProviders() {
  try {
    const r = await fetch('/api/providers');
    const data = await r.json();
    if (data.error) {
      providerList.innerHTML = `<div class="loading" style="color:var(--danger)">${escapeHtml(data.detail || data.error)}</div>`;
      return;
    }
    providers = data.providers;
    plannerModelInput.value = data.default_planner_model || '';
    supModelInput.value     = data.default_supervisor_model || '';
    supEnabledChk.checked   = !!data.supervisor_enabled_default;

    if (!providers.length) {
      providerList.innerHTML = `<div class="loading">没有 provider — 检查 ~/.duet/config.toml</div>`;
      return;
    }
    providerList.innerHTML = '';
    for (const p of providers) {
      const el = document.createElement('div');
      el.className = 'provider' + (p.has_key ? ' has-key' : '');
      el.innerHTML = `
        <span class="name">${escapeHtml(p.name)}</span>
        <span class="kind">${escapeHtml(p.kind)}</span>
        <span class="stat">${p.has_key ? '✓ 已就绪 ' + escapeHtml(p.redacted) : '需要 key'}</span>
      `;
      el.addEventListener('click', () => selectProvider(p.name));
      providerList.appendChild(el);
    }
    // 默认选中默认 planner provider
    selectProvider(data.default_planner_provider || providers[0].name);
  } catch (e) {
    providerList.innerHTML = `<div class="loading" style="color:var(--danger)">连接服务器失败:${escapeHtml(String(e))}</div>`;
  }
}

function selectProvider(name) {
  selectedProvider = name;
  $$('.provider', providerList).forEach((el, i) => {
    el.classList.toggle('selected', providers[i]?.name === name);
  });
  const p = providers.find(x => x.name === name);
  if (p && !p.has_key) apiKeyInput.focus();
}

async function loadSessions() {
  try {
    const r = await fetch('/api/sessions');
    const { sessions } = await r.json();
    if (!sessions || !sessions.length) {
      sessionsList.innerHTML = `<div class="loading">(无历史会话)</div>`;
      return;
    }
    sessionsList.innerHTML = '';
    for (const s of sessions) {
      const el = document.createElement('div');
      el.className = 'session';
      const ago = formatAgo(s.last_ts * 1000);
      el.innerHTML = `<span style="color:var(--accent-p)">${escapeHtml(s.id)}</span>
                      <span>${s.msg_count} 条</span>
                      <span style="color:var(--fg-mute);margin-left:auto">${ago}</span>`;
      el.addEventListener('click', () => {
        selectedResume = (selectedResume === s.id) ? null : s.id;
        $$('.session', sessionsList).forEach(e => e.classList.toggle('selected',
          e.firstElementChild.textContent === selectedResume));
      });
      sessionsList.appendChild(el);
    }
  } catch {
    sessionsList.innerHTML = `<div class="loading">加载失败</div>`;
  }
}

$('#toggle-key').addEventListener('click', () => {
  apiKeyInput.type = apiKeyInput.type === 'password' ? 'text' : 'password';
});

startBtn.addEventListener('click', async () => {
  if (!selectedProvider) { alert('请选择一个 provider'); return; }
  const p = providers.find(x => x.name === selectedProvider);
  const key = apiKeyInput.value.trim();
  if (!p.has_key && !key) {
    apiKeyInput.focus();
    apiKeyInput.style.borderColor = 'var(--danger)';
    setTimeout(() => apiKeyInput.style.borderColor = '', 1200);
    return;
  }
  startBtn.disabled = true;
  startBtn.textContent = '启动中…';
  try {
    const r = await fetch('/api/start', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({
        provider: selectedProvider,
        model: plannerModelInput.value.trim() || undefined,
        api_key: key || undefined,
        supervisor_enabled: supEnabledChk.checked,
        supervisor_model: supModelInput.value.trim() || undefined,
        resume_id: selectedResume || undefined,
      }),
    });
    if (!r.ok) {
      const d = await r.json().catch(() => ({}));
      throw new Error(d.detail || `HTTP ${r.status}`);
    }
    const { session_id } = await r.json();
    apiKeyInput.value = '';   // 立刻从 DOM 抹掉
    bootApp(session_id, selectedProvider, plannerModelInput.value.trim(),
            supEnabledChk.checked, supModelInput.value.trim());
  } catch (e) {
    alert('启动失败:' + (e.message || e));
    startBtn.disabled = false;
    startBtn.textContent = '开始对话';
  }
});

// 启动:加载 providers + sessions
loadProviders();
loadSessions();
launcher.showModal();

// ── Main app ─────────────────────────────────────────────────────────
let ws = null;
let interruptRequested = false;
let supervisorActive = false;

const appRoot = $('#app');
const msgsP = $('#msgs-planner');
const msgsS = $('#msgs-supervisor');
const inputEl = $('#input');
const sendBtn = $('#send');
const sideEl  = $('#side');
const sideBody = $('#side-body');
const slashPopup = $('#slash-popup');

// 当前 streaming 容器,按 who 区分
const streamRefs = { planner: null, supervisor: null };

function bootApp(sid, provider, plannerModel, supEnabled, supModel) {
  launcher.close();
  appRoot.hidden = false;
  $('#meta-planner').textContent    = `planner · ${provider}/${plannerModel || '?'}`;
  $('#meta-supervisor').textContent = supEnabled
    ? `supervisor · ${supModel || plannerModel || '?'}`
    : `supervisor · 关`;
  $('#meta-session').textContent    = `session · ${sid}`;
  supervisorActive = supEnabled;
  if (!supEnabled) $('.col-supervisor').classList.add('dim');

  connectWs(sid);
  inputEl.focus();
}

function connectWs(sid) {
  const proto = location.protocol === 'https:' ? 'wss' : 'ws';
  ws = new WebSocket(`${proto}://${location.host}/api/ws/${sid}`);
  ws.addEventListener('open', () => {
    sysMsg('已连接到会话 ' + sid);
    ws.send(JSON.stringify({ t: 'ready' }));
  });
  ws.addEventListener('close', () => sysMsg('连接已关闭'));
  ws.addEventListener('error', () => sysMsg('连接出错'));
  ws.addEventListener('message', (e) => {
    let ev;
    try { ev = JSON.parse(e.data); } catch { return; }
    handleEvent(ev);
  });
}

function sysMsg(text) {
  appendMsg('planner', 'system', text);
}

function handleEvent(ev) {
  switch (ev.t) {
    case 'say':         return onSay(ev.who, ev.text);
    case 'stream_begin':return onStreamBegin(ev.who);
    case 'stream':      return onStream(ev.text);
    case 'stream_end':  return onStreamEnd();
    case 'diff':        return onDiff(ev);
    case 'approval':    return onApproval(ev);
    case 'slash':       return onSlash(ev.commands);
    case 'exit':        return sysMsg('会话已结束');
  }
}

function onSay(who, text) {
  // 把消息按"作者"放到对应列。tool/system → 跟随当前活跃流;否则系统消息双列。
  const target = (who === 'planner' || who === 'supervisor')
    ? who
    : (streamRefs.planner ? 'planner' : (streamRefs.supervisor ? 'supervisor' : 'planner'));
  appendMsg(target, who, text);
  // system 消息也镜像到另一列,保持两边对齐
  if (who === 'system' || who === 'user') {
    const other = target === 'planner' ? 'supervisor' : 'planner';
    appendMsg(other, who, text);
  }
}

function onStreamBegin(who) {
  const col = who === 'supervisor' ? msgsS : msgsP;
  const m = document.createElement('div');
  m.className = `msg ${who} streaming`;
  m.innerHTML = `<div class="who">${who}</div><div class="body"></div>`;
  col.appendChild(m);
  streamRefs[who] = m;
  col.scrollTop = col.scrollHeight;
}

function onStream(text) {
  // 路由到当前正在 stream 的那一列
  const m = streamRefs.planner || streamRefs.supervisor;
  if (!m) return;
  m.querySelector('.body').textContent += text;
  const col = m.parentElement;
  col.scrollTop = col.scrollHeight;
}

function onStreamEnd() {
  for (const who of ['planner','supervisor']) {
    const m = streamRefs[who];
    if (!m) continue;
    m.classList.remove('streaming');
    addCopyBtn(m);
    streamRefs[who] = null;
  }
}

function appendMsg(column, who, text) {
  const col = column === 'supervisor' ? msgsS : msgsP;
  const m = document.createElement('div');
  m.className = `msg ${who}`;
  m.innerHTML = `<div class="who">${escapeHtml(who)}</div>
                 <div class="body">${escapeHtml(text)}</div>`;
  addCopyBtn(m);
  col.appendChild(m);
  col.scrollTop = col.scrollHeight;
}

function addCopyBtn(msgEl) {
  const who = msgEl.querySelector('.who');
  if (!who || who.querySelector('.copy')) return;
  const btn = document.createElement('button');
  btn.className = 'copy';
  btn.title = '复制';
  btn.textContent = '复制';
  btn.addEventListener('click', () => {
    const text = msgEl.querySelector('.body').textContent;
    navigator.clipboard?.writeText(text).then(
      () => { btn.textContent = '✓'; setTimeout(() => btn.textContent = '复制', 1000); },
      () => { btn.textContent = '失败'; },
    );
  });
  who.appendChild(btn);
}

// ── Diff side panel ─────────────────────────────────────────────────
function onDiff(ev) {
  const block = document.createElement('div');
  block.className = 'side-block';
  const ts = new Date().toLocaleTimeString();
  block.innerHTML = `
    <div class="head">
      <span class="kind ${ev.kind}">${ev.kind}</span>
      <span style="flex:1">${escapeHtml(ev.label || '')}</span>
      <span style="color:var(--fg-mute)">${ts}</span>
    </div>
    <pre>${ev.kind === 'file' ? colorDiff(ev.text) : escapeHtml(ev.text || '')}</pre>
  `;
  sideBody.prepend(block);
  // 自动弹出侧栏(只在第一次)
  if (!sideEl.dataset.everOpened) {
    sideEl.dataset.everOpened = '1';
    sideEl.classList.add('open');
  }
}

function colorDiff(text) {
  return escapeHtml(text || '')
    .split('\n')
    .map(l => {
      if (l.startsWith('+++') || l.startsWith('---')) return `<span class="hunk">${l}</span>`;
      if (l.startsWith('@@')) return `<span class="hunk">${l}</span>`;
      if (l.startsWith('+'))  return `<span class="add">${l}</span>`;
      if (l.startsWith('-'))  return `<span class="del">${l}</span>`;
      return l;
    })
    .join('\n');
}

$('#side-close').addEventListener('click', () => sideEl.classList.remove('open'));
$('#btn-side').addEventListener('click', () => sideEl.classList.toggle('open'));
$('#side-clear').addEventListener('click', () => sideBody.innerHTML = '');

// ── Approval dialog ─────────────────────────────────────────────────
function onApproval(ev) {
  const d = $('#approval');
  $('#approval-label').textContent = ev.label || '需要确认';
  $('#approval-body').textContent = JSON.stringify(ev.artifact || {}, null, 2);
  d.returnValue = '';
  d.showModal();
  $('#approve-yes').onclick = (e) => { e.preventDefault(); d.close(); reply(true); };
  $('#approve-no' ).onclick = (e) => { e.preventDefault(); d.close(); reply(false); };
  function reply(ok) {
    ws?.send(JSON.stringify({ t: 'approval_reply', id: ev.id, ok }));
  }
}

// ── Slash commands ──────────────────────────────────────────────────
let slashCommands = [];
let slashIndex = 0;

function onSlash(commands) {
  slashCommands = commands || [];
}

inputEl.addEventListener('input', updateSlashPopup);
inputEl.addEventListener('focus', updateSlashPopup);
inputEl.addEventListener('blur',  () => setTimeout(() => slashPopup.hidden = true, 120));

function updateSlashPopup() {
  const v = inputEl.value;
  if (!v.startsWith('/') || !slashCommands.length) {
    slashPopup.hidden = true;
    return;
  }
  const head = v.split(/\s/)[0];
  const matches = slashCommands.filter(c => c.name.startsWith(head));
  if (!matches.length) { slashPopup.hidden = true; return; }
  slashIndex = Math.min(slashIndex, matches.length - 1);
  slashPopup.innerHTML = matches.map((c, i) => `
    <div class="slash-item ${i === slashIndex ? 'active' : ''}" data-name="${escapeHtml(c.name)}">
      <span class="name">${escapeHtml(c.name)}</span>
      <span class="summary">${escapeHtml(c.summary || '')}${c.usage ? '  · ' + escapeHtml(c.usage) : ''}</span>
    </div>
  `).join('');
  slashPopup.hidden = false;
  $$('.slash-item', slashPopup).forEach(el => {
    el.addEventListener('mousedown', (e) => {
      e.preventDefault();
      inputEl.value = el.dataset.name + ' ';
      inputEl.focus();
      updateSlashPopup();
    });
  });
}

// ── Send / interrupt ────────────────────────────────────────────────
function send() {
  const text = inputEl.value;
  if (!text.trim()) return;
  if (!ws || ws.readyState !== WebSocket.OPEN) {
    sysMsg('未连接,无法发送'); return;
  }
  ws.send(JSON.stringify({ t: 'submit', text }));
  inputEl.value = '';
  slashPopup.hidden = true;
  autosize();
}

sendBtn.addEventListener('click', send);

inputEl.addEventListener('keydown', (e) => {
  // slash popup nav
  if (!slashPopup.hidden) {
    const items = $$('.slash-item', slashPopup);
    if (e.key === 'ArrowDown') { e.preventDefault(); slashIndex = (slashIndex + 1) % items.length; updateSlashPopup(); return; }
    if (e.key === 'ArrowUp')   { e.preventDefault(); slashIndex = (slashIndex - 1 + items.length) % items.length; updateSlashPopup(); return; }
    if (e.key === 'Tab' || e.key === 'Enter') {
      const cur = items[slashIndex];
      if (cur && (e.key === 'Tab' || (e.key === 'Enter' && !inputEl.value.includes(' ')))) {
        e.preventDefault();
        inputEl.value = cur.dataset.name + ' ';
        updateSlashPopup();
        return;
      }
    }
    if (e.key === 'Escape') { slashPopup.hidden = true; return; }
  }
  if (e.key === 'Enter' && !e.shiftKey) {
    e.preventDefault();
    send();
  }
});

inputEl.addEventListener('input', autosize);
function autosize() {
  inputEl.style.height = 'auto';
  inputEl.style.height = Math.min(200, inputEl.scrollHeight) + 'px';
}

$$('.chip').forEach(c => c.addEventListener('click', () => {
  const pre = c.dataset.prefill;
  if (pre.startsWith('/')) inputEl.value = pre;
  else                     inputEl.value = pre + inputEl.value;
  inputEl.focus();
  autosize();
  updateSlashPopup();
}));

$('#btn-interrupt').addEventListener('click', () => {
  if (!ws) return;
  ws.send(JSON.stringify({ t: 'interrupt' }));
});

document.addEventListener('keydown', (e) => {
  if (e.key === 'Escape' && document.activeElement !== inputEl) {
    if (sideEl.classList.contains('open')) sideEl.classList.remove('open');
    else $('#btn-interrupt').click();
  }
});

// ── utils ───────────────────────────────────────────────────────────
function escapeHtml(s) {
  return String(s ?? '')
    .replaceAll('&', '&amp;')
    .replaceAll('<', '&lt;')
    .replaceAll('>', '&gt;')
    .replaceAll('"', '&quot;')
    .replaceAll("'", '&#39;');
}

function formatAgo(ms) {
  const d = Date.now() - ms;
  if (d < 60_000)        return '刚刚';
  if (d < 3_600_000)     return Math.floor(d/60_000) + ' 分钟前';
  if (d < 86_400_000)    return Math.floor(d/3_600_000) + ' 小时前';
  return Math.floor(d/86_400_000) + ' 天前';
}
