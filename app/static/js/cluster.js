let socket;
let updateInterval;
let reconnectAttempts = 0;
const MAX_RECONNECT_ATTEMPTS = 5;
let lastKnownProcesses = [];
let _buildEventSource = null;

// ── Socket.IO ──────────────────────────────────────────────────

document.addEventListener('DOMContentLoaded', function () {
    socket = io({
        reconnection: true,
        reconnectionDelay: 1000,
        reconnectionDelayMax: 5000,
        reconnectionAttempts: MAX_RECONNECT_ATTEMPTS,
    });

    socket.on('connect', function () {
        reconnectAttempts = 0;
        requestStatus();
        if (updateInterval) clearInterval(updateInterval);
        updateInterval = setInterval(requestStatus, 3000);
    });

    socket.on('disconnect', function () {
        if (updateInterval) clearInterval(updateInterval);
    });

    socket.on('connect_error', function () { reconnectAttempts++; });

    socket.on('status_update', function (data) {
        if (data.processes && data.processes.length > 0) {
            lastKnownProcesses = data.processes;
            updateBotCards(data.processes);
        } else if (lastKnownProcesses.length > 0) {
            updateBotCards(lastKnownProcesses);
        }
    });

    window.onclick = function (e) {
        ['log-modal', 'cron-modal', 'addbot-modal'].forEach(id => {
            const m = document.getElementById(id);
            if (e.target === m) closeModal(id);
        });
    };

    loadCronSetting();
});

function requestStatus() {
    if (socket && socket.connected) socket.emit('request_status');
    else socket.connect();
}

// ── Bot card rendering ─────────────────────────────────────────

function getBotNumber(name) {
    const m = name.match(/(\d+)$/);
    return m ? parseInt(m[1]) : null;
}

function sortProcesses(ps) {
    return [...ps].sort((a, b) => {
        const na = getBotNumber(a.name) ?? Infinity;
        const nb = getBotNumber(b.name) ?? Infinity;
        if (na !== nb) return na - nb;
        return a.name.localeCompare(b.name);
    });
}

function formatBotName(name) {
    const n = getBotNumber(name);
    return n !== null ? `Bot #${n}` : name;
}

function updateBotCards(processes) {
    const grid = document.getElementById('bot-grid');
    if (!grid) return;
    const sorted = sortProcesses(processes);
    let online = 0, offline = 0, paused = 0;
    grid.innerHTML = '';

    sorted.forEach(proc => {
        const isRunning   = proc.status === 'RUNNING';
        const isPaused    = proc.paused;
        const isAutoPaused = proc.auto_paused;
        const panelSlug   = proc.panel_slug;

        if (isRunning && !isPaused && !isAutoPaused) online++;
        else if (isPaused || isAutoPaused) paused++;
        else offline++;

        let statusClass, statusLabel;
        if (isAutoPaused)       { statusClass = 'status-fatal';   statusLabel = 'Failed';  }
        else if (isPaused)      { statusClass = 'status-paused';  statusLabel = 'Paused';  }
        else if (isRunning)     { statusClass = 'status-online';  statusLabel = 'Online';  }
        else                    { statusClass = 'status-offline'; statusLabel = 'Offline'; }

        const panelBadge = panelSlug
            ? `<a href="/${panelSlug}" target="_blank" class="panel-badge">
                 <svg width="10" height="10" viewBox="0 0 16 16" fill="currentColor">
                   <path d="M2 2h12v2H2zm0 4h8v2H2zm0 4h10v2H2zm0 4h6v2H2z"/>
                 </svg>/${panelSlug}</a>`
            : '';

        const esc = s => s.replace(/'/g, "\\'");

        let controls;
        if (isAutoPaused) {
            controls = `
              <button onclick="clearFailure('${esc(proc.name)}')" class="control-btn clear-btn">Clear &amp; Restart</button>
              <button onclick="viewLogs('${esc(proc.name)}')"     class="control-btn log-btn">Logs</button>
              <button onclick="removeBot('${esc(proc.name)}')"    class="control-btn remove-btn">Remove</button>`;
        } else {
            const pauseLabel  = isPaused ? 'Resume' : 'Pause';
            const pauseAction = isPaused
                ? `resumeBot('${esc(proc.name)}')`
                : `pauseBot('${esc(proc.name)}')`;
            controls = `
              <button onclick="toggleBot('${esc(proc.name)}','${proc.status}')"
                      class="control-btn ${isRunning ? 'stop-btn' : 'start-btn'}">
                ${isRunning ? 'Stop' : 'Start'}
              </button>
              <button onclick="restartBot('${esc(proc.name)}')"
                      class="control-btn restart-btn" ${!isRunning ? 'disabled' : ''}>Restart</button>
              <button onclick="${pauseAction}"
                      class="control-btn pause-btn"   ${!isRunning ? 'disabled' : ''}>${pauseLabel}</button>
              <button onclick="viewLogs('${esc(proc.name)}')"  class="control-btn log-btn">Logs</button>
              <button onclick="removeBot('${esc(proc.name)}')" class="control-btn remove-btn">Remove</button>`;
        }

        const utcTime = new Date().toISOString().replace('T', ' ').slice(0, 19);
        const card = document.createElement('div');
        card.className = 'bot-card' + (isAutoPaused ? ' card-fatal' : '');
        card.innerHTML = `
          <div class="bot-header">
            <h2>${formatBotName(proc.name)}${panelBadge}</h2>
            <span class="bot-status ${statusClass}">${statusLabel}</span>
          </div>
          <div class="bot-info">
            <p><strong>Process:</strong> ${proc.name}</p>
            <p><strong>Status:</strong>  ${proc.status}</p>
            <p><strong>PID:</strong>     ${proc.pid    || 'N/A'}</p>
            <p><strong>Uptime:</strong>  ${proc.uptime || '0:00:00'}</p>
            <p><strong>Updated:</strong> ${utcTime}</p>
          </div>
          <div class="bot-controls">${controls}</div>`;
        grid.appendChild(card);
    });

    const $  = id => document.getElementById(id);
    if ($('stat-online'))     $('stat-online').textContent  = online;
    if ($('stat-offline'))    $('stat-offline').textContent = offline;
    if ($('stat-paused'))     $('stat-paused').textContent  = paused;
    if ($('bot-count-badge')) $('bot-count-badge').textContent =
        `${sorted.length} bot${sorted.length !== 1 ? 's' : ''}`;
}

// ── Bot actions ────────────────────────────────────────────────

function toggleBot(name, status) {
    const action = status === 'RUNNING' ? 'stop' : 'start';
    if (action === 'stop' && !confirm(`Stop ${formatBotName(name)}?`)) return;
    fetch(`/supervisor/${action}/${name}`, { method: 'POST' })
        .then(r => r.json())
        .then(d => { if (d.status === 'success') setTimeout(requestStatus, 1000); else alert(d.message); })
        .catch(() => alert(`Failed to ${action}.`));
}

function restartBot(name) {
    if (!confirm(`Restart ${formatBotName(name)}?`)) return;
    fetch(`/supervisor/restart/${name}`, { method: 'POST' })
        .then(r => r.json())
        .then(d => { if (d.status === 'success') setTimeout(requestStatus, 2000); else alert(d.message); })
        .catch(() => alert('Failed to restart.'));
}

function pauseBot(name) {
    fetch(`/supervisor/pause/${name}`, { method: 'POST' })
        .then(r => r.json())
        .then(d => { if (d.status === 'success') setTimeout(requestStatus, 1000); else alert(d.message); });
}

function resumeBot(name) {
    fetch(`/supervisor/resume/${name}`, { method: 'POST' })
        .then(r => r.json())
        .then(d => { if (d.status === 'success') setTimeout(requestStatus, 1000); else alert(d.message); });
}

function clearFailure(name) {
    fetch(`/supervisor/clear_failure/${name}`, { method: 'POST' })
        .then(r => r.json())
        .then(d => { if (d.status === 'success') setTimeout(requestStatus, 1500); else alert(d.message); })
        .catch(() => alert('Failed to clear failure.'));
}

function viewLogs(name) {
    fetch(`/supervisor/log/${name}`)
        .then(r => { if (!r.ok) throw new Error(`HTTP ${r.status}`); return r.blob(); })
        .then(blob => {
            const url = URL.createObjectURL(blob);
            const a   = Object.assign(document.createElement('a'),
                { href: url, download: `${name}_log.txt`, style: 'display:none' });
            document.body.appendChild(a); a.click();
            URL.revokeObjectURL(url); a.remove();
        })
        .catch(() => alert('Failed to fetch logs.'));
}

function removeBot(name) {
    if (!confirm(`Permanently remove ${formatBotName(name)}?\nThis will stop the process and delete all files.`)) return;
    fetch(`/api/bots/${name}`, { method: 'DELETE' })
        .then(r => r.json())
        .then(d => { if (d.status === 'success') setTimeout(requestStatus, 1500); else alert(d.message); })
        .catch(() => alert('Failed to remove bot.'));
}

// ── Add Bot Modal ──────────────────────────────────────────────

function openAddBotModal() {
    // Reset form
    ['ab-name','ab-git','ab-run','ab-pyver','ab-port','ab-label'].forEach(id => {
        const el = document.getElementById(id);
        if (el) el.value = '';
    });
    document.getElementById('ab-branch').value = 'main';
    document.getElementById('env-pairs').innerHTML = '';
    resetBuildLog();
    document.getElementById('addbot-submit-btn').disabled = false;
    document.getElementById('addbot-submit-btn').textContent = 'Add Bot';
    document.getElementById('addbot-modal').style.display = 'block';
}

function closeAddBotModal() {
    if (_buildEventSource) { _buildEventSource.close(); _buildEventSource = null; }
    document.getElementById('addbot-modal').style.display = 'none';
}

function closeModal(id) {
    if (id === 'addbot-modal') closeAddBotModal();
    else document.getElementById(id).style.display = 'none';
}

function addEnvPair(key = '', val = '') {
    const row = document.createElement('div');
    row.className = 'env-pair';
    row.innerHTML = `
      <input type="text" placeholder="KEY"   value="${key}" class="env-key">
      <input type="text" placeholder="VALUE" value="${val}" class="env-val">
      <button onclick="this.parentElement.remove()">✕</button>`;
    document.getElementById('env-pairs').appendChild(row);
}

// ── Build log UI ───────────────────────────────────────────────

function resetBuildLog() {
    const el = document.getElementById('build-log');
    if (el) { el.textContent = ''; el.parentElement.style.display = 'none'; }
}

function showBuildLog() {
    const wrap = document.getElementById('build-log-wrap');
    if (wrap) wrap.style.display = 'block';
}

function appendLog(line) {
    const el = document.getElementById('build-log');
    if (!el) return;
    el.textContent += line + '\n';
    el.scrollTop = el.scrollHeight;
}

function setLogStatus(msg, color) {
    const el = document.getElementById('addbot-status');
    if (!el) return;
    el.style.color = color;
    el.textContent = msg;
}

// ── Submit ─────────────────────────────────────────────────────

function submitAddBot() {
    const btn = document.getElementById('addbot-submit-btn');

    const botname      = document.getElementById('ab-name').value.trim();
    const git_url      = document.getElementById('ab-git').value.trim();
    const branch       = document.getElementById('ab-branch').value.trim();
    const run_command  = document.getElementById('ab-run').value.trim();
    const python_version = document.getElementById('ab-pyver').value.trim() || null;
    const panel_port   = parseInt(document.getElementById('ab-port').value) || null;
    const panel_label  = document.getElementById('ab-label').value.trim() || botname;

    if (!botname || !git_url || !branch || !run_command) {
        setLogStatus('Please fill in all required fields.', '#f87171');
        return;
    }

    const env = {};
    document.querySelectorAll('.env-pair').forEach(row => {
        const k = row.querySelector('.env-key').value.trim();
        const v = row.querySelector('.env-val').value.trim();
        if (k) env[k] = v;
    });

    btn.disabled = true;
    btn.textContent = 'Building…';
    resetBuildLog();
    showBuildLog();
    setLogStatus('Starting build…', '#a78bfa');

    // Step 1: kick off the build job
    fetch('/api/bots', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ botname, git_url, branch, run_command,
                               env, python_version, panel_port, panel_label }),
    })
    .then(r => r.json())
    .then(data => {
        if (data.status !== 'started') {
            setLogStatus(`Error: ${data.message}`, '#f87171');
            btn.disabled = false;
            btn.textContent = 'Add Bot';
            return;
        }

        const jobId = data.job_id;
        setLogStatus('Build in progress…', '#a78bfa');

        // Step 2: stream the build log via SSE
        if (_buildEventSource) _buildEventSource.close();
        _buildEventSource = new EventSource(`/api/bots/stream/${jobId}`);

        _buildEventSource.onmessage = function (e) {
            let msg;
            try { msg = JSON.parse(e.data); } catch { return; }

            if (msg.type === 'log') {
                appendLog(msg.line);
            } else if (msg.type === 'done') {
                _buildEventSource.close();
                _buildEventSource = null;
                btn.disabled    = false;
                // Stop the pulsing dot
                const dot = document.getElementById('build-dot');
                if (dot) { dot.style.animation = 'none'; dot.style.background = msg.success ? '#4ade80' : '#f87171'; }
                btn.textContent = 'Add Bot';
                if (msg.success) {
                    setLogStatus(
                        msg.message + (msg.panel_slug ? ` · Panel at /${msg.panel_slug}` : ''),
                        '#4ade80'
                    );
                    setTimeout(() => { closeAddBotModal(); requestStatus(); }, 3000);
                } else {
                    setLogStatus(`Failed: ${msg.message}`, '#f87171');
                    appendLog(`\n✗ Build failed: ${msg.message}`);
                }
            } else if (msg.type === 'heartbeat') {
                // keep-alive — ignore
            }
        };

        _buildEventSource.onerror = function () {
            _buildEventSource.close();
            _buildEventSource = null;
            btn.disabled    = false;
            btn.textContent = 'Add Bot';
            setLogStatus('Connection lost during build.', '#f87171');
        };
    })
    .catch(err => {
        btn.disabled    = false;
        btn.textContent = 'Add Bot';
        setLogStatus(`Request failed: ${err}`, '#f87171');
    });
}

// ── Cron ───────────────────────────────────────────────────────

function openCronModal()  { document.getElementById('cron-modal').style.display = 'block'; }
function closeCronModal() { document.getElementById('cron-modal').style.display = 'none'; }

function loadCronSetting() {
    fetch('/config/cron').then(r => r.json()).then(d => {
        if (d.hours !== undefined) document.getElementById('cron-hours').value = d.hours;
    }).catch(() => {});
}

function saveCron() {
    const hours = parseInt(document.getElementById('cron-hours').value) || 0;
    fetch('/config/cron', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ hours }),
    }).then(r => r.json()).then(d => {
        if (d.status === 'success') closeCronModal();
        else alert('Failed to save cron setting.');
    }).catch(() => alert('Failed to save cron setting.'));
}

// ── Visibility / unload ────────────────────────────────────────

document.addEventListener('visibilitychange', function () {
    if (document.hidden) {
        if (updateInterval) clearInterval(updateInterval);
    } else {
        requestStatus();
        updateInterval = setInterval(requestStatus, 3000);
    }
});

window.onbeforeunload = function () {
    if (_buildEventSource) _buildEventSource.close();
    if (socket) socket.disconnect();
    if (updateInterval) clearInterval(updateInterval);
};
