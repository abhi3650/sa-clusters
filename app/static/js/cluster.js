let socket;
let updateInterval;
let reconnectAttempts = 0;
const MAX_RECONNECT_ATTEMPTS = 5;
let lastKnownProcesses = [];

document.addEventListener('DOMContentLoaded', function () {
    socket = io({
        reconnection: true,
        reconnectionDelay: 1000,
        reconnectionDelayMax: 5000,
        reconnectionAttempts: MAX_RECONNECT_ATTEMPTS
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

    socket.on('connect_error', function () {
        reconnectAttempts++;
    });

    socket.on('status_update', function (data) {
        if (data.processes && Array.isArray(data.processes) && data.processes.length > 0) {
            lastKnownProcesses = data.processes;
            updateBotCards(data.processes);
        } else if (lastKnownProcesses.length > 0) {
            updateBotCards(lastKnownProcesses);
        }
    });

    window.onclick = function (event) {
        ['log-modal', 'cron-modal', 'addbot-modal'].forEach(id => {
            const m = document.getElementById(id);
            if (event.target === m) m.style.display = 'none';
        });
    };

    loadCronSetting();
});

function requestStatus() {
    if (socket && socket.connected) socket.emit('request_status');
    else socket.connect();
}

function getBotNumber(processName) {
    const match = processName.match(/bot(\d+)$/i);
    return match ? parseInt(match[1]) : null;
}

function sortProcesses(processes) {
    return [...processes].sort((a, b) => {
        const na = getBotNumber(a.name) || 0;
        const nb = getBotNumber(b.name) || 0;
        return na - nb;
    });
}

function formatBotName(processName) {
    const n = getBotNumber(processName);
    return n ? `Bot #${n}` : processName;
}

function updateBotCards(processes) {
    const botGrid = document.getElementById('bot-grid');
    if (!botGrid) return;
    const sorted = sortProcesses(processes);
    let online = 0, offline = 0, paused = 0;
    botGrid.innerHTML = '';

    sorted.forEach(process => {
        const isRunning = process.status === 'RUNNING';
        const isPaused = process.paused;
        const isAutoPaused = process.auto_paused;
        const panelSlug = process.panel_slug;

        if (isRunning && !isPaused && !isAutoPaused) online++;
        else if (isPaused || isAutoPaused) paused++;
        else offline++;

        const displayName = formatBotName(process.name);
        const utcTime = new Date().toISOString().replace('T', ' ').slice(0, 19);

        let statusClass, statusLabel;
        if (isAutoPaused) { statusClass = 'status-fatal'; statusLabel = 'Failed'; }
        else if (isPaused) { statusClass = 'status-paused'; statusLabel = 'Paused'; }
        else if (isRunning) { statusClass = 'status-online'; statusLabel = 'Online'; }
        else { statusClass = 'status-offline'; statusLabel = 'Offline'; }

        const card = document.createElement('div');
        card.className = 'bot-card' + (isAutoPaused ? ' card-fatal' : '');

        const panelBadge = panelSlug
            ? `<a href="/${panelSlug}" target="_blank" class="panel-badge">
                   <svg width="10" height="10" viewBox="0 0 16 16" fill="currentColor"><path d="M2 2h12v2H2zm0 4h8v2H2zm0 4h10v2H2zm0 4h6v2H2z"/></svg>
                   /${panelSlug}
               </a>`
            : '';

        let controlsHTML = '';
        if (isAutoPaused) {
            controlsHTML = `
                <button onclick="clearFailure('${process.name}')" class="control-btn clear-btn">Clear &amp; Restart</button>
                <button onclick="viewLogs('${process.name}')" class="control-btn log-btn">Logs</button>
                <button onclick="removeBot('${process.name}')" class="control-btn remove-btn">Remove</button>
            `;
        } else {
            controlsHTML = `
                <button onclick="toggleBot('${process.name}', '${process.status}')"
                        class="control-btn ${isRunning ? 'stop-btn' : 'start-btn'}">
                    ${isRunning ? 'Stop' : 'Start'}
                </button>
                <button onclick="restartBot('${process.name}')"
                        class="control-btn restart-btn" ${!isRunning ? 'disabled' : ''}>
                    Restart
                </button>
                <button onclick="${isPaused ? `resumeBot('${process.name}')` : `pauseBot('${process.name}')`}"
                        class="control-btn pause-btn" ${!isRunning ? 'disabled' : ''}>
                    ${isPaused ? 'Resume' : 'Pause'}
                </button>
                <button onclick="viewLogs('${process.name}')" class="control-btn log-btn">Logs</button>
                <button onclick="removeBot('${process.name}')" class="control-btn remove-btn">Remove</button>
            `;
        }

        card.innerHTML = `
            <div class="bot-header">
                <h2>${displayName}${panelBadge}</h2>
                <span class="bot-status ${statusClass}">${statusLabel}</span>
            </div>
            <div class="bot-info">
                <p><strong>Process:</strong> ${process.name}</p>
                <p><strong>Status:</strong> ${process.status}</p>
                <p><strong>PID:</strong> ${process.pid || 'N/A'}</p>
                <p><strong>Uptime:</strong> ${process.uptime || '0:00:00'}</p>
                <p><strong>Updated:</strong> ${utcTime}</p>
            </div>
            <div class="bot-controls">${controlsHTML}</div>
        `;
        botGrid.appendChild(card);
    });

    const el = id => document.getElementById(id);
    if (el('stat-online')) el('stat-online').textContent = online;
    if (el('stat-offline')) el('stat-offline').textContent = offline;
    if (el('stat-paused')) el('stat-paused').textContent = paused;
    if (el('bot-count-badge')) el('bot-count-badge').textContent = `${sorted.length} bot${sorted.length !== 1 ? 's' : ''}`;
}

// ── Bot actions ────────────────────────────────────────────────

function toggleBot(processName, currentStatus) {
    const action = currentStatus === 'RUNNING' ? 'stop' : 'start';
    if (action === 'stop' && !confirm(`Stop ${formatBotName(processName)}?`)) return;
    fetch(`/supervisor/${action}/${processName}`, { method: 'POST' })
        .then(r => r.json())
        .then(data => { if (data.status === 'success') setTimeout(requestStatus, 1000); else alert(`Error: ${data.message}`); })
        .catch(() => alert(`Failed to ${action}.`));
}

function restartBot(processName) {
    if (!confirm(`Restart ${formatBotName(processName)}?`)) return;
    fetch(`/supervisor/restart/${processName}`, { method: 'POST' })
        .then(r => r.json())
        .then(data => { if (data.status === 'success') setTimeout(requestStatus, 2000); else alert(`Error: ${data.message}`); })
        .catch(() => alert('Failed to restart.'));
}

function pauseBot(processName) {
    fetch(`/supervisor/pause/${processName}`, { method: 'POST' })
        .then(r => r.json())
        .then(data => { if (data.status === 'success') setTimeout(requestStatus, 1000); else alert(`Error: ${data.message}`); });
}

function resumeBot(processName) {
    fetch(`/supervisor/resume/${processName}`, { method: 'POST' })
        .then(r => r.json())
        .then(data => { if (data.status === 'success') setTimeout(requestStatus, 1000); else alert(`Error: ${data.message}`); });
}

function clearFailure(processName) {
    fetch(`/supervisor/clear_failure/${processName}`, { method: 'POST' })
        .then(r => r.json())
        .then(data => { if (data.status === 'success') setTimeout(requestStatus, 1500); else alert(`Error: ${data.message}`); })
        .catch(() => alert('Failed to clear failure.'));
}

function viewLogs(processName) {
    fetch(`/supervisor/log/${processName}`)
        .then(r => { if (!r.ok) throw new Error(`HTTP ${r.status}`); return r.blob(); })
        .then(blob => {
            const url = URL.createObjectURL(blob);
            const a = Object.assign(document.createElement('a'), { href: url, download: `${processName}_log.txt`, style: 'display:none' });
            document.body.appendChild(a); a.click(); URL.revokeObjectURL(url); a.remove();
        })
        .catch(() => alert('Failed to fetch logs.'));
}

function removeBot(processName) {
    if (!confirm(`Permanently remove ${formatBotName(processName)}? This will stop the process and delete all its files.`)) return;
    fetch(`/api/bots/${processName}`, { method: 'DELETE' })
        .then(r => r.json())
        .then(data => {
            if (data.status === 'success') { setTimeout(requestStatus, 1500); }
            else alert(`Error: ${data.message}`);
        })
        .catch(() => alert('Failed to remove bot.'));
}

// ── Add Bot Modal ──────────────────────────────────────────────

function openAddBotModal() {
    document.getElementById('addbot-modal').style.display = 'block';
    document.getElementById('addbot-status').textContent = '';
    document.getElementById('env-pairs').innerHTML = '';
}

function closeAddBotModal() {
    document.getElementById('addbot-modal').style.display = 'none';
}

function addEnvPair(key = '', val = '') {
    const container = document.getElementById('env-pairs');
    const row = document.createElement('div');
    row.className = 'env-pair';
    row.innerHTML = `
        <input type="text" placeholder="KEY" value="${key}" class="env-key">
        <input type="text" placeholder="VALUE" value="${val}" class="env-val">
        <button onclick="this.parentElement.remove()">✕</button>
    `;
    container.appendChild(row);
}

function submitAddBot() {
    const btn = document.getElementById('addbot-submit-btn');
    const status = document.getElementById('addbot-status');

    const botname = document.getElementById('ab-name').value.trim();
    const git_url = document.getElementById('ab-git').value.trim();
    const branch = document.getElementById('ab-branch').value.trim();
    const run_command = document.getElementById('ab-run').value.trim();
    const python_version = document.getElementById('ab-pyver').value.trim() || null;
    const panel_port = parseInt(document.getElementById('ab-port').value) || null;
    const panel_label = document.getElementById('ab-label').value.trim() || botname;

    if (!botname || !git_url || !branch || !run_command) {
        status.style.color = '#f87171';
        status.textContent = 'Please fill in all required fields.';
        return;
    }

    const env = {};
    document.querySelectorAll('.env-pair').forEach(row => {
        const k = row.querySelector('.env-key').value.trim();
        const v = row.querySelector('.env-val').value.trim();
        if (k) env[k] = v;
    });

    btn.disabled = true;
    btn.textContent = 'Adding…';
    status.style.color = '#a78bfa';
    status.textContent = 'Cloning repo and setting up bot…';

    fetch('/api/bots', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ botname, git_url, branch, run_command, env, python_version, panel_port, panel_label })
    })
        .then(r => r.json())
        .then(data => {
            btn.disabled = false;
            btn.textContent = 'Add Bot';
            if (data.status === 'success') {
                status.style.color = '#4ade80';
                status.textContent = data.message + (data.panel_slug ? ` · Panel at /${data.panel_slug}` : '');
                setTimeout(() => { closeAddBotModal(); requestStatus(); }, 2000);
            } else {
                status.style.color = '#f87171';
                status.textContent = `Error: ${data.message}`;
            }
        })
        .catch(err => {
            btn.disabled = false;
            btn.textContent = 'Add Bot';
            status.style.color = '#f87171';
            status.textContent = `Request failed: ${err}`;
        });
}

// ── Cron ───────────────────────────────────────────────────────

function openCronModal() { document.getElementById('cron-modal').style.display = 'block'; }
function closeCronModal() { document.getElementById('cron-modal').style.display = 'none'; }

function loadCronSetting() {
    fetch('/config/cron').then(r => r.json()).then(data => {
        if (data.hours !== undefined) document.getElementById('cron-hours').value = data.hours;
    }).catch(() => {});
}

function saveCron() {
    const hours = parseInt(document.getElementById('cron-hours').value) || 0;
    fetch('/config/cron', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ hours })
    }).then(r => r.json()).then(data => {
        if (data.status === 'success') closeCronModal();
        else alert('Failed to save cron setting.');
    }).catch(() => alert('Failed to save cron setting.'));
}

// ── Visibility ──────────────────────────────────────────────────

document.addEventListener('visibilitychange', function () {
    if (document.hidden) { if (updateInterval) clearInterval(updateInterval); }
    else { requestStatus(); updateInterval = setInterval(requestStatus, 3000); }
});

window.onbeforeunload = function () {
    if (socket) socket.disconnect();
    if (updateInterval) clearInterval(updateInterval);
};
