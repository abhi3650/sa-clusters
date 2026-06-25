// ════════════════════════════════════════════════════════════════
//  BotClusters — cluster.js  (v3)
// ════════════════════════════════════════════════════════════════
let socket;
let updateInterval;
let lastKnownProcesses = [];
let activeFilter = 'all';
let searchQuery   = '';
let currentLogProcess = null;
let logSSE = null;
let confirmCallback = null;
let deployTab = 'git';

// ── Socket setup ─────────────────────────────────────────────

document.addEventListener('DOMContentLoaded', () => {
  socket = io({ reconnection: true, reconnectionDelay: 1000,
                reconnectionDelayMax: 5000, reconnectionAttempts: 10 });

  socket.on('connect', () => {
    requestStatus();
    if (updateInterval) clearInterval(updateInterval);
    updateInterval = setInterval(requestStatus, 4000);
  });

  socket.on('disconnect', () => { if (updateInterval) clearInterval(updateInterval); });

  socket.on('status_update', (data) => {
    if (data.processes && data.processes.length > 0) {
      lastKnownProcesses = data.processes;
    }
    renderGrid(lastKnownProcesses);
  });

  loadCronSetting();
  checkCapabilities();

  // Close dropdowns on outside click
  document.addEventListener('click', (e) => {
    if (!e.target.closest('#tools-btn') && !e.target.closest('#tools-menu')) {
      document.getElementById('tools-menu').classList.remove('open');
    }
  });

  // Close modals on overlay click
  document.querySelectorAll('.modal-overlay').forEach(overlay => {
    overlay.addEventListener('click', (e) => {
      if (e.target === overlay) overlay.style.display = 'none';
    });
  });
});

function requestStatus() {
  if (socket && socket.connected) socket.emit('request_status');
  else if (socket) socket.connect();
}

document.addEventListener('visibilitychange', () => {
  if (!document.hidden) { requestStatus(); updateInterval = setInterval(requestStatus, 4000); }
  else clearInterval(updateInterval);
});

// ── Helpers ──────────────────────────────────────────────────

function getBotNumber(name) {
  const m = name.match(/bot(\d+)$/i);
  return m ? parseInt(m[1]) : null;
}

function formatBotName(name) {
  const n = getBotNumber(name);
  return n ? `Bot #${n}` : name;
}

function parseEnvText(text) {
  const env = {};
  (text || '').split('\n').forEach(line => {
    line = line.trim();
    if (!line || !line.includes('=')) return;
    const idx = line.indexOf('=');
    env[line.slice(0, idx).trim()] = line.slice(idx + 1).trim();
  });
  return env;
}

function showToast(msg, type = 'info', duration = 3500) {
  const t = document.getElementById('toast');
  t.textContent = msg;
  t.className = `toast toast-${type} show`;
  setTimeout(() => t.classList.remove('show'), duration);
}

function confirmAction(title, body, okLabel, danger, cb) {
  document.getElementById('confirm-title').textContent = title;
  document.getElementById('confirm-body').textContent  = body;
  const btn = document.getElementById('confirm-ok-btn');
  btn.textContent  = okLabel;
  btn.className    = danger ? 'hbtn hbtn-danger' : 'hbtn hbtn-primary';
  confirmCallback  = cb;
  document.getElementById('confirm-modal').style.display = 'flex';
}

function confirmOk() {
  closeConfirm();
  if (confirmCallback) confirmCallback();
}

function closeConfirm() {
  document.getElementById('confirm-modal').style.display = 'none';
  confirmCallback = null;
}

// ── Filter / search ───────────────────────────────────────────

function setFilter(f, el) {
  activeFilter = f;
  document.querySelectorAll('.ftab').forEach(b => b.classList.remove('active'));
  el.classList.add('active');
  renderGrid(lastKnownProcesses);
}

function filterBots(q) {
  searchQuery = q.toLowerCase();
  renderGrid(lastKnownProcesses);
}

function applyFilters(processes) {
  return processes.filter(p => {
    if (searchQuery && !p.name.toLowerCase().includes(searchQuery) &&
        !formatBotName(p.name).toLowerCase().includes(searchQuery)) return false;
    if (activeFilter === 'all') return true;
    if (activeFilter === 'RUNNING') return p.status === 'RUNNING';
    if (activeFilter === 'STOPPED') return p.status !== 'RUNNING' && !p.paused && !p.auto_paused;
    if (activeFilter === 'paused')  return p.paused || p.auto_paused;
    if (activeFilter === 'docker')  return p.is_docker;
    return true;
  });
}

// ── Grid renderer ─────────────────────────────────────────────

function renderGrid(processes) {
  const grid = document.getElementById('bot-grid');
  const empty = document.getElementById('empty-state');
  if (!grid) return;

  const sorted = [...processes].sort((a, b) => (getBotNumber(a.name)||0) - (getBotNumber(b.name)||0));
  const filtered = applyFilters(sorted);

  // Stats
  let online = 0, offline = 0, paused = 0;
  sorted.forEach(p => {
    if (p.status === 'RUNNING' && !p.paused && !p.auto_paused) online++;
    else if (p.paused || p.auto_paused) paused++;
    else offline++;
  });
  document.getElementById('stat-online').textContent  = online;
  document.getElementById('stat-offline').textContent = offline;
  document.getElementById('stat-paused').textContent  = paused;
  document.getElementById('stat-total').textContent   = sorted.length;
  const badge = document.getElementById('bot-count-badge');
  if (badge) badge.textContent = `${sorted.length} bot${sorted.length !== 1 ? 's' : ''}`;

  if (sorted.length === 0) {
    empty.style.display = 'flex';
    // Remove old cards but keep empty state
    Array.from(grid.children).forEach(c => { if (c !== empty) c.remove(); });
    return;
  }
  empty.style.display = 'none';

  // Build cards — update existing, add new, remove old
  const existingCards = {};
  grid.querySelectorAll('.bot-card').forEach(c => { existingCards[c.dataset.name] = c; });

  const rendered = new Set();
  filtered.forEach((p, idx) => {
    rendered.add(p.name);
    let card = existingCards[p.name];
    if (!card) {
      card = document.createElement('div');
      card.className = 'bot-card';
      card.dataset.name = p.name;
      grid.appendChild(card);
    }
    updateCard(card, p);
  });

  // Remove cards not in filtered set
  Object.entries(existingCards).forEach(([name, card]) => {
    if (!rendered.has(name)) card.remove();
  });
}

function updateCard(card, p) {
  const isRunning    = p.status === 'RUNNING';
  const isPaused     = p.paused;
  const isAutoPaused = p.auto_paused;
  const isDocker     = p.is_docker;
  const botNum       = getBotNumber(p.name);
  const hasWebUI     = isDocker && p.web_port && botNum && isRunning;

  let statusClass, statusLabel;
  if      (isAutoPaused)         { statusClass = 'status-fatal';   statusLabel = 'Failed'; }
  else if (isPaused)             { statusClass = 'status-paused';  statusLabel = 'Paused'; }
  else if (isRunning)            { statusClass = 'status-online';  statusLabel = 'Running'; }
  else if (p.status === 'STARTING') { statusClass = 'status-starting'; statusLabel = 'Starting'; }
  else                           { statusClass = 'status-offline'; statusLabel = p.status || 'Stopped'; }

  card.className = 'bot-card' + (isAutoPaused ? ' card-fatal' : '') +
                   (isPaused ? ' card-paused' : '') +
                   (isRunning && !isPaused ? ' card-running' : '');

  const dockerBadge = isDocker
    ? `<span class="tag tag-docker" title="Docker container">🐳</span>`
    : `<span class="tag tag-git" title="Git/Python">🐍</span>`;

  const webUIBtn = hasWebUI
    ? `<a href="/bot${botNum}" target="_blank" class="cbtn cbtn-webui" title="Open web UI">🌐</a>` : '';

  const progressBar = isRunning
    ? `<div class="uptime-bar"><div class="uptime-fill" style="width:100%"></div></div>` : '';

  let controls = '';
  if (isAutoPaused) {
    controls = `
      <button class="cbtn cbtn-start" onclick="clearFailure('${p.name}')">↺ Retry</button>
      <button class="cbtn cbtn-log"   onclick="openLogModal('${p.name}')">📋 Logs</button>
      <button class="cbtn cbtn-delete" onclick="deleteBot('${p.name}')">🗑</button>
    `;
  } else {
    controls = `
      <button class="cbtn ${isRunning ? 'cbtn-stop' : 'cbtn-start'}"
              onclick="toggleBot('${p.name}','${p.status}')">
        ${isRunning ? '⏹ Stop' : '▶ Start'}
      </button>
      <button class="cbtn cbtn-restart" onclick="restartBot('${p.name}')" ${!isRunning?'disabled':''}>↺</button>
      <button class="cbtn cbtn-pause"   onclick="${isPaused ? `resumeBot('${p.name}')` : `pauseBot('${p.name}')`}"
              ${!isRunning && !isPaused ? 'disabled':''}>
        ${isPaused ? '▶ Resume' : '⏸ Pause'}
      </button>
      <button class="cbtn cbtn-log"      onclick="openLogModal('${p.name}')">📋</button>
      <button class="cbtn cbtn-metrics"  onclick="openMetricsModal('${p.name}')">📊</button>
      <button class="cbtn cbtn-env"      onclick="openEnvModal('${p.name}')" title="Edit env vars">🔧</button>
      <button class="cbtn cbtn-stdin"    onclick="openStdinModal('${p.name}')" ${!isRunning?'disabled':''} title="Inject stdin">⌨</button>
      <button class="cbtn cbtn-health"   onclick="openHealthModal('${p.name}')" title="Health check">${p.health ? (p.health.ok ? '🟢' : '🔴') : '🏥'}</button>
      <button class="cbtn cbtn-rl"       onclick="openRlModal('${p.name}')" title="Rate limit">${p.rate_limit?.limited ? '🚫' : '🚦'}</button>
      <button class="cbtn cbtn-webhook"  onclick="openWebhookModal('${p.name}')" title="Git webhook">${p.has_webhook ? '🔗✓' : '🔗'}</button>
      <button class="cbtn cbtn-zip"      onclick="openZipModal('${p.name}')" title="Deploy ZIP">📦</button>
      <button class="cbtn cbtn-rollback" onclick="openRollbackModal('${p.name}')" title="Rollback" ${p.commit_count < 2 || p.deploy_type==='zip' ? 'disabled':''}>⏪</button>
      <button class="cbtn cbtn-sched"    onclick="openSchedModal('${p.name}')" title="Schedule">${p.scheduled ? '⏰✓' : '⏰'}</button>
      ${webUIBtn}
      <button class="cbtn cbtn-delete" onclick="deleteBot('${p.name}')">🗑</button>
    `;
  }

  const uptimeLine = isRunning && p.uptime
    ? `<span class="meta-item">⏱ ${p.uptime}</span>` : '';
  const pidLine = p.pid
    ? `<span class="meta-item">PID ${p.pid}</span>` : '';
  const portLine = hasWebUI
    ? `<span class="meta-item">:${p.web_port}</span>` : '';
  const gitLine = p.git_url
    ? `<span class="meta-item meta-git" title="${p.git_url}">⎇ ${p.branch||'main'}</span>` : '';

  const cpuLine   = (isRunning && p.cpu  !== undefined) ? `<span class="meta-item meta-cpu" title="CPU">⚡ ${p.cpu}%</span>` : '';
  const memLine   = (isRunning && p.mem_mb !== undefined && p.mem_mb > 0) ? `<span class="meta-item meta-mem" title="RAM">💾 ${p.mem_mb}MB</span>` : '';
  const rcLine    = (p.restart_count > 0) ? `<span class="meta-item meta-rc" title="Total restarts">↺ ${p.restart_count}</span>` : '';
  const healthLine = p.health
    ? `<span class="meta-item ${p.health.ok ? 'meta-health-ok' : 'meta-health-fail'}" title="Health: ${p.health.url}">${p.health.ok ? '🟢 Healthy' : `🔴 ${p.health.failures} fail${p.health.failures!==1?'s':''}`}</span>` : '';
  const rlLine    = (p.rate_limit?.limited)
    ? `<span class="meta-item meta-rl-limited" title="Rate limited">🚫 Rate limited</span>` : '';
  const webhookLine = p.has_webhook
    ? `<span class="meta-item meta-webhook" title="Webhook active">🔗 Webhook</span>` : '';
  const schedLine = p.scheduled
    ? `<span class="meta-item meta-sched" title="Auto-deploy scheduled">⏰ Scheduled</span>` : '';
  const deployTypeLine = (p.deploy_type === 'zip')
    ? `<span class="meta-item meta-zip" title="ZIP deployed">📦 ZIP</span>` : '';

  card.innerHTML = `
    <div class="card-header">
      <span class="card-title">${formatBotName(p.name)}</span>
      <div class="card-badges">${dockerBadge}<span class="tag ${statusClass}">${statusLabel}</span></div>
    </div>
    <div class="card-meta">
      ${uptimeLine}${pidLine}${portLine}${gitLine}${cpuLine}${memLine}${rcLine}${healthLine}${rlLine}${webhookLine}${schedLine}${deployTypeLine}
    </div>
    ${progressBar}
    <div class="card-controls">${controls}</div>
  `;
}

// ── Bot actions ───────────────────────────────────────────────

function toggleBot(name, status) {
  const action = status === 'RUNNING' ? 'stop' : 'start';
  if (action === 'stop') {
    confirmAction(`Stop ${formatBotName(name)}?`,
      'The bot process will be stopped.',
      'Stop', true, () => _supervisorAction(action, name));
  } else {
    _supervisorAction(action, name);
  }
}

function restartBot(name) {
  confirmAction(`Restart ${formatBotName(name)}?`,
    'The bot will pull latest code and restart.',
    'Restart', false, () => _supervisorAction('restart', name));
}

function pauseBot(name) {
  fetch(`/supervisor/pause/${name}`, { method:'POST' })
    .then(r => r.json()).then(d => {
      if (d.status === 'success') { showToast(`Paused ${formatBotName(name)}`,'info'); setTimeout(requestStatus,800); }
      else showToast(`Error: ${d.message}`,'error');
    });
}

function resumeBot(name) {
  fetch(`/supervisor/resume/${name}`, { method:'POST' })
    .then(r => r.json()).then(d => {
      if (d.status === 'success') { showToast(`Resumed ${formatBotName(name)}`,'success'); setTimeout(requestStatus,800); }
      else showToast(`Error: ${d.message}`,'error');
    });
}

function clearFailure(name) {
  fetch(`/supervisor/clear_failure/${name}`, { method:'POST' })
    .then(r => r.json()).then(d => {
      if (d.status === 'success') { showToast(`Retrying ${formatBotName(name)}`,'info'); setTimeout(requestStatus,1200); }
      else showToast(`Error: ${d.message}`,'error');
    });
}

function deleteBot(name) {
  confirmAction(`Delete ${formatBotName(name)}?`,
    'This will permanently stop and remove the bot, its config, and all files.',
    '🗑 Delete', true, () => {
      fetch(`/bot/delete/${name}`, { method:'DELETE' })
        .then(r => r.json()).then(d => {
          if (d.status === 'success') {
            showToast(`Deleted ${formatBotName(name)}`,'success');
            lastKnownProcesses = lastKnownProcesses.filter(p => p.name !== name);
            setTimeout(requestStatus, 500);
          } else {
            showToast(`Delete failed: ${d.message}`,'error');
          }
        });
    });
}

function _supervisorAction(action, name) {
  showToast(`${action.charAt(0).toUpperCase()+action.slice(1)}ing ${formatBotName(name)}…`,'info',2000);
  fetch(`/supervisor/${action}/${name}`, { method:'POST' })
    .then(r => r.json()).then(d => {
      if (d.status === 'success') {
        showToast(`${formatBotName(name)} ${action}ped`,'success');
        setTimeout(requestStatus, 1000);
      } else {
        showToast(`Error: ${d.message}`,'error');
      }
    })
    .catch(() => showToast(`Network error`,'error'));
}

function restartAllBots() {
  confirmAction('Restart all bots?',
    'All running bots will pull latest code and restart.',
    '↺ Restart All', false, () => {
      lastKnownProcesses.forEach(p => {
        if (p.status === 'RUNNING') _supervisorAction('restart', p.name);
      });
      showToast('Restarting all running bots…','info');
    });
}

function stopAllBots() {
  confirmAction('Stop all bots?',
    'All running bots will be stopped.',
    '⏹ Stop All', true, () => {
      lastKnownProcesses.forEach(p => {
        if (p.status === 'RUNNING') _supervisorAction('stop', p.name);
      });
      showToast('Stopping all bots…','info');
    });
}

// ── Add Bot Modal ─────────────────────────────────────────────

function openAddModal() {
  document.getElementById('add-modal').style.display = 'flex';
  document.getElementById('add-status').style.display = 'none';
  switchDeployTab('git');
}

function closeAddModal() {
  document.getElementById('add-modal').style.display = 'none';
}

function switchDeployTab(tab) {
  deployTab = tab;
  document.querySelectorAll('.dtab').forEach(b => b.classList.remove('active'));
  document.getElementById(`dtab-${tab}`).classList.add('active');
  document.getElementById('fg-run-command').style.display   = tab === 'git'    ? '' : 'none';
  document.getElementById('fg-python-version').style.display = tab === 'git'   ? '' : 'none';
  document.getElementById('fg-web-port').style.display       = tab === 'docker' ? '' : 'none';
  document.getElementById('fg-build-args').style.display     = tab === 'docker' ? '' : 'none';
}

async function submitAddBot() {
  const statusEl = document.getElementById('add-status');
  statusEl.style.display = 'block';

  const name       = document.getElementById('add-name').value.trim();
  const gitUrl     = document.getElementById('add-git-url').value.trim();
  const branch     = document.getElementById('add-branch').value.trim() || 'main';
  const runCommand = document.getElementById('add-run-command').value.trim();
  const pyVer      = document.getElementById('add-python-version').value.trim();
  const webPort    = document.getElementById('add-web-port').value.trim();
  const envText    = document.getElementById('add-env').value.trim();
  const baText     = document.getElementById('add-build-args').value.trim();

  if (!name || !gitUrl) {
    setStatus(statusEl,'error','⚠ Bot Name and Git URL are required.'); return;
  }
  if (!/bot\d+$/i.test(name)) {
    setStatus(statusEl,'error','⚠ Name must end with botN (e.g. "my bot1").'); return;
  }
  if (deployTab === 'git' && !runCommand) {
    setStatus(statusEl,'error','⚠ Run Command is required for Git bots.'); return;
  }

  const payload = {
    process_name: name, git_url: gitUrl, branch, deploy_type: deployTab,
    run_command: runCommand, python_version: pyVer,
    env: parseEnvText(envText),
    build_args: parseEnvText(baText),
  };
  if (webPort) payload.web_port = parseInt(webPort);

  setStatus(statusEl,'info','⏳ Deploying — cloning & building…');
  try {
    const resp = await fetch('/bot/add', {
      method: 'POST', headers: {'Content-Type':'application/json'},
      body: JSON.stringify(payload)
    });
    const d = await resp.json();
    if (d.status === 'success') {
      setStatus(statusEl,'success', `✅ ${d.message}${d.web_proxy ? ` — Web UI: <a href="${d.web_proxy}" target="_blank">${d.web_proxy}</a>` : ''}`);
      showToast(`Bot deployed!`,'success');
      requestStatus();
    } else {
      setStatus(statusEl,'error', `❌ ${d.message}`);
    }
  } catch(e) {
    setStatus(statusEl,'error',`❌ Network error: ${e.message}`);
  }
}

function setStatus(el, type, html) {
  el.className = `status-banner status-${type}`;
  el.innerHTML = html;
  el.style.display = 'block';
}

// ── Log Modal ─────────────────────────────────────────────────

function openLogModal(name) {
  currentLogProcess = name;
  document.getElementById('log-modal').style.display = 'flex';
  document.getElementById('log-modal-title').textContent = formatBotName(name);
  document.getElementById('log-content').textContent = '⏳ Loading logs…';
  document.getElementById('log-search').value = '';
  startLogStream(name);
}

function closeLogModal() {
  document.getElementById('log-modal').style.display = 'none';
  if (logSSE) { logSSE.close(); logSSE = null; }
  currentLogProcess = null;
}

function startLogStream(name) {
  if (logSSE) { logSSE.close(); logSSE = null; }
  const pre = document.getElementById('log-content');
  pre.textContent = '';
  logSSE = new EventSource(`/supervisor/logtail/${name}?lines=300`);
  logSSE.onmessage = (e) => {
    try {
      const d = JSON.parse(e.data);
      pre.textContent += d.data;
      if (document.getElementById('log-autoscroll').checked) {
        pre.scrollTop = pre.scrollHeight;
      }
    } catch {}
  };
  logSSE.onerror = () => { pre.textContent += '\n[stream ended]'; };
}

function clearLogView() {
  document.getElementById('log-content').textContent = '';
}

function downloadCurrentLog() {
  if (!currentLogProcess) return;
  window.open(`/supervisor/log/${currentLogProcess}`, '_blank');
}

function filterLogLines(query) {
  const pre = document.getElementById('log-content');
  if (!query) { pre.style.setProperty('--log-filter',''); return; }
  // Highlight matching lines (CSS hack using mark)
  const lines = pre.textContent.split('\n');
  pre.innerHTML = lines.map(l =>
    l.toLowerCase().includes(query.toLowerCase())
      ? `<span class="log-match">${escapeHtml(l)}</span>`
      : `<span class="log-dim">${escapeHtml(l)}</span>`
  ).join('\n');
}

function escapeHtml(s) {
  return s.replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;');
}

// ── Tools menu ────────────────────────────────────────────────

function toggleToolsMenu() {
  document.getElementById('tools-menu').classList.toggle('open');
}

// ── Export / Import ───────────────────────────────────────────

function exportBots() {
  window.open('/bot/export','_blank');
  showToast('Export started','info');
}

async function importBots(input) {
  const file = input.files[0];
  if (!file) return;
  const formData = new FormData();
  formData.append('file', file);
  try {
    const resp = await fetch('/bot/import', { method:'POST', body: formData });
    const d    = await resp.json();
    if (d.status === 'success') {
      showToast(`Imported ${d.message}`,'success');
    } else {
      showToast(`Import failed: ${d.message}`,'error');
    }
  } catch(e) {
    showToast(`Import error: ${e.message}`,'error');
  }
  input.value = '';
}

// ── Cron ─────────────────────────────────────────────────────

function openCronModal()  { document.getElementById('cron-modal').style.display = 'flex'; }
function closeCronModal() { document.getElementById('cron-modal').style.display = 'none'; }

function loadCronSetting() {
  fetch('/config/cron').then(r=>r.json()).then(d => {
    if (d.hours !== undefined) document.getElementById('cron-hours').value = d.hours;
  }).catch(()=>{});
}

async function checkCapabilities() {
  try {
    const resp = await fetch('/system/capabilities');
    const d    = await resp.json();

    const tab = document.getElementById('dtab-docker');
    if (!tab) return;

    if (d.docker_available) {
      const runtime = d.container_runtime || 'docker';
      // Show what runtime is backing Docker deployment
      tab.textContent = runtime === 'podman' ? '🦭 Dockerfile (Podman)' : '🐳 Dockerfile';
      tab.title = runtime === 'podman'
        ? 'Using Podman as Docker-compatible runtime (daemonless)'
        : 'Using Docker runtime';
    } else {
      // No container runtime — disable tab
      tab.disabled = true;
      tab.title    = 'No container runtime available. Use Git or ZIP deployment.';
      tab.style.opacity = '0.4';
      tab.style.cursor  = 'not-allowed';
      // Hide Docker filter tab
      document.querySelectorAll('.ftab[data-filter="docker"]').forEach(b => {
        b.style.display = 'none';
      });
    }
  } catch {}
}

function saveCron() {
  const hours = parseInt(document.getElementById('cron-hours').value) || 0;
  fetch('/config/cron', {
    method:'POST', headers:{'Content-Type':'application/json'},
    body: JSON.stringify({hours})
  }).then(r=>r.json()).then(d => {
    if (d.status === 'success') { closeCronModal(); showToast('Cron schedule saved','success'); }
    else showToast('Failed to save cron','error');
  });
}

// ════════════════════════════════════════════════════════════════
//  Metrics Modal
// ════════════════════════════════════════════════════════════════

let cpuChart   = null;
let memChart   = null;
let currentMetricsBot  = null;
let currentMetricsWin  = 60;

const CHART_DEFAULTS = {
  responsive: true,
  animation: false,
  plugins: { legend: { display: false }, tooltip: { mode: 'index', intersect: false } },
  scales: {
    x: { ticks: { maxTicksLimit: 8, color: '#556b91', font: { size: 11 } }, grid: { color: '#1e2d4d' } },
    y: { beginAtZero: true, ticks: { color: '#556b91', font: { size: 11 } }, grid: { color: '#1e2d4d' } },
  },
};

function openMetricsModal(name) {
  currentMetricsBot = name;
  currentMetricsWin = parseInt(document.getElementById('metrics-window').value) || 60;
  document.getElementById('metrics-modal-title').textContent = formatBotName(name);
  document.getElementById('metrics-modal').style.display = 'flex';
  loadMetrics(name, currentMetricsWin);
}

function closeMetricsModal() {
  document.getElementById('metrics-modal').style.display = 'none';
  if (cpuChart) { cpuChart.destroy(); cpuChart = null; }
  if (memChart) { memChart.destroy(); memChart = null; }
  currentMetricsBot = null;
}

function changeMetricsWindow(val) {
  currentMetricsWin = parseInt(val);
  if (currentMetricsBot) loadMetrics(currentMetricsBot, currentMetricsWin);
}

async function loadMetrics(name, windowMin) {
  try {
    const resp = await fetch(`/metrics/${name}?window=${windowMin}`);
    const d = await resp.json();
    if (d.status !== 'success') return;
    renderMetrics(d);
  } catch (e) {
    console.error('Metrics fetch error:', e);
  }
}

function renderMetrics(d) {
  const samples = d.samples || [];
  const labels  = samples.map(s => {
    const dt = new Date(s.ts * 1000);
    return dt.toLocaleTimeString([], {hour:'2-digit', minute:'2-digit'});
  });
  const cpuData = samples.map(s => s.cpu);
  const memData = samples.map(s => s.mem_mb);

  // Summary pills
  const lastCpu = cpuData.length ? cpuData[cpuData.length-1] : 0;
  const lastMem = memData.length ? memData[memData.length-1] : 0;
  document.getElementById('ms-cpu').textContent      = `${lastCpu}%`;
  document.getElementById('ms-mem').textContent      = `${lastMem} MB`;
  document.getElementById('ms-uptime').textContent   = `${d.uptime_pct}%`;
  document.getElementById('ms-restarts').textContent = d.restart_count;

  // CPU chart
  const cpuCtx = document.getElementById('cpu-chart').getContext('2d');
  if (cpuChart) cpuChart.destroy();
  cpuChart = new Chart(cpuCtx, {
    type: 'line',
    data: {
      labels,
      datasets: [{
        data: cpuData,
        borderColor: '#38bdf8',
        backgroundColor: 'rgba(56,189,248,0.08)',
        borderWidth: 1.5,
        pointRadius: 0,
        fill: true,
        tension: 0.3,
      }]
    },
    options: { ...CHART_DEFAULTS, scales: { ...CHART_DEFAULTS.scales, y: { ...CHART_DEFAULTS.scales.y, max: 100 } } }
  });

  // Memory chart
  const memCtx = document.getElementById('mem-chart').getContext('2d');
  if (memChart) memChart.destroy();
  memChart = new Chart(memCtx, {
    type: 'line',
    data: {
      labels,
      datasets: [{
        data: memData,
        borderColor: '#a78bfa',
        backgroundColor: 'rgba(167,139,250,0.08)',
        borderWidth: 1.5,
        pointRadius: 0,
        fill: true,
        tension: 0.3,
      }]
    },
    options: CHART_DEFAULTS
  });

  // Uptime history bar
  renderUptimeBar(d.uptime_history || []);
}

function renderUptimeBar(history) {
  const wrap = document.getElementById('uptime-bar-wrap');
  wrap.innerHTML = '';
  const now   = Date.now() / 1000;
  const start = now - 7 * 86400;
  const total = now - start;
  const SLOTS = 168;  // 168 × 1-hour slots = 7 days
  const slotSec = total / SLOTS;

  // Build slot states
  const slots = new Array(SLOTS).fill('stopped');
  const sorted = [...history].sort((a,b) => a.ts - b.ts);

  let runningFrom = null;
  for (const ev of sorted) {
    if (ev.event === 'start') {
      runningFrom = ev.ts;
    } else if (ev.event === 'crash' || ev.event === 'stop') {
      if (runningFrom !== null) {
        _fillSlots(slots, start, slotSec, runningFrom, ev.ts, ev.event === 'crash' ? 'crash' : 'online');
      }
      runningFrom = null;
    }
  }
  if (runningFrom !== null) {
    _fillSlots(slots, start, slotSec, runningFrom, now, 'online');
  }

  // Render
  slots.forEach((state, i) => {
    const seg = document.createElement('div');
    seg.className = `ub-seg ub-${state}`;
    const slotTs = start + i * slotSec;
    const dt = new Date(slotTs * 1000);
    seg.title = `${dt.toLocaleString()} — ${state}`;
    wrap.appendChild(seg);
  });
}

function _fillSlots(slots, start, slotSec, from, to, state) {
  const si = Math.max(0, Math.floor((from - start) / slotSec));
  const ei = Math.min(slots.length - 1, Math.floor((to - start) / slotSec));
  for (let i = si; i <= ei; i++) slots[i] = state;
}


// ════════════════════════════════════════════════════════════════
//  Alert Config Modal
// ════════════════════════════════════════════════════════════════

async function openAlertModal() {
  document.getElementById('alert-modal').style.display = 'flex';
  document.getElementById('alert-status').style.display = 'none';
  try {
    const r = await fetch('/config/alerts');
    const d = await r.json();
    if (d.config) {
      document.getElementById('alert-tg-token').value  = '';   // never show token
      document.getElementById('alert-tg-chat').value   = d.config.telegram_chat_id || '';
      document.getElementById('alert-discord').value   = d.config.discord_webhook  || '';
    }
  } catch {}
}

function closeAlertModal() {
  document.getElementById('alert-modal').style.display = 'none';
}

async function saveAlerts() {
  const statusEl = document.getElementById('alert-status');
  const payload = {
    telegram_chat_id:  document.getElementById('alert-tg-chat').value.trim(),
    discord_webhook:   document.getElementById('alert-discord').value.trim(),
  };
  const token = document.getElementById('alert-tg-token').value.trim();
  if (token) payload.telegram_token = token;

  try {
    const r = await fetch('/config/alerts', {
      method:'POST', headers:{'Content-Type':'application/json'},
      body: JSON.stringify(payload)
    });
    const d = await r.json();
    if (d.status === 'success') {
      setStatus(statusEl, 'success', '✅ Alert settings saved');
      showToast('Alert settings saved', 'success');
    } else {
      setStatus(statusEl, 'error', `❌ ${d.message}`);
    }
  } catch (e) {
    setStatus(statusEl, 'error', `❌ Network error: ${e.message}`);
  }
}

async function testAlert() {
  const statusEl = document.getElementById('alert-status');
  setStatus(statusEl, 'info', '⏳ Sending test alert…');
  try {
    const r = await fetch('/config/alerts/test', { method:'POST' });
    const d = await r.json();
    if (d.status === 'success') {
      setStatus(statusEl, 'success', '✅ Test alert sent! Check your Telegram/Discord.');
    } else {
      setStatus(statusEl, 'error', `❌ ${d.message}`);
    }
  } catch (e) {
    setStatus(statusEl, 'error', `❌ ${e.message}`);
  }
}

// ════════════════════════════════════════════════════════════════
//  1. Live stdin injection
// ════════════════════════════════════════════════════════════════

let stdinBot = null;

function openStdinModal(name) {
  stdinBot = name;
  document.getElementById('stdin-modal-title').textContent = formatBotName(name);
  document.getElementById('stdin-modal').style.display = 'flex';
  document.getElementById('stdin-status').style.display = 'none';
  document.getElementById('stdin-history').innerHTML = '';
  document.getElementById('stdin-line').value = '';
  setTimeout(() => document.getElementById('stdin-line').focus(), 80);
}

function closeStdinModal() {
  document.getElementById('stdin-modal').style.display = 'none';
  stdinBot = null;
}

async function sendStdin() {
  if (!stdinBot) return;
  const input = document.getElementById('stdin-line');
  const line  = input.value.trim();
  if (!line) return;

  const history = document.getElementById('stdin-history');
  const entry   = document.createElement('div');
  entry.className = 'stdin-entry stdin-sent';
  entry.textContent = `$ ${line}`;
  history.appendChild(entry);
  history.scrollTop = history.scrollHeight;
  input.value = '';

  try {
    const resp = await fetch(`/bot/stdin/${stdinBot}`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ line })
    });
    const d = await resp.json();
    const fb = document.createElement('div');
    fb.className = d.status === 'success' ? 'stdin-entry stdin-ok' : 'stdin-entry stdin-err';
    fb.textContent = d.status === 'success' ? '✓ Sent' : `✗ ${d.message}`;
    history.appendChild(fb);
    history.scrollTop = history.scrollHeight;
  } catch (e) {
    const fb = document.createElement('div');
    fb.className = 'stdin-entry stdin-err';
    fb.textContent = `✗ Network error: ${e.message}`;
    history.appendChild(fb);
  }
}


// ════════════════════════════════════════════════════════════════
//  2. Environment variable editor
// ════════════════════════════════════════════════════════════════

let envBot = null;

async function openEnvModal(name) {
  envBot = name;
  document.getElementById('env-modal-title').textContent = formatBotName(name);
  document.getElementById('env-modal').style.display = 'flex';
  document.getElementById('env-status').style.display = 'none';
  document.getElementById('env-rows').innerHTML = '<div style="color:var(--text-muted);font-size:13px">Loading…</div>';

  try {
    const resp = await fetch(`/bot/env/${name}`);
    const d    = await resp.json();
    const env  = d.env || {};
    renderEnvRows(env);
  } catch (e) {
    document.getElementById('env-rows').innerHTML =
      `<div style="color:var(--red)">Failed to load: ${e.message}</div>`;
  }
}

function closeEnvModal() {
  document.getElementById('env-modal').style.display = 'none';
  envBot = null;
}

function renderEnvRows(env) {
  const container = document.getElementById('env-rows');
  container.innerHTML = '';
  const entries = Object.entries(env);
  if (entries.length === 0) {
    addEnvRow('', '');
  } else {
    entries.forEach(([k, v]) => addEnvRow(k, v));
  }
}

function addEnvRow(key = '', val = '') {
  const container = document.getElementById('env-rows');
  const row = document.createElement('div');
  row.className = 'env-row';
  row.innerHTML = `
    <input class="env-key"  type="text"     value="${escapeAttr(key)}" placeholder="KEY">
    <span class="env-eq">=</span>
    <input class="env-val"  type="text"     value="${escapeAttr(val)}" placeholder="value">
    <button class="cbtn cbtn-delete env-del" onclick="this.closest('.env-row').remove()" title="Remove">✕</button>
  `;
  container.appendChild(row);
  row.querySelector('.env-key').focus();
}

function escapeAttr(s) {
  return String(s).replace(/"/g, '&quot;').replace(/'/g, '&#39;');
}

function collectEnv() {
  const env = {};
  document.querySelectorAll('.env-row').forEach(row => {
    const k = row.querySelector('.env-key').value.trim();
    const v = row.querySelector('.env-val').value.trim();
    if (k) env[k] = v;
  });
  return env;
}

async function saveEnv(doRestart) {
  if (!envBot) return;
  const statusEl = document.getElementById('env-status');
  const env = collectEnv();

  setStatus(statusEl, 'info', `⏳ Saving${doRestart ? ' and restarting' : ''}…`);

  try {
    const resp = await fetch(`/bot/env/${envBot}`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ env, restart: doRestart })
    });
    const d = await resp.json();
    if (d.status === 'success') {
      setStatus(statusEl, 'success', `✅ ${d.message}`);
      showToast('Env vars updated', 'success');
      if (doRestart) setTimeout(requestStatus, 1500);
    } else {
      setStatus(statusEl, 'error', `❌ ${d.message}`);
    }
  } catch (e) {
    setStatus(statusEl, 'error', `❌ Network error: ${e.message}`);
  }
}


// ════════════════════════════════════════════════════════════════
//  3. Health check
// ════════════════════════════════════════════════════════════════

let healthBot = null;

async function openHealthModal(name) {
  healthBot = name;
  document.getElementById('health-modal-title').textContent = formatBotName(name);
  document.getElementById('health-modal').style.display = 'flex';
  document.getElementById('health-status').style.display = 'none';
  document.getElementById('health-state-box').style.display = 'none';

  try {
    const resp = await fetch(`/bot/health/${name}`);
    const d    = await resp.json();
    const cfg  = d.config || {};
    const st   = d.state  || {};

    document.getElementById('health-url').value      = cfg.url      || '';
    document.getElementById('health-interval').value = cfg.interval_sec || 30;
    document.getElementById('health-timeout').value  = cfg.timeout_sec  || 5;
    document.getElementById('health-enabled').value  = cfg.enabled === false ? 'false' : 'true';

    if (cfg.url) {
      document.getElementById('health-state-box').style.display = 'block';
      const ok = st.consecutive_failures === 0 && st.last_ok;
      document.getElementById('hsb-status').innerHTML =
        ok ? '<span style="color:var(--green)">✓ Healthy</span>'
           : `<span style="color:var(--red)">✗ Failing</span>`;
      document.getElementById('hsb-last-ok').textContent =
        st.last_ok ? new Date(st.last_ok * 1000).toLocaleString() : 'Never';
      document.getElementById('hsb-failures').textContent = st.consecutive_failures || 0;
    }
  } catch (e) {
    setStatus(document.getElementById('health-status'), 'error', `Failed to load: ${e.message}`);
  }
}

function closeHealthModal() {
  document.getElementById('health-modal').style.display = 'none';
  healthBot = null;
}

async function saveHealth() {
  if (!healthBot) return;
  const statusEl = document.getElementById('health-status');
  const payload  = {
    url:          document.getElementById('health-url').value.trim(),
    interval_sec: parseInt(document.getElementById('health-interval').value) || 30,
    timeout_sec:  parseInt(document.getElementById('health-timeout').value)  || 5,
    enabled:      document.getElementById('health-enabled').value === 'true',
  };
  setStatus(statusEl, 'info', '⏳ Saving…');
  try {
    const resp = await fetch(`/bot/health/${healthBot}`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(payload)
    });
    const d = await resp.json();
    if (d.status === 'success') {
      setStatus(statusEl, 'success', `✅ ${d.message}`);
      showToast('Health check saved', 'success');
    } else {
      setStatus(statusEl, 'error', `❌ ${d.message}`);
    }
  } catch (e) {
    setStatus(statusEl, 'error', `❌ ${e.message}`);
  }
}

async function removeHealth() {
  if (!healthBot) return;
  try {
    await fetch(`/bot/health/${healthBot}`, { method: 'DELETE' });
    showToast('Health check removed', 'info');
    closeHealthModal();
  } catch (e) {
    showToast(`Error: ${e.message}`, 'error');
  }
}


// ════════════════════════════════════════════════════════════════
//  4. Restart rate limiter
// ════════════════════════════════════════════════════════════════

let rlBot = null;

async function openRlModal(name) {
  rlBot = name;
  document.getElementById('rl-modal-title').textContent = formatBotName(name);
  document.getElementById('ratelimit-modal').style.display = 'flex';
  document.getElementById('rl-status').style.display = 'none';
  document.getElementById('rl-current').style.display = 'none';

  try {
    const resp = await fetch(`/bot/ratelimit/${name}`);
    const d    = await resp.json();
    const rl   = d.rate_limit || {};

    if (rl.max_restarts) {
      document.getElementById('rl-max').value    = rl.max_restarts;
      document.getElementById('rl-window').value = rl.window_sec;
      document.getElementById('rl-current').style.display = 'block';
      document.getElementById('rl-remaining').textContent =
        `${d.recent_restarts} used of ${rl.max_restarts}`;
      document.getElementById('rl-window-disp').textContent =
        `${rl.window_sec}s (${Math.round(rl.window_sec/60)} min)`;
    }
  } catch (e) {
    setStatus(document.getElementById('rl-status'), 'error', `Load failed: ${e.message}`);
  }
}

function closeRlModal() {
  document.getElementById('ratelimit-modal').style.display = 'none';
  rlBot = null;
}

async function saveRateLimit() {
  if (!rlBot) return;
  const statusEl  = document.getElementById('rl-status');
  const maxR      = parseInt(document.getElementById('rl-max').value)    || 5;
  const windowSec = parseInt(document.getElementById('rl-window').value) || 3600;
  setStatus(statusEl, 'info', '⏳ Saving…');
  try {
    const resp = await fetch(`/bot/ratelimit/${rlBot}`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ max_restarts: maxR, window_sec: windowSec })
    });
    const d = await resp.json();
    if (d.status === 'success') {
      setStatus(statusEl, 'success', `✅ ${d.message}`);
      showToast('Rate limit saved', 'success');
    } else {
      setStatus(statusEl, 'error', `❌ ${d.message}`);
    }
  } catch (e) {
    setStatus(statusEl, 'error', `❌ ${e.message}`);
  }
}

async function removeRateLimit() {
  if (!rlBot) return;
  try {
    await fetch(`/bot/ratelimit/${rlBot}`, { method: 'DELETE' });
    showToast('Rate limit removed', 'info');
    closeRlModal();
  } catch (e) {
    showToast(`Error: ${e.message}`, 'error');
  }
}

// ════════════════════════════════════════════════════════════════
//  Feature 1 — Git Webhook config
// ════════════════════════════════════════════════════════════════

let webhookBot = null;

async function openWebhookModal(name) {
  webhookBot = name;
  document.getElementById('webhook-modal-title').textContent = formatBotName(name);
  document.getElementById('webhook-modal').style.display = 'flex';
  document.getElementById('webhook-status').style.display = 'none';
  document.getElementById('webhook-url').value    = 'Loading…';
  document.getElementById('webhook-secret').value = '';

  try {
    const resp = await fetch(`/bot/webhook/config/${name}`);
    const d    = await resp.json();
    document.getElementById('webhook-url').value    = d.webhook_url || '';
    document.getElementById('webhook-secret').value = d.has_secret
      ? '(secret set — regenerate to view)'
      : '(none — click Regenerate to create)';

    // Auto-generate a secret if none exists
    if (!d.has_secret) await regenWebhookSecret(true);
  } catch (e) {
    setStatus(document.getElementById('webhook-status'), 'error', `Load failed: ${e.message}`);
  }
}

function closeWebhookModal() {
  document.getElementById('webhook-modal').style.display = 'none';
  webhookBot = null;
}

function copyWebhookUrl() {
  const val = document.getElementById('webhook-url').value;
  navigator.clipboard.writeText(val).then(() => showToast('URL copied!', 'success'))
    .catch(() => showToast('Copy failed', 'error'));
}

async function regenWebhookSecret(silent = false) {
  if (!webhookBot) return;
  try {
    const resp = await fetch(`/bot/webhook/config/${webhookBot}`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ regenerate: true })
    });
    const d = await resp.json();
    if (d.status === 'success') {
      document.getElementById('webhook-secret').value = d.secret;
      document.getElementById('webhook-url').value    = d.webhook_url;
      if (!silent) showToast('Secret regenerated', 'success');
    }
  } catch (e) {
    if (!silent) showToast(`Error: ${e.message}`, 'error');
  }
}


// ════════════════════════════════════════════════════════════════
//  Feature 2 — ZIP upload deploy
// ════════════════════════════════════════════════════════════════

let zipBot  = null;
let zipFile = null;

function openZipModal(name) {
  zipBot  = name;
  zipFile = null;
  document.getElementById('zip-modal-title').textContent = formatBotName(name);
  document.getElementById('zip-modal').style.display = 'flex';
  document.getElementById('zip-status').style.display = 'none';
  document.getElementById('zip-drop-label').textContent = '📂 Click to select or drag & drop a .zip';
  document.getElementById('zip-drop-zone').classList.remove('drag-over');
  document.getElementById('zip-file-input').value = '';
}

function closeZipModal() {
  document.getElementById('zip-modal').style.display = 'none';
  zipBot = zipFile = null;
}

function handleZipSelect(input) {
  if (input.files[0]) {
    zipFile = input.files[0];
    document.getElementById('zip-drop-label').textContent = `✅ ${zipFile.name}`;
  }
}

function handleZipDrop(e) {
  e.preventDefault();
  document.getElementById('zip-drop-zone').classList.remove('drag-over');
  const f = e.dataTransfer.files[0];
  if (f && f.name.endsWith('.zip')) {
    zipFile = f;
    document.getElementById('zip-drop-label').textContent = `✅ ${f.name}`;
  } else {
    showToast('Please drop a .zip file', 'error');
  }
}

async function submitZipDeploy() {
  if (!zipBot) return;
  const statusEl = document.getElementById('zip-status');

  if (!zipFile) {
    setStatus(statusEl, 'error', '⚠ Please select a ZIP file first.'); return;
  }

  const runCmd = document.getElementById('zip-run-command').value.trim();
  const pyVer  = document.getElementById('zip-python-version').value.trim();
  const envTxt = document.getElementById('zip-env').value.trim();

  const envObj = {};
  envTxt.split('\n').forEach(line => {
    const idx = line.indexOf('=');
    if (idx > 0) envObj[line.slice(0,idx).trim()] = line.slice(idx+1).trim();
  });

  const formData = new FormData();
  formData.append('file', zipFile);
  formData.append('run_command', runCmd || 'bot.py');
  formData.append('python_version', pyVer);
  formData.append('env_json', JSON.stringify(envObj));

  setStatus(statusEl, 'info', '⏳ Uploading and deploying…');
  try {
    const resp = await fetch(`/bot/upload/${zipBot}`, { method: 'POST', body: formData });
    const d    = await resp.json();
    if (d.status === 'success') {
      setStatus(statusEl, 'success', `✅ ${d.message}`);
      showToast('ZIP deployed!', 'success');
      setTimeout(requestStatus, 1000);
    } else {
      setStatus(statusEl, 'error', `❌ ${d.message}`);
    }
  } catch (e) {
    setStatus(statusEl, 'error', `❌ ${e.message}`);
  }
}


// ════════════════════════════════════════════════════════════════
//  Feature 3 — Rollback to previous commit
// ════════════════════════════════════════════════════════════════

let rollbackBot = null;

async function openRollbackModal(name) {
  rollbackBot = name;
  document.getElementById('rollback-modal-title').textContent = formatBotName(name);
  document.getElementById('rollback-modal').style.display = 'flex';
  document.getElementById('rollback-status').style.display = 'none';
  document.getElementById('commit-list').innerHTML = '<div class="commit-loading">Loading commits…</div>';

  try {
    const resp    = await fetch(`/bot/commits/${name}`);
    const d       = await resp.json();
    const commits = d.commits || [];

    if (!commits.length) {
      document.getElementById('commit-list').innerHTML =
        '<div class="commit-loading">No commit history available.</div>';
      return;
    }

    document.getElementById('commit-list').innerHTML = commits.map((c, i) => `
      <div class="commit-row ${i === 0 ? 'commit-current' : ''}">
        <div class="commit-info">
          <span class="commit-sha">${c.sha.slice(0,8)}</span>
          <span class="commit-msg">${escapeHtml(c.msg)}</span>
          <span class="commit-meta">${c.author} · ${c.ts.slice(0,16)}</span>
        </div>
        <div class="commit-actions">
          ${i === 0
            ? '<span class="commit-badge-current">current</span>'
            : `<button class="hbtn hbtn-sm" onclick="doRollback('${c.sha}','${escapeHtml(c.msg).slice(0,30)}')">⏪ Rollback</button>`
          }
        </div>
      </div>
    `).join('');
  } catch (e) {
    document.getElementById('commit-list').innerHTML =
      `<div class="commit-loading" style="color:var(--red)">Error: ${e.message}</div>`;
  }
}

function closeRollbackModal() {
  document.getElementById('rollback-modal').style.display = 'none';
  rollbackBot = null;
}

function doRollback(sha, msg) {
  confirmAction(`Rollback to ${sha.slice(0,8)}?`,
    `"${msg}" — The bot will restart at this exact commit.`,
    '⏪ Rollback', true, async () => {
      const statusEl = document.getElementById('rollback-status');
      setStatus(statusEl, 'info', `⏳ Rolling back to ${sha.slice(0,8)}…`);
      try {
        const resp = await fetch(`/bot/rollback/${rollbackBot}`, {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ sha })
        });
        const d = await resp.json();
        if (d.status === 'success') {
          setStatus(statusEl, 'success', `✅ Rolled back to ${sha.slice(0,8)}`);
          showToast('Rollback complete', 'success');
          setTimeout(requestStatus, 1200);
        } else {
          setStatus(statusEl, 'error', `❌ ${d.message}`);
        }
      } catch (e) {
        setStatus(statusEl, 'error', `❌ ${e.message}`);
      }
    });
}


// ════════════════════════════════════════════════════════════════
//  Feature 4 — Scheduled deployments
// ════════════════════════════════════════════════════════════════

let schedBot = null;

async function openSchedModal(name) {
  schedBot = name;
  document.getElementById('sched-modal-title').textContent = formatBotName(name);
  document.getElementById('schedule-modal').style.display = 'flex';
  document.getElementById('sched-status').style.display = 'none';
  document.getElementById('sched-current').style.display = 'none';

  try {
    const resp  = await fetch(`/bot/schedule/${name}`);
    const d     = await resp.json();
    const sched = d.schedule || {};

    if (sched.interval_hours) {
      document.getElementById('sched-hours').value   = sched.interval_hours;
      document.getElementById('sched-enabled').value = sched.enabled ? 'true' : 'false';
      document.getElementById('sched-current').style.display = 'block';
      document.getElementById('sched-next').textContent =
        sched.next_run ? new Date(sched.next_run * 1000).toLocaleString() : '—';
      document.getElementById('sched-last').textContent =
        sched.last_run ? new Date(sched.last_run * 1000).toLocaleString() : 'Never';
    }
  } catch (e) {
    setStatus(document.getElementById('sched-status'), 'error', `Load failed: ${e.message}`);
  }
}

function closeSchedModal() {
  document.getElementById('schedule-modal').style.display = 'none';
  schedBot = null;
}

async function saveSchedule() {
  if (!schedBot) return;
  const statusEl = document.getElementById('sched-status');
  const hours    = parseFloat(document.getElementById('sched-hours').value) || 24;
  const enabled  = document.getElementById('sched-enabled').value === 'true';

  setStatus(statusEl, 'info', '⏳ Saving…');
  try {
    const resp = await fetch(`/bot/schedule/${schedBot}`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ interval_hours: hours, enabled })
    });
    const d = await resp.json();
    if (d.status === 'success') {
      setStatus(statusEl, 'success', `✅ ${d.message}`);
      showToast('Schedule saved', 'success');
      if (d.schedule?.next_run) {
        document.getElementById('sched-current').style.display = 'block';
        document.getElementById('sched-next').textContent =
          new Date(d.schedule.next_run * 1000).toLocaleString();
        document.getElementById('sched-last').textContent = 'Never';
      }
    } else {
      setStatus(statusEl, 'error', `❌ ${d.message}`);
    }
  } catch (e) {
    setStatus(statusEl, 'error', `❌ ${e.message}`);
  }
}

async function removeSchedule() {
  if (!schedBot) return;
  try {
    await fetch(`/bot/schedule/${schedBot}`, { method: 'DELETE' });
    showToast('Schedule removed', 'info');
    closeSchedModal();
  } catch (e) {
    showToast(`Error: ${e.message}`, 'error');
  }
}
