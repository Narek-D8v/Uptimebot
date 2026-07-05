const API_BASE = '/api/v1';
let initData = '';
let userData = null;
let monitorsCache = [];
let chartInstance = null;
let _audioCtx = null;
function getAudioCtx() {
  if (!_audioCtx) _audioCtx = new (window.AudioContext || window.webkitAudioContext)();
  return _audioCtx;
}

const Toast = {
  el: null,
  show(msg, type = 'info', duration = 2800) {
    if (!this.el) { this.el = document.getElementById('snackbar') }
    this.el.textContent = msg;
    this.el.className = 'snackbar show ' + type;
    clearTimeout(this.el._timer);
    this.el._timer = setTimeout(() => this.el.classList.remove('show'), duration);
  }
};

function playBeep(freq = 880, duration = 200) {
  try {
    const ctx = getAudioCtx();
    if (ctx.state === 'suspended') ctx.resume();
    const osc = ctx.createOscillator();
    const gain = ctx.createGain();
    osc.type = 'sine';
    osc.frequency.value = freq;
    gain.gain.setValueAtTime(0.3, ctx.currentTime);
    gain.gain.exponentialRampToValueAtTime(0.01, ctx.currentTime + duration / 1000);
    osc.connect(gain);
    gain.connect(ctx.destination);
    osc.start();
    osc.stop(ctx.currentTime + duration / 1000);
  } catch {}
}

function requestNotify() {
  if ('Notification' in window && Notification.permission === 'default') {
    Notification.requestPermission();
  }
}

function notifyBrowser(title, body) {
  if ('Notification' in window && Notification.permission === 'granted') {
    new Notification(title, { body, icon: '/favicon.ico' });
  }
}

function playDownAlert() { playBeep(440, 400); playBeep(330, 400); }

async function api(method, path, body = null) {
  const headers = { 'X-Telegram-InitData': initData };
  if (body && !(body instanceof FormData)) { headers['Content-Type'] = 'application/json'; body = JSON.stringify(body); }
  const res = await fetch(API_BASE + path, { method, headers, body });
  if (!res.ok) {
    let err = 'Request failed';
    try { const j = await res.json(); err = j.error || err; } catch {}
    throw new Error(err);
  }
  const ct = res.headers.get('content-type') || '';
  if (ct.includes('image')) return res;
  if (ct.includes('csv')) return res.blob();
  return res.json();
}

const router = {
  stack: [],
  go(page, data) {
    this.stack.push({ page, data });
    this._render(page, data);
    this._updateTabs(page);
    document.getElementById('back-btn').style.display = this.stack.length > 1 ? 'flex' : 'none';
    document.getElementById('context-menu').style.display = 'none';
  },
  back() {
    if (this.stack.length <= 1) return;
    this.stack.pop();
    const prev = this.stack[this.stack.length - 1];
    this._render(prev.page, prev.data);
    this._updateTabs(prev.page);
    document.getElementById('back-btn').style.display = this.stack.length > 1 ? 'flex' : 'none';
  },
  replace(page, data) {
    this.stack = [{ page, data }];
    this._render(page, data);
    this._updateTabs(page);
    document.getElementById('back-btn').style.display = 'none';
  },
  _render(page, data) {
    const content = document.getElementById('page-content');
    content.classList.remove('fade-enter');
    void content.offsetWidth;
    const title = document.getElementById('page-title');
    if (page === 'dashboard') { title.textContent = 'Dashboard'; renderDashboard(content); }
    else if (page === 'monitors') { title.textContent = 'Monitors'; renderMonitorsList(content); }
    else if (page === 'monitor') { title.textContent = 'Monitor'; renderMonitorDetail(content, data); }
    else if (page === 'edit') { title.textContent = 'Edit'; renderMonitorEdit(content, data); }
    else if (page === 'add') { title.textContent = 'Add Monitor'; renderMonitorAdd(content); }
    else if (page === 'incidents') { title.textContent = 'Timeline'; renderIncidents(content); }
    else if (page === 'settings') { title.textContent = 'Settings'; renderSettings(content); }
    else if (page === 'export') { title.textContent = 'Export'; renderExport(content); }
    else if (page === 'achievements') { title.textContent = 'Achievements'; renderAchievements(content); }
    content.classList.add('fade-enter');
  },
  _updateTabs(page) {
    const tabs = ['dashboard','monitors','incidents','export'];
    document.querySelectorAll('.tab-btn').forEach(b => b.classList.toggle('active', b.dataset.page === (tabs.includes(page) ? page : 'monitors')));
  }
};

if (window.Telegram && window.Telegram.WebApp) {
  const tg = window.Telegram.WebApp;
  tg.ready(); tg.expand();
  initData = tg.initData || '';
  tg.BackButton.onClick(() => router.back());
  tg.onEvent('themeChanged', () => {});
} else {
  initData = new URLSearchParams(window.location.search).get('tgWebAppData') || '';
}

const accentColor = localStorage.getItem('ellis_accent') || 'purple';
document.documentElement.setAttribute('data-accent', accentColor);

requestNotify();
initApp();

async function initApp() {
  if (!initData) {
    document.getElementById('loading-screen').style.display = 'none';
    document.getElementById('error-screen').style.display = 'flex';
    document.getElementById('error-message').textContent = 'No Telegram auth data';
    return;
  }
  try {
    userData = await api('GET', '/user');
    document.getElementById('loading-screen').style.display = 'none';
    document.getElementById('main-screen').style.display = 'flex';
    router.replace('dashboard');
  } catch (e) {
    document.getElementById('loading-screen').style.display = 'none';
    document.getElementById('error-screen').style.display = 'flex';
    document.getElementById('error-message').textContent = e.message;
  }
}

function esc(s) { const d = document.createElement('div'); d.textContent = s || ''; return d.innerHTML; }

function tagHtml(type) {
  return `<span class="tag">${esc(type)}</span>`;
}

function statusDot(isUp, pulse = false) {
  const cls = isUp === true ? 'status-up' : isUp === false ? 'status-down' : 'status-unknown';
  return `<span class="status-dot ${cls}${pulse && isUp === true ? ' pulse' : ''}"></span>`;
}

function avatarEmoji(config) {
  const emojis = { http:'🌐', keyword:'🔍', ping:'📡', port:'🔌', heartbeat:'💓', dns:'🌍', api:'⚙️', udp:'📦' };
  return config && config.icon ? config.icon : (emojis[config && config._type] || '📡');
}

function makeSparkline(checks) {
  if (!checks || checks.length < 3) return '';
  const vals = checks.slice(0, 30).reverse();
  const max = Math.max(...vals.map(c => c.response_time_ms || 0), 1);
  let html = '<span class="sparkline">';
  vals.forEach(c => {
    const h = Math.max(2, (c.response_time_ms || 0) / max * 16);
    html += `<span class="${c.is_up ? 'up' : 'down'}" style="height:${Math.round(h)}px"></span>`;
  });
  return html + '</span>';
}

function computeTrust(monitor) {
  if (monitor.is_up === false) return 0;
  const pct = (monitor.stats_24h && monitor.stats_24h.uptime) || 100;
  if (pct >= 99.9) return 4;
  if (pct >= 99) return 3;
  if (pct >= 95) return 2;
  return 1;
}

function trustClass(level) {
  return ['','trust-cold','trust-cool','trust-warm','trust-hot'][level] || '';
}

function computeAchievements(monitors, stats) {
  const a = [];
  const allUp = monitors.every(m => m.is_up === true && !m.is_paused);
  if (allUp && monitors.length > 0) a.push({ id:'iron', label:'🛡️ Iron Guardian', unlocked:true, desc:'All monitors UP' });
  if (monitors.length === 0) a.push({ id:'iron', label:'🛡️ Iron Guardian', unlocked:false, desc:'All monitors UP' });
  const types = new Set(monitors.map(m => m.type));
  if (types.size >= 8) a.push({ id:'collector', label:'🏆 Collector', unlocked:true, desc:'All 8 types used' });
  else a.push({ id:'collector', label:'🏆 Collector', unlocked:false, desc:`${types.size}/8 types` });
  const allPaused = monitors.length > 0 && monitors.every(m => m.is_paused);
  if (allPaused) a.push({ id:'hermit', label:'🧘 Hermit', unlocked:true, desc:'All monitors paused' });
  else a.push({ id:'hermit', label:'🧘 Hermit', unlocked:false, desc:'Pause all monitors' });
  if (stats.total >= 10) a.push({ id:'watcher', label:'👁️ Watcher', unlocked:true, desc:'10+ monitors' });
  else a.push({ id:'watcher', label:'👁️ Watcher', unlocked:false, desc:'Create 10 monitors' });
  if (monitors.some(m => m.type === 'heartbeat')) a.push({ id:'alive', label:'💓 Heartbeat', unlocked:true, desc:'Heartbeat monitor active' });
  else a.push({ id:'alive', label:'💓 Heartbeat', unlocked:false, desc:'Add a Heartbeat monitor' });
  return a;
}

// ========== DASHBOARD ==========
async function renderDashboard(container) {
  container.innerHTML = '<div style="text-align:center;padding:40px"><div class="spinner"></div></div>';
  try {
    const [stats, monitors] = await Promise.all([api('GET', '/stats'), api('GET', '/monitors')]);
    monitorsCache = monitors;
    await Promise.allSettled(monitors.map(m =>
      api('GET', `/monitors/${m.id}/checks?limit=50`).then(checks => {
        m._sparkline = makeSparkline(checks);
      }).catch(() => {})
    ));
    const downCount = monitors.filter(m => m.is_up === false && !m.is_paused).length;
    let html = '<div class="stat-grid" style="display:grid;grid-template-columns:1fr 1fr 1fr;gap:8px;margin-bottom:10px">';
    html += `<div class="stat-card"><div class="stat-value">${stats.total}</div><div class="stat-label">Total</div></div>`;
    html += `<div class="stat-card"><div class="stat-value">${stats.active}</div><div class="stat-label">Active</div></div>`;
    html += `<div class="stat-card"><div class="stat-value" style="-webkit-text-fill-color:${downCount > 0 ? 'var(--danger)' : 'var(--success)'}">${downCount}</div><div class="stat-label">Down</div></div>`;
    html += '</div>';

    html += '<div class="card" style="text-align:center;padding:20px 16px">';
    const circ = 2 * Math.PI * 42;
    const offset = circ - (stats.avg_uptime_24h / 100) * circ;
    html += `<div class="uptime-ring"><svg width="100" height="100" viewBox="0 0 100 100">
      <circle class="bg" cx="50" cy="50" r="42"/>
      <circle class="fg" cx="50" cy="50" r="42" stroke-dasharray="${circ}" stroke-dashoffset="${offset}"/>
    </svg><div class="center"><div class="pct">${stats.avg_uptime_24h}%</div><div class="lbl">Uptime 24h</div></div></div>`;
    html += '</div>';

    html += '<div class="chips" id="dash-filter">';
    html += '<button class="chip active" data-filter="all" onclick="filterDash(\'all\')">All</button>';
    html += '<button class="chip" data-filter="up" onclick="filterDash(\'up\')">🟢 UP</button>';
    html += '<button class="chip" data-filter="down" onclick="filterDash(\'down\')">🔴 DOWN</button>';
    html += '<button class="chip" data-filter="paused" onclick="filterDash(\'paused\')">⏸ Paused</button>';
    html += '</div>';

    const ach = computeAchievements(monitors, stats);
    html += '<div style="display:flex;flex-wrap:wrap;gap:4px;margin-bottom:10px">';
    ach.filter(a => a.unlocked).slice(0, 3).forEach(a => { html += `<span class="achievement unlocked">${a.label}</span>`; });
    if (ach.filter(a => a.unlocked).length > 3) html += `<span class="achievement unlocked">+${ach.filter(a=>a.unlocked).length - 3} more</span>`;
    html += '</div>';

    html += '<button class="btn btn-block btn-glow" onclick="router.go(\'add\')" style="margin-bottom:12px">+ Add Monitor</button>';

    html += '<div id="dash-mon-list">';
    if (!monitors.length) { html += '<div class="empty-state"><div class="icon">📡</div><p>No monitors yet</p></div>'; }
    else { html += renderMonitorItems(monitors, true); }
    html += '</div>';
    container.innerHTML = html;
  } catch (e) { container.innerHTML = `<div class="empty-state"><p>Error: ${esc(e.message)}</p></div>`; }
}

function renderMonitorItems(monitors, withSparkline = false) {
  let html = '';
  monitors.forEach((m, idx) => {
    const trust = computeTrust(m);
    html += `<div class="monitor-item${m.is_paused ? ' paused' : ''}" data-monitor-id="${m.id}" data-is-up="${m.is_up}" data-paused="${m.is_paused}" draggable="true"
      ondragstart="dragStart(event,${idx})" ondragover="dragOver(event)" ondrop="dropItem(event,${idx})" ondragend="dragEnd(event)"
      ontouchstart="touchStart(event,${m.id},'${esc(m.name||m.id)}')" onclick="router.go('monitor',${m.id})">
      <div class="drag-handle" onmousedown="event.stopPropagation()">⠿</div>
      ${statusDot(m.is_up, true)}
      <div class="monitor-info">
        <div class="monitor-name">${trustClass(trust) ? `<span class="${trustClass(trust)}" style="display:inline-block;width:8px;height:8px;border-radius:50%;margin-right:4px"></span>` : ''}${esc(m.name||m.id)} ${tagHtml(m.type)}</div>
        <div class="monitor-meta">
          <span>${m.is_up === true ? 'UP' : m.is_up === false ? 'DOWN' : 'Unknown'}</span>
          ${withSparkline && m._sparkline ? m._sparkline : ''}
        </div>
      </div>
      <div class="monitor-actions">
        <button class="btn-icon" onclick="event.stopPropagation();quickPause(${m.id},${!m.is_paused})">${m.is_paused ? '▶️' : '⏸'}</button>
        <button class="btn-icon" onclick="event.stopPropagation();quickCheck(${m.id})">🔄</button>
      </div>
    </div>`;
  });
  return html;
}

function filterDash(filter) {
  document.querySelectorAll('#dash-filter .chip').forEach(c => c.classList.toggle('active', c.dataset.filter === filter));
  const items = document.querySelectorAll('#dash-mon-list .monitor-item');
  items.forEach(item => {
    const isUp = item.dataset.isUp === 'true';
    const paused = item.dataset.paused === 'true';
    let show = true;
    if (filter === 'up') show = isUp && !paused;
    else if (filter === 'down') show = !isUp && !paused;
    else if (filter === 'paused') show = paused;
    item.style.display = show ? 'flex' : 'none';
  });
}

function quickPause(id, paused) {
  api('PATCH', `/monitors/${id}`, { is_paused: paused }).then(() => {
    Toast.show(paused ? '⏸ Paused' : '▶️ Resumed', 'success');
    router.go('dashboard');
  }).catch(e => Toast.show(e.message, 'error'));
}

function quickCheck(id) {
  api('GET', `/monitors/${id}`).then(() => {
    Toast.show('🔄 Check triggered', 'success');
    playBeep(660, 120);
  }).catch(e => Toast.show(e.message, 'error'));
}

// ========== DRAG & DROP ==========
let dragIdx = -1;
function dragStart(e, idx) { dragIdx = idx; e.dataTransfer.effectAllowed = 'move'; }
function dragOver(e) { e.preventDefault(); e.dataTransfer.dropEffect = 'move'; }
function dropItem(e, idx) {
  e.preventDefault();
  const items = [...document.querySelectorAll('#dash-mon-list .monitor-item')];
  if (dragIdx < 0 || dragIdx === idx) return;
  const monitors = monitorsCache;
  const [moved] = monitors.splice(dragIdx, 1);
  monitors.splice(idx, 0, moved);
  monitorsCache = monitors;
  const order = monitors.map((m, i) => ({ id: m.id, sort_order: i }));
  api('POST', '/monitors/reorder', { order }).then(() => {
    Toast.show('Order saved', 'success');
    router.go('dashboard');
  }).catch(e => Toast.show(e.message, 'error'));
}
function dragEnd(e) { dragIdx = -1; }

// ========== LONG PRESS ==========
let longPressTimer = null;
function touchStart(e, id, name) {
  longPressTimer = setTimeout(() => {
    e.preventDefault();
    showContextMenu(e, id, name);
  }, 500);
  const upHandler = () => { clearTimeout(longPressTimer); document.removeEventListener('touchend', upHandler); };
  document.addEventListener('touchend', upHandler, { once: true });
}

function showContextMenu(e, id, name) {
  const menu = document.getElementById('context-menu');
  menu.style.display = 'block';
  menu.innerHTML = `
    <button onclick="router.go('monitor',${id});menuClose()">🔍 View</button>
    <button onclick="quickPause(${id},true);menuClose()">⏸ Pause</button>
    <button onclick="quickCheck(${id});menuClose()">🔄 Check</button>
    <button class="danger" onclick="deleteMonitor(${id});menuClose()">🗑 Delete</button>
  `;
  const rect = e.touches ? { clientX: e.touches[0].clientX, clientY: e.touches[0].clientY } : { clientX: e.clientX, clientY: e.clientY };
  menu.style.left = Math.min(rect.clientX, window.innerWidth - 180) + 'px';
  menu.style.top = Math.min(rect.clientY, window.innerHeight - 200) + 'px';
}
function menuClose() { document.getElementById('context-menu').style.display = 'none'; }
document.addEventListener('click', (e) => { if (!e.target.closest('.context-menu')) menuClose(); });

// ========== MONITORS LIST ==========
async function renderMonitorsList(container) {
  container.innerHTML = '<div style="text-align:center;padding:40px"><div class="spinner"></div></div>';
  try {
    const monitors = await api('GET', '/monitors');
    monitorsCache = monitors;
    let html = '<button class="btn btn-block btn-glow" onclick="router.go(\'add\')" style="margin-bottom:12px">+ Add Monitor</button>';
    if (!monitors.length) { html += '<div class="empty-state"><div class="icon">📡</div><p>No monitors</p></div>'; }
    else {
      monitors.forEach(m => {
        html += `<div class="monitor-item${m.is_paused ? ' paused' : ''}" onclick="router.go('monitor',${m.id})">
          ${statusDot(m.is_up, true)}
          <div class="monitor-info">
            <div class="monitor-name">${esc(m.name||m.id)} ${tagHtml(m.type)}</div>
            <div class="monitor-meta">${m.is_up === true ? 'UP' : m.is_up === false ? 'DOWN' : 'Unknown'} · ${m.last_checked ? new Date(m.last_checked).toLocaleString() : 'never'}</div>
          </div>
        </div>`;
      });
    }
    container.innerHTML = html;
  } catch (e) { container.innerHTML = `<div class="empty-state"><p>Error: ${esc(e.message)}</p></div>`; }
}

// ========== MONITOR DETAIL ==========
async function renderMonitorDetail(container, id) {
  container.innerHTML = '<div style="text-align:center;padding:40px"><div class="spinner"></div></div>';
  try {
    const mon = await api('GET', `/monitors/${id}`);
    const cfg = mon.config || {};
    const cfgLabel = cfg.url || cfg.host || cfg.domain || (cfg.path ? '/' + cfg.path : '') || '-';
    const icon = cfg.icon || '';
    
    let html = `<div class="card" style="text-align:center;padding:20px">
      <div style="font-size:36px;margin-bottom:4px">${icon || avatarEmoji({...cfg, _type: mon.type})}</div>
      <h2 style="font-size:20px;font-weight:700;margin-bottom:2px">${statusDot(mon.is_up, true)} ${esc(mon.name||id)}</h2>
      <div class="card-text">${tagHtml(mon.type)} ${esc(cfgLabel)}</div>
    </div>`;
    html += '<div class="stat-grid" style="display:grid;grid-template-columns:repeat(3,1fr);gap:8px">';
    html += `<div class="stat-card"><div class="stat-value" style="-webkit-text-fill-color:${mon.is_up ? 'var(--success)' : 'var(--danger)'}">${mon.is_up === true ? 'UP' : mon.is_up === false ? 'DOWN' : '?'}</div><div class="stat-label">Status</div></div>`;
    html += `<div class="stat-card"><div class="stat-value">${mon.interval_seconds}s</div><div class="stat-label">Interval</div></div>`;
    html += `<div class="stat-card"><div class="stat-value">${mon.consecutive_failures||0}</div><div class="stat-label">Failures</div></div>`;
    html += '</div>';

    html += '<div class="action-bar" style="display:flex;gap:6px;flex-wrap:wrap;margin:12px 0">';
    html += `<button class="btn btn-sm ${mon.is_paused ? 'btn-outline' : ''}" onclick="detailPause(${id},${!mon.is_paused})">${mon.is_paused ? '▶️ Resume' : '⏸ Pause'}</button>`;
    html += `<button class="btn btn-sm btn-outline" onclick="detailCheck(${id})">🔄 Check</button>`;
    html += `<button class="btn btn-sm btn-outline" onclick="router.go('edit',${id})">✏️ Edit</button>`;
    html += `<button class="btn btn-sm btn-glow" onclick="pickAvatar(${id}, '${esc(JSON.stringify(cfg).replace(/'/g,"\\'"))}')">🎨 Avatar</button>`;
    html += `<button class="btn btn-sm btn-danger" onclick="deleteMonitor(${id})">🗑 Delete</button>`;
    html += '</div>';

    html += '<div class="card"><div class="card-title">📊 Response Time</div>';
    html += '<div class="filter-bar" style="display:flex;gap:6px;margin:8px 0">';
    html += `<select id="graph-period" onchange="loadChart(${id})" style="flex:1">
      <option value="24">24 hours</option><option value="168">7 days</option><option value="720">30 days</option>
    </select>
    <select id="graph-style" onchange="loadChart(${id})" style="flex:1">
      <option value="line">Line</option><option value="bar">Bar</option><option value="pie">Pie</option>
    </select></div>
    <div class="graph-container"><canvas id="chart-canvas"></canvas></div></div>`;

    html += '<div class="card"><div class="card-title">📋 Recent Checks</div><div class="table-wrap">';
    html += '<table><thead><tr><th>Time</th><th>Status</th><th>Response</th></tr></thead><tbody>';
    const checks = mon.recent_checks || [];
    checks.forEach(c => {
      html += `<tr><td>${new Date(c.checked_at).toLocaleString()}</td>
        <td>${statusDot(c.is_up)} ${c.is_up ? 'UP' : 'DOWN'}</td>
        <td>${c.response_time_ms != null ? Math.round(c.response_time_ms) + 'ms' : '-'}</td></tr>`;
    });
    html += '</tbody></table></div></div>';
    container.innerHTML = html;
    
    setTimeout(() => loadChart(id), 100);
    fetchSparkles(id);
  } catch (e) { container.innerHTML = `<div class="empty-state"><p>Error: ${esc(e.message)}</p></div>`; }
}

async function fetchSparkles(id) {
  try {
    const checks = await api('GET', `/monitors/${id}/checks?limit=50`);
    const mon = monitorsCache.find(m => m.id === id);
    if (mon) mon._sparkline = makeSparkline(checks);
  } catch {}
}

async function loadChart(id) {
  const period = document.getElementById('graph-period');
  const style = document.getElementById('graph-style');
  if (!period || !style) return;
  const hours = parseInt(period.value);
  const chartStyle = style.value;
  try {
    const checks = await api('GET', `/monitors/${id}/checks?limit=200`);
    const data = checks.reverse();
    if (!data.length) return;
    const labels = data.map(c => new Date(c.checked_at).toLocaleTimeString());
    const times = data.map(c => c.response_time_ms || 0);
    const upCount = data.filter(c => c.is_up).length;
    const canvas = document.getElementById('chart-canvas');
    if (!canvas) return;
    if (chartInstance) chartInstance.destroy();
    const ctx = canvas.getContext('2d');
    const isDark = window.matchMedia('(prefers-color-scheme: dark)').matches;
    const textColor = isDark ? '#e4e6f0' : '#1a1a2e';
    const gridColor = isDark ? 'rgba(255,255,255,0.06)' : 'rgba(0,0,0,0.06)';
    if (chartStyle === 'pie') {
      chartInstance = new Chart(ctx, {
        type: 'doughnut',
        data: { labels: ['UP', 'DOWN'], datasets: [{ data: [upCount, data.length - upCount], backgroundColor: ['#2ecc71', '#ff4757'], borderWidth: 0 }] },
        options: { responsive: true, maintainAspectRatio: false, plugins: { legend: { labels: { color: textColor } } } }
      });
    } else {
      const gradient = ctx.createLinearGradient(0, 0, 0, 200);
      gradient.addColorStop(0, 'rgba(102,126,234,0.3)');
      gradient.addColorStop(1, 'rgba(102,126,234,0)');
      chartInstance = new Chart(ctx, {
        type: chartStyle === 'bar' ? 'bar' : 'line',
        data: {
          labels,
          datasets: [{
            label: 'Response time (ms)',
            data: times,
            borderColor: '#667eea',
            backgroundColor: chartStyle === 'bar' ? 'rgba(102,126,234,0.6)' : gradient,
            fill: chartStyle === 'line',
            tension: 0.3,
            pointRadius: 1,
            pointHitRadius: 10,
            borderWidth: 2
          }]
        },
        options: {
          responsive: true, maintainAspectRatio: false,
          plugins: { legend: { display: false } },
          scales: {
            x: { ticks: { color: textColor, maxTicksLimit: 8 }, grid: { color: gridColor } },
            y: { beginAtZero: true, ticks: { color: textColor }, grid: { color: gridColor } }
          }
        }
      });
    }
  } catch {}
}

async function detailPause(id, paused) {
  try { await api('PATCH', `/monitors/${id}`, { is_paused: paused }); Toast.show(paused ? '⏸ Paused' : '▶️ Resumed', 'success'); router.go('monitor', id); }
  catch (e) { Toast.show(e.message, 'error'); }
}
async function detailCheck(id) {
  try { await api('GET', `/monitors/${id}`); Toast.show('🔄 Check triggered', 'success'); playBeep(660, 120); }
  catch (e) { Toast.show(e.message, 'error'); }
}
async function deleteMonitor(id) {
  const menu = document.getElementById('context-menu');
  menu.style.display = 'none';
  if (!confirm('Delete this monitor?')) return;
  try { await api('DELETE', `/monitors/${id}`); Toast.show('🗑 Deleted', 'success'); router.replace('monitors'); }
  catch (e) { Toast.show(e.message, 'error'); }
}

// ========== AVATAR PICKER ==========
function pickAvatar(id, configJson) {
  const config = JSON.parse(configJson);
  const emojis = ['🌐','🔍','📡','🔌','💓','🌍','⚙️','📦','🗄️','📧','🖥️','🔒','☁️','📊','🎯','💎'];
  const menu = document.getElementById('context-menu');
  menu.style.display = 'block';
  menu.innerHTML = `<div style="padding:8px"><strong style="font-size:13px">Choose icon</strong><div class="emoji-picker">${emojis.map(e =>
    `<button class="${config.icon === e ? 'selected' : ''}" onclick="setAvatar(${id},'${e}')">${e}</button>`
  ).join('')}</div></div>`;
  menu.style.left = '50%';
  menu.style.top = '40%';
  menu.style.transform = 'translate(-50%,-50%)';
  menu.style.minWidth = '260px';
}

async function setAvatar(id, emoji) {
  try {
    const mon = await api('GET', `/monitors/${id}`);
    const config = mon.config || {};
    config.icon = emoji;
    await api('PATCH', `/monitors/${id}`, { config });
    menuClose();
    Toast.show('Avatar updated', 'success');
    router.go('monitor', id);
  } catch (e) { Toast.show(e.message, 'error'); }
}

// ========== EDIT MONITOR ==========
async function renderMonitorEdit(container, id) {
  container.innerHTML = '<div style="text-align:center;padding:40px"><div class="spinner"></div></div>';
  try {
    const mon = await api('GET', `/monitors/${id}`);
    const cfg = mon.config || {};
    container.innerHTML = `<form id="edit-form" onsubmit="submitEdit(${id},event)">
      <div class="form-group"><label class="form-label">Name</label><input name="name" value="${esc(mon.name||'')}" required/></div>
      <div class="form-group"><label class="form-label">Interval (seconds, min 30)</label><input name="interval_seconds" type="number" value="${mon.interval_seconds}" min="30" required/></div>
      <div class="form-group"><label class="form-label">Config (JSON)</label><textarea name="config" rows="5">${esc(JSON.stringify(cfg,null,2))}</textarea></div>
      <button class="btn btn-block" type="submit">Save</button>
      <button class="btn btn-block btn-outline" type="button" onclick="router.back()" style="margin-top:6px">Cancel</button>
    </form>`;
  } catch (e) { container.innerHTML = `<div class="empty-state"><p>Error: ${esc(e.message)}</p></div>`; }
}
async function submitEdit(id, e) {
  e.preventDefault();
  const fd = new FormData(e.target);
  const body = { name: fd.get('name'), interval_seconds: parseInt(fd.get('interval_seconds')) };
  try { body.config = JSON.parse(fd.get('config')); }
  catch { Toast.show('Invalid JSON config', 'error'); return; }
  try { await api('PATCH', `/monitors/${id}`, body); Toast.show('✅ Saved', 'success'); router.go('monitor', id); }
  catch (e) { Toast.show(e.message, 'error'); }
}

// ========== ADD MONITOR ==========
function renderMonitorAdd(container) {
  container.innerHTML = `<form id="add-form" onsubmit="submitAdd(event)">
    <div class="form-group"><label class="form-label">Type</label>
    <select name="type" required onchange="toggleAddFields()">
      <option value="">Select type…</option>
      <option value="http">🌐 HTTP(s)</option><option value="keyword">🔍 Keyword</option><option value="ping">📡 Ping</option>
      <option value="port">🔌 Port</option><option value="heartbeat">💓 Heartbeat</option><option value="dns">🌍 DNS</option>
      <option value="api">⚙️ API (JSONPath)</option><option value="udp">📦 UDP</option>
    </select></div>
    <div class="form-group"><label class="form-label">Name</label><input name="name" placeholder="My Monitor"/></div>
    <div id="af-http" class="add-fields" style="display:none">
      <div class="form-group"><label class="form-label">URL</label><input name="url" placeholder="https://example.com"/></div>
      <div class="form-group"><label class="form-label">Expected Status</label><input name="expected_status" type="number" value="200"/></div>
    </div>
    <div id="af-keyword" class="add-fields" style="display:none">
      <div class="form-group"><label class="form-label">URL</label><input name="url"/></div>
      <div class="form-group"><label class="form-label">Keyword</label><input name="keyword"/></div>
      <div class="form-group"><label class="form-label">Mode</label><select name="mode"><option value="present">Present</option><option value="absent">Absent</option></select></div>
    </div>
    <div id="af-ping" class="add-fields" style="display:none"><div class="form-group"><label class="form-label">Host</label><input name="host" placeholder="google.com"/></div></div>
    <div id="af-port" class="add-fields" style="display:none">
      <div class="form-group"><label class="form-label">Host</label><input name="host"/></div>
      <div class="form-group"><label class="form-label">Port</label><input name="port" type="number" placeholder="80"/></div>
    </div>
    <div id="af-heartbeat" class="add-fields" style="display:none">
      <div class="form-group"><label class="form-label">Path (leave empty for auto)</label><input name="path"/></div>
      <div class="form-group"><label class="form-label">Max Interval (s)</label><input name="max_interval" type="number" value="600"/></div>
    </div>
    <div id="af-dns" class="add-fields" style="display:none">
      <div class="form-group"><label class="form-label">Domain</label><input name="domain" placeholder="example.com"/></div>
      <div class="form-group"><label class="form-label">Record Type</label><input name="record_type" value="A"/></div>
      <div class="form-group"><label class="form-label">Expected Value (optional)</label><input name="expected_value"/></div>
    </div>
    <div id="af-api" class="add-fields" style="display:none">
      <div class="form-group"><label class="form-label">URL</label><input name="url"/></div>
      <div class="form-group"><label class="form-label">JSONPath</label><input name="jsonpath" value="$.status"/></div>
      <div class="form-group"><label class="form-label">Expected Value (optional)</label><input name="expected_value"/></div>
    </div>
    <div id="af-udp" class="add-fields" style="display:none">
      <div class="form-group"><label class="form-label">Host</label><input name="host"/></div>
      <div class="form-group"><label class="form-label">Port</label><input name="port" type="number"/></div>
      <div class="form-group"><label class="form-label">Send Data</label><input name="send_data"/></div>
      <div class="form-group"><label class="form-label">Expected Response</label><input name="expected_response"/></div>
    </div>
    <div class="form-group"><label class="form-label">Interval (seconds, min 30)</label><input name="interval_seconds" type="number" value="300" min="30"/></div>
    <button class="btn btn-block" type="submit">Create Monitor</button>
    <button class="btn btn-block btn-outline" type="button" onclick="router.back()" style="margin-top:6px">Cancel</button>
  </form>`;
}
function toggleAddFields() {
  const type = document.querySelector('[name=type]').value;
  document.querySelectorAll('.add-fields').forEach(el => el.style.display = 'none');
  const el = document.getElementById('af-' + type);
  if (el) el.style.display = 'block';
}
async function submitAdd(e) {
  e.preventDefault();
  const fd = new FormData(e.target);
  const type = fd.get('type');
  if (!type) { Toast.show('Select a type', 'error'); return; }
  let config = {};
  const interval = parseInt(fd.get('interval_seconds')) || 300;
  try {
    if (type === 'http') config = { url: fd.get('url'), expected_status: parseInt(fd.get('expected_status')) || 200 };
    else if (type === 'keyword') config = { url: fd.get('url'), keyword: fd.get('keyword'), mode: fd.get('mode')||'present' };
    else if (type === 'ping') config = { host: fd.get('host') };
    else if (type === 'port') config = { host: fd.get('host'), port: parseInt(fd.get('port')) };
    else if (type === 'heartbeat') config = { path: fd.get('path')||'', max_interval: parseInt(fd.get('max_interval'))||600 };
    else if (type === 'dns') config = { domain: fd.get('domain'), record_type: fd.get('record_type')||'A', expected_value: fd.get('expected_value')||null };
    else if (type === 'api') config = { url: fd.get('url'), jsonpath: fd.get('jsonpath')||'$.status', expected_value: fd.get('expected_value')||null };
    else if (type === 'udp') config = { host: fd.get('host'), port: parseInt(fd.get('port')), send_data: fd.get('send_data')||'', expected_response: fd.get('expected_response')||null };
  } catch { Toast.show('Invalid config', 'error'); return; }
  if ((type === 'http' || type === 'keyword' || type === 'api') && !config.url) { Toast.show('URL required', 'error'); return; }
  if ((type === 'ping' || type === 'port' || type === 'udp') && !config.host) { Toast.show('Host required', 'error'); return; }
  try {
    const res = await api('POST', '/monitors', { type, name: fd.get('name')||null, config, interval_seconds: interval });
    Toast.show('✅ Monitor created', 'success');
    playBeep(880, 150);
    router.replace('monitor', res.id);
  } catch (e) { Toast.show(e.message, 'error'); }
}

// ========== INCIDENTS ==========
async function renderIncidents(container) {
  container.innerHTML = '<div style="text-align:center;padding:40px"><div class="spinner"></div></div>';
  try {
    const incidents = await api('GET', '/incidents');
    if (!incidents.length) { container.innerHTML = '<div class="empty-state"><div class="icon">📋</div><p>No incidents recorded</p></div>'; return; }
    const byMonth = {};
    incidents.forEach(inc => {
      const d = new Date(inc.to_time);
      const key = d.toLocaleString('en', { month:'long', year:'numeric' });
      if (!byMonth[key]) byMonth[key] = [];
      byMonth[key].push(inc);
    });
    let html = '';
    for (const [month, items] of Object.entries(byMonth)) {
      html += `<div class="card"><div class="card-title">${month}</div>`;
      items.forEach(inc => {
        const fromIcon = inc.from_status ? '🟢' : '🔴';
        const toIcon = inc.to_status ? '🟢' : '🔴';
        const fromLabel = inc.from_status ? 'UP' : 'DOWN';
        const toLabel = inc.to_status ? 'UP' : 'DOWN';
        const isRecovery = inc.to_status;
        html += `<div class="incident-item" style="border-left:3px solid ${isRecovery ? 'var(--success)' : 'var(--danger)'};padding-left:12px;margin-bottom:4px">
          <strong>${esc(inc.monitor_name||'Monitor')}</strong> ${tagHtml(inc.monitor_type)}<br>
          <span>${fromIcon} ${fromLabel}</span> → <span>${toIcon} ${toLabel}</span>
          <span class="incident-time">${new Date(inc.to_time).toLocaleString()}</span>
          ${inc.from_time ? `<span class="incident-time">Duration: ${msToHuman(new Date(inc.to_time) - new Date(inc.from_time))}</span>` : ''}
        </div>`;
      });
      html += '</div>';
    }
    container.innerHTML = html;
  } catch (e) { container.innerHTML = `<div class="empty-state"><p>Error: ${esc(e.message)}</p></div>`; }
}

function msToHuman(ms) {
  if (ms < 0) return '';
  const s = Math.floor(ms / 1000);
  if (s < 60) return s + 's';
  const m = Math.floor(s / 60);
  if (m < 60) return m + 'm ' + (s % 60) + 's';
  const h = Math.floor(m / 60);
  return h + 'h ' + (m % 60) + 'm';
}

// ========== SETTINGS ==========
async function renderSettings(container) {
  container.innerHTML = '<div style="text-align:center;padding:40px"><div class="spinner"></div></div>';
  try {
    const user = await api('GET', '/user');
    container.innerHTML = `
    <form id="settings-form" onsubmit="submitSettings(event)">
      <div class="card">
        <div class="card-title">👤 Account</div>
        <div class="card-text">ID: ${user.user_id}<br>Premium: ${user.is_premium ? '✅ Yes' : '❌ No'}<br>Limit: ${user.monitor_limit} monitors</div>
      </div>
      <div class="card">
        <div class="card-title">🎨 Theme Accent</div>
        <div class="chips" id="accent-picker">
          <button class="chip ${accentColor === 'purple' ? 'active' : ''}" onclick="setAccent('purple')">💜 Purple</button>
          <button class="chip ${accentColor === 'blue' ? 'active' : ''}" onclick="setAccent('blue')">💙 Blue</button>
          <button class="chip ${accentColor === 'green' ? 'active' : ''}" onclick="setAccent('green')">💚 Green</button>
          <button class="chip ${accentColor === 'orange' ? 'active' : ''}" onclick="setAccent('orange')">🧡 Orange</button>
        </div>
      </div>
      <div class="form-group"><label class="form-label">Email</label><input name="email" type="email" value="${esc(user.email||'')}"/>
        <button class="btn btn-sm btn-outline" type="button" onclick="testEmail()" style="margin-top:6px">📧 Test Email</button></div>
      <div class="form-group"><label class="form-label">Alert Repeat (minutes, 0=off)</label><input name="alert_repeat" type="number" value="${user.alert_repeat||0}" min="0"/></div>
      <div class="form-group"><label class="form-label">Maintenance Window</label>
        <div style="display:flex;gap:8px"><input name="maintenance_from" type="time" value="${user.maintenance_from||''}" style="flex:1"/>
        <input name="maintenance_to" type="time" value="${user.maintenance_to||''}" style="flex:1"/></div>
      </div>
      <button class="btn btn-block" type="submit">Save Settings</button>
    </form>
    <div class="card" style="margin-top:12px">
      <div class="card-title">🔗 Public Status Page</div>
      <div class="card-text" style="margin-bottom:8px">${user.public_token ? '🔗 ' + window.location.origin + '/api/v1/public/' + esc(user.public_token) : 'No public token'}</div>
      <button class="btn btn-sm" onclick="genToken()">${user.public_token ? '🔄 Regenerate' : '🔑 Generate'} Token</button>
      ${user.public_token ? `<button class="btn btn-sm btn-outline" style="margin-left:6px" onclick="copyUrl('${user.public_token}')">📋 Copy URL</button>` : ''}
    </div>
    <div class="card">
      <div class="card-title">🏆 Achievements</div>
      <button class="btn btn-sm btn-outline btn-block" onclick="router.go('achievements')">View All Achievements</button>
    </div>`;
  } catch (e) { container.innerHTML = `<div class="empty-state"><p>Error: ${esc(e.message)}</p></div>`; }
}

function setAccent(color) {
  localStorage.setItem('ellis_accent', color);
  document.documentElement.setAttribute('data-accent', color);
  document.querySelectorAll('#accent-picker .chip').forEach(c => c.classList.toggle('active', c.textContent.toLowerCase().includes(color)));
  Toast.show('🎨 Theme updated', 'success');
}

async function submitSettings(e) {
  e.preventDefault();
  const fd = new FormData(e.target);
  const body = {};
  if (fd.get('email')) body.email = fd.get('email');
  body.alert_repeat = parseInt(fd.get('alert_repeat')) || 0;
  body.maintenance_from = fd.get('maintenance_from') || '';
  body.maintenance_to = fd.get('maintenance_to') || '';
  try { await api('POST', '/settings', body); Toast.show('✅ Settings saved', 'success'); }
  catch (e) { Toast.show(e.message, 'error'); }
}

async function testEmail() {
  try { await api('POST', '/settings/test-email'); Toast.show('📧 Test email sent! Check your inbox', 'success'); }
  catch (e) { Toast.show(e.message, 'error'); }
}

async function genToken() {
  try { const res = await api('POST', '/public/token'); Toast.show('🔑 Token: ' + res.token, 'success'); router.go('settings'); }
  catch (e) { Toast.show(e.message, 'error'); }
}
function copyUrl(token) {
  navigator.clipboard.writeText(window.location.origin + '/api/v1/public/' + token)
    .then(() => Toast.show('📋 Copied to clipboard', 'success'));
}

// ========== ACHIEVEMENTS ==========
async function renderAchievements(container) {
  container.innerHTML = '<div style="text-align:center;padding:40px"><div class="spinner"></div></div>';
  try {
    const [stats, monitors] = await Promise.all([api('GET', '/stats'), api('GET', '/monitors')]);
    const ach = computeAchievements(monitors, stats);
    let html = '<div class="card"><div class="card-title">🏆 Achievements</div><div class="card-text" style="margin-bottom:10px">Unlocked: ' + ach.filter(a => a.unlocked).length + '/' + ach.length + '</div>';
    ach.forEach(a => {
      html += `<div class="achievement ${a.unlocked ? 'unlocked' : 'locked'}" style="display:flex;justify-content:space-between;align-items:center;padding:10px;margin:4px 0;border-radius:10px;background:var(--card-bg);border:1px solid var(--card-border)">
        <div><strong>${a.label}</strong><br><span style="font-size:12px;color:var(--hint)">${a.desc}</span></div>
        <div style="font-size:20px">${a.unlocked ? '✅' : '🔒'}</div>
      </div>`;
    });
    html += '</div>';
    container.innerHTML = html;
  } catch (e) { container.innerHTML = `<div class="empty-state"><p>Error: ${esc(e.message)}</p></div>`; }
}

// ========== EXPORT ==========
async function renderExport(container) {
  container.innerHTML = '<div style="text-align:center;padding:40px"><div class="spinner"></div></div>';
  try {
    const monitors = await api('GET', '/monitors');
    container.innerHTML = `<form onsubmit="doExport(event)">
      <div class="card"><div class="card-title">📥 Export Data</div></div>
      <div class="form-group"><label class="form-label">Monitor</label>
      <select name="monitor_id"><option value="">All monitors</option>
        ${monitors.map(m => `<option value="${m.id}">${esc(m.name||m.id)} [${m.type}]</option>`).join('')}
      </select></div>
      <div class="form-group"><label class="form-label">Period</label>
      <select name="hours"><option value="24">24 hours</option><option value="168">7 days</option><option value="720">30 days</option></select></div>
      <div class="form-group"><label class="form-label">Format</label>
      <select name="format"><option value="json">JSON</option><option value="csv">CSV</option></select></div>
      <button class="btn btn-block" type="submit">⬇ Download Export</button>
    </form>`;
  } catch (e) { container.innerHTML = `<div class="empty-state"><p>Error: ${esc(e.message)}</p></div>`; }
}
async function doExport(e) {
  e.preventDefault();
  const fd = new FormData(e.target);
  const mid = fd.get('monitor_id');
  const hours = fd.get('hours') || '24';
  const fmt = fd.get('format') || 'json';
  let url = `${API_BASE}/export?hours=${hours}&format=${fmt}`;
  if (mid) url += `&monitor_id=${mid}`;
  try {
    const res = await fetch(url, { headers: { 'X-Telegram-InitData': initData } });
    if (!res.ok) { const j = await res.json(); throw new Error(j.error); }
    const blob = await res.blob();
    const a = document.createElement('a');
    a.href = URL.createObjectURL(blob);
    a.download = `export.${fmt}`;
    a.click();
    URL.revokeObjectURL(a.href);
    Toast.show('⬇ Download started', 'success');
  } catch (e) { Toast.show(e.message, 'error'); }
}

// ========== POLLING ==========
setInterval(async () => {
  try {
    const stats = await api('GET', '/stats');
    const down = stats.active - stats.up;
    if (down > 0) {
      const prevDown = parseInt(sessionStorage.getItem('_prevDown') || '0');
      if (prevDown < down) { playDownAlert(); notifyBrowser('🔴 Monitor Down', `${down} monitor(s) are down`); }
      sessionStorage.setItem('_prevDown', String(down));
    } else {
      sessionStorage.setItem('_prevDown', '0');
    }
  } catch {}
}, 30000);

// Check for name changes (simulate initData detection)
document.addEventListener('visibilitychange', () => {
  if (!document.hidden && router.stack.length > 0) {
    const cur = router.stack[router.stack.length - 1];
    if (cur.page === 'dashboard') router.go('dashboard');
  }
});
