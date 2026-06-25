import eventlet
eventlet.monkey_patch()

import os, json, signal, subprocess, re, shutil, socket as _socket
from datetime import datetime
from functools import wraps
from pathlib import Path
import logging, time, configparser
from collections import defaultdict

from app import app
from flask import (Flask, render_template, request, jsonify, Response,
                   send_file, redirect, url_for, session, flash, stream_with_context)
from flask_socketio import SocketIO, emit

try:
    import requests as _requests
except ImportError:
    _requests = None

try:
    import psutil as _psutil
except ImportError:
    _psutil = None

logging.basicConfig(level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s', filename='app.log')
logger = logging.getLogger(__name__)

# ════════════════════════════════════════════════════════════════
#  Metrics store  (in-memory, up to 24h of samples per bot)
# ════════════════════════════════════════════════════════════════
#  METRICS[bot_name] = deque of { ts, cpu, mem_mb }
#  UPTIME_HISTORY[bot_name] = list of { ts, event: 'start'|'stop'|'crash' }
#  RESTART_COUNTS[bot_name] = int
#  ALERT_CONFIG = { telegram_token, telegram_chat_id, discord_webhook }

from collections import deque

METRICS: dict        = defaultdict(lambda: deque(maxlen=1440))   # 1 sample/min → 24h
UPTIME_HISTORY: dict = defaultdict(list)    # event log, kept last 7 days of entries
RESTART_COUNTS: dict = defaultdict(int)
PREV_STATUSES: dict  = {}                   # track status transitions for alerting

METRICS_FILE       = Path('/app/metrics_history.json')
ALERT_CONFIG_FILE  = Path('/app/alert_config.json')
ALERT_CONFIG: dict = {}

def _load_metrics_history():
    """Load persisted uptime history and restart counts on startup."""
    if METRICS_FILE.exists():
        try:
            data = json.loads(METRICS_FILE.read_text())
            for k, v in data.get('uptime_history', {}).items():
                UPTIME_HISTORY[k] = v
            for k, v in data.get('restart_counts', {}).items():
                RESTART_COUNTS[k] = v
            logger.info("Loaded metrics history")
        except Exception as e:
            logger.error(f"metrics load error: {e}")

def _save_metrics_history():
    try:
        # Prune uptime history to last 7 days
        cutoff = (datetime.utcnow().timestamp() - 7 * 86400)
        for k in list(UPTIME_HISTORY.keys()):
            UPTIME_HISTORY[k] = [e for e in UPTIME_HISTORY[k] if e.get('ts', 0) > cutoff]
        METRICS_FILE.parent.mkdir(parents=True, exist_ok=True)
        METRICS_FILE.write_text(json.dumps({
            'uptime_history': dict(UPTIME_HISTORY),
            'restart_counts': dict(RESTART_COUNTS),
        }, indent=2))
    except Exception as e:
        logger.error(f"metrics save error: {e}")

def _load_alert_config():
    if ALERT_CONFIG_FILE.exists():
        try:
            ALERT_CONFIG.update(json.loads(ALERT_CONFIG_FILE.read_text()))
        except Exception as e:
            logger.error(f"alert config load error: {e}")

def _save_alert_config():
    try:
        ALERT_CONFIG_FILE.write_text(json.dumps(ALERT_CONFIG, indent=2))
    except Exception as e:
        logger.error(f"alert config save error: {e}")

_load_metrics_history()
_load_alert_config()


# ── Alert dispatcher ─────────────────────────────────────────────

def _send_alert(bot_name: str, event: str, detail: str = ''):
    """Fire crash/recovery alerts to configured channels."""
    msg = f"🤖 *BotClusters Alert*\n\nBot: `{bot_name}`\nEvent: *{event}*"
    if detail:
        msg += f"\nDetail: {detail}"
    msg += f"\nTime: {datetime.utcnow().strftime('%Y-%m-%d %H:%M:%S')} UTC"

    # Telegram
    tg_token = ALERT_CONFIG.get('telegram_token','').strip()
    tg_chat  = ALERT_CONFIG.get('telegram_chat_id','').strip()
    if tg_token and tg_chat and _requests:
        try:
            _requests.post(
                f"https://api.telegram.org/bot{tg_token}/sendMessage",
                json={'chat_id': tg_chat, 'text': msg, 'parse_mode': 'Markdown'},
                timeout=8)
            logger.info(f"Telegram alert sent for {bot_name}: {event}")
        except Exception as e:
            logger.error(f"Telegram alert failed: {e}")

    # Discord
    dw = ALERT_CONFIG.get('discord_webhook','').strip()
    if dw and _requests:
        try:
            color = 0xe74c3c if 'crash' in event.lower() or 'fatal' in event.lower() else 0x2ecc71
            _requests.post(dw, json={'embeds': [{
                'title': f'BotClusters — {event}',
                'description': f'**Bot:** {bot_name}\n{detail}',
                'color': color,
                'footer': {'text': datetime.utcnow().strftime('%Y-%m-%d %H:%M:%S UTC')}
            }]}, timeout=8)
            logger.info(f"Discord alert sent for {bot_name}: {event}")
        except Exception as e:
            logger.error(f"Discord alert failed: {e}")


# ── CPU/RAM sampler (runs every 60 s) ────────────────────────────

def _get_process_metrics(pid: int) -> dict:
    """Read CPU % and RSS memory for a PID using psutil."""
    if not _psutil or not pid:
        return {'cpu': 0.0, 'mem_mb': 0.0}
    try:
        proc = _psutil.Process(pid)
        cpu  = proc.cpu_percent(interval=0.5)
        mem  = proc.memory_info().rss / (1024 * 1024)
        return {'cpu': round(cpu, 1), 'mem_mb': round(mem, 1)}
    except (_psutil.NoSuchProcess, _psutil.AccessDenied):
        return {'cpu': 0.0, 'mem_mb': 0.0}


def _metrics_loop():
    """Background loop: sample all running bots every 60 s."""
    eventlet.sleep(10)   # let supervisor settle on startup
    while True:
        try:
            status = run_supervisor_command("status")
            if status["status"] == "success":
                now = datetime.utcnow().timestamp()
                for line in status["message"].splitlines():
                    p = parse_supervisor_status(line)
                    if not p:
                        continue
                    name = p["name"]
                    prev = PREV_STATUSES.get(name)

                    # ── Detect transitions ──────────────────────────
                    cur_status = p["status"]
                    if prev is not None and prev != cur_status:
                        ts = now
                        if cur_status == "RUNNING" and prev in ("STOPPED","EXITED","FATAL","STARTING"):
                            UPTIME_HISTORY[name].append({'ts': ts, 'event': 'start'})
                            if prev in ("EXITED","FATAL","BACKOFF"):
                                RESTART_COUNTS[name] += 1
                                _send_alert(name, "Restarted after crash", f"Previous status: {prev}")
                            else:
                                _send_alert(name, "Started", "")
                        elif cur_status in ("FATAL","BACKOFF") and prev == "RUNNING":
                            UPTIME_HISTORY[name].append({'ts': ts, 'event': 'crash'})
                            _send_alert(name, "🚨 CRASHED", f"Status: {cur_status}")
                        elif cur_status in ("STOPPED","EXITED") and prev == "RUNNING":
                            UPTIME_HISTORY[name].append({'ts': ts, 'event': 'stop'})
                    elif prev is None and cur_status == "RUNNING":
                        # First time seeing this bot as running
                        UPTIME_HISTORY[name].append({'ts': now, 'event': 'start'})

                    PREV_STATUSES[name] = cur_status

                    # ── Sample CPU/RAM ──────────────────────────────
                    if cur_status == "RUNNING" and p.get("pid"):
                        try:
                            pid = int(p["pid"])
                        except (ValueError, TypeError):
                            pid = None
                        if pid:
                            m = _get_process_metrics(pid)
                            METRICS[name].append({'ts': now, **m})

                _save_metrics_history()
        except Exception as e:
            logger.error(f"metrics loop error: {e}")
        eventlet.sleep(60)

app.config['SECRET_KEY'] = os.environ.get('SECRET_KEY', os.urandom(24))
socketio = SocketIO(app, async_mode='eventlet', cors_allowed_origins="*",
                    ping_timeout=60, ping_interval=25)

SUPERVISOR_LOG_DIR   = "/var/log/supervisor"
SUPERVISORD_CONF_DIR = "/etc/supervisor/conf.d"
STATUS_CHECK_INTERVAL    = 2
MAX_STATUS_CHECK_ATTEMPTS = 10
TEMP_SUPERVISOR_CONFIGS  = {}
FAILURE_COUNTS  = defaultdict(int)
MAX_FAILURES_BEFORE_PAUSE = 5
PAUSED_BY_SYSTEM = set()
CRON_RESTART_INTERVAL = int(os.environ.get('CRON_RESTART_HOURS', 0))
_cron_thread = None
DOCKER_REGISTRY: dict = {}

# ── In-memory bot registry (survives without env vars) ─────────
# { safe_name: { name, git_url, branch, run_command, env, python_version, deploy_type, web_port, ... } }
BOT_REGISTRY: dict = {}
BOT_REGISTRY_FILE = Path('/app/bot_registry.json')

# ════════════════════════════════════════════════════════════════
#  Runtime control globals
# ════════════════════════════════════════════════════════════════

# Live stdin injection — { safe_name: subprocess.Popen } for git bots whose
# stdin we keep open so we can write to them.
STDIN_PIPES: dict = {}

# Health check config — persisted in /app/health_config.json
# { safe_name: { url, interval_sec, timeout_sec, enabled } }
HEALTH_CONFIG: dict = {}
HEALTH_CONFIG_FILE = Path('/app/health_config.json')
# Health check state — { safe_name: { last_ok, last_check, consecutive_failures } }
HEALTH_STATE: dict = defaultdict(lambda: {'last_ok': None, 'last_check': None,
                                           'consecutive_failures': 0})
HEALTH_MAX_FAILURES = 3   # failures before auto-restart

# Restart rate limiter — { safe_name: deque of timestamps }
# Config stored in BOT_REGISTRY per bot as 'rate_limit': { max_restarts, window_sec }
RESTART_TIMESTAMPS: dict = defaultdict(lambda: deque(maxlen=50))

# ════════════════════════════════════════════════════════════════
#  Deployment feature globals
# ════════════════════════════════════════════════════════════════

# Git commit history: { safe_name: [ {sha, msg, ts, author}, ... ] } — last 10 per bot
COMMIT_HISTORY: dict  = defaultdict(list)
COMMIT_HISTORY_FILE   = Path('/app/commit_history.json')

# Webhook secrets: { safe_name: secret_token }
WEBHOOK_SECRETS: dict = {}
WEBHOOK_SECRETS_FILE  = Path('/app/webhook_secrets.json')

# Scheduled deployments: { safe_name: { cron_expr, next_run_ts, enabled } }
SCHEDULED_DEPLOYS: dict = {}
SCHEDULED_FILE         = Path('/app/scheduled_deploys.json')
_sched_thread          = None

# Upload staging dir for ZIP deploys
UPLOAD_STAGING = Path('/app/_uploads')
UPLOAD_STAGING.mkdir(parents=True, exist_ok=True)

def _load_deployment_state():
    for path, store in [
        (COMMIT_HISTORY_FILE, COMMIT_HISTORY),
        (WEBHOOK_SECRETS_FILE, WEBHOOK_SECRETS),
        (SCHEDULED_FILE,       SCHEDULED_DEPLOYS),
    ]:
        if path.exists():
            try:
                store.update(json.loads(path.read_text()))
            except Exception as e:
                logger.error(f"load {path.name}: {e}")

def _save_commit_history():
    try:
        COMMIT_HISTORY_FILE.write_text(json.dumps(dict(COMMIT_HISTORY), indent=2))
    except Exception as e:
        logger.error(f"save commit history: {e}")

def _save_webhook_secrets():
    try:
        WEBHOOK_SECRETS_FILE.write_text(json.dumps(WEBHOOK_SECRETS, indent=2))
    except Exception as e:
        logger.error(f"save webhook secrets: {e}")

def _save_scheduled():
    try:
        SCHEDULED_FILE.write_text(json.dumps(SCHEDULED_DEPLOYS, indent=2))
    except Exception as e:
        logger.error(f"save scheduled: {e}")

_load_deployment_state()


def _snapshot_commits(safe: str, bot_dir: Path, keep: int = 10):
    """Read last `keep` git commits and store in COMMIT_HISTORY."""
    try:
        result = subprocess.run(
            ['git', 'log', f'--max-count={keep}',
             '--pretty=format:%H|||%s|||%ai|||%an'],
            cwd=str(bot_dir), capture_output=True, text=True, timeout=10)
        entries = []
        for line in result.stdout.strip().splitlines():
            parts = line.split('|||')
            if len(parts) == 4:
                entries.append({'sha': parts[0], 'msg': parts[1],
                                'ts': parts[2], 'author': parts[3]})
        COMMIT_HISTORY[safe] = entries
        _save_commit_history()
    except Exception as e:
        logger.error(f"snapshot_commits {safe}: {e}")


def _redeploy_git_bot(process_name: str, bot_dir: Path, breg: dict) -> dict:
    """
    Re-install deps and restart a git bot in-place.
    Returns { status, message }.
    """
    safe       = process_name.replace(' ', '_')
    python_ver = breg.get('python_version', '')
    run_cmd    = breg.get('run_command', '')
    env_vars   = breg.get('env', {})

    try:
        python_exec = shutil.which(f"python{python_ver}") or shutil.which("python3") or "python3"
        venv_dir    = bot_dir / 'venv'
        req_file    = bot_dir / 'requirements.txt'

        if req_file.exists():
            if not venv_dir.exists():
                subprocess.run([python_exec, '-m', 'venv', str(venv_dir)], check=True)
            subprocess.run([str(venv_dir/'bin'/'pip'), 'install', '--no-cache-dir',
                            '-r', str(req_file)], check=True)

        bot_file = bot_dir / run_cmd
        py       = venv_dir / 'bin' / 'python3'
        if bot_file.suffix == '.sh':
            command = f"bash {bot_file}"
        elif bot_file.suffix == '.py':
            command = f"{py} {bot_file}"
        else:
            command = f"{py} -m {bot_file.stem}"

        write_git_supervisord_config(process_name, command, str(bot_dir), env_vars)
        supervisord_reload()

        run_supervisor_command("stop", process_name)
        time.sleep(1)
        run_supervisor_command("start", process_name)
        _record_restart(process_name)
        broadcast_status_update()
        return {"status": "success", "message": f"Redeployed {process_name}"}
    except Exception as e:
        return {"status": "error", "message": str(e)}

def _load_health_config():
    if HEALTH_CONFIG_FILE.exists():
        try:
            HEALTH_CONFIG.update(json.loads(HEALTH_CONFIG_FILE.read_text()))
        except Exception as e:
            logger.error(f"health config load: {e}")

def _save_health_config():
    try:
        HEALTH_CONFIG_FILE.write_text(json.dumps(HEALTH_CONFIG, indent=2))
    except Exception as e:
        logger.error(f"health config save: {e}")

_load_health_config()

def _load_bot_registry():
    if BOT_REGISTRY_FILE.exists():
        try:
            data = json.loads(BOT_REGISTRY_FILE.read_text())
            BOT_REGISTRY.update(data)
            logger.info(f"Loaded {len(BOT_REGISTRY)} bots from registry")
        except Exception as e:
            logger.error(f"Failed to load bot registry: {e}")

def _save_bot_registry():
    try:
        BOT_REGISTRY_FILE.parent.mkdir(parents=True, exist_ok=True)
        BOT_REGISTRY_FILE.write_text(json.dumps(BOT_REGISTRY, indent=2))
    except Exception as e:
        logger.error(f"Failed to save bot registry: {e}")

_load_bot_registry()

# ════════════════════════════════════════════════════════════════
#  Utility helpers
# ════════════════════════════════════════════════════════════════

def _bot_number_from_name(name: str):
    m = re.search(r'bot(\d+)$', name, re.IGNORECASE)
    return m.group(1) if m else None

def _find_free_port(start=9100):
    port = start
    while port < 65000:
        with _socket.socket(_socket.AF_INET, _socket.SOCK_STREAM) as s:
            if s.connect_ex(('localhost', port)) != 0:
                return port
        port += 1
    raise RuntimeError("No free port")

def parse_supervisor_status(line):
    try:
        parts = line.strip().split()
        if len(parts) < 2:
            return None
        name   = parts[0]
        status = parts[1]
        pid_m    = re.search(r'pid (\d+)', line)
        uptime_m = re.search(r'uptime ([\d:]+)', line)
        pid = pid_m.group(1) if pid_m else None
        paused = bool(pid and is_process_paused(pid))
        safe = name.replace(' ', '_')
        dreg = DOCKER_REGISTRY.get(safe) or DOCKER_REGISTRY.get(name, {})
        breg = BOT_REGISTRY.get(safe) or BOT_REGISTRY.get(name, {})
        return {
            "name":       name,
            "status":     status,
            "pid":        pid,
            "uptime":     uptime_m.group(1) if uptime_m else "0:00:00",
            "paused":     paused,
            "web_port":   dreg.get('web_port'),
            "is_docker":  bool(dreg),
            "deploy_type": breg.get('deploy_type', 'docker' if dreg else 'git'),
            "git_url":    breg.get('git_url', ''),
            "branch":     breg.get('branch', ''),
        }
    except Exception as e:
        logger.error(f"parse error: {e}")
    return None

def is_process_paused(pid):
    try:
        with open(f"/proc/{pid}/status") as f:
            for line in f:
                if line.startswith("State:") and "\tT" in line:
                    return True
    except Exception:
        pass
    return False

def run_supervisor_command(command, process_name=None, timeout=30):
    try:
        cmd = ["supervisorctl", command]
        if process_name:
            cmd.append(process_name)
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        if result.returncode == 0:
            return {"status": "success", "message": result.stdout.strip()}
        return {"status": "error", "message": result.stderr.strip() or result.stdout.strip()}
    except subprocess.TimeoutExpired:
        return {"status": "error", "message": "Command timed out"}
    except Exception as e:
        return {"status": "error", "message": str(e)}

def verify_process_status(process_name):
    r = run_supervisor_command("status", process_name)
    return r["message"] if r["status"] == "success" else None

def broadcast_status_update():
    try:
        with app.app_context():
            status = run_supervisor_command("status")
            if status["status"] != "success":
                return False
            processes = []
            for line in status["message"].splitlines():
                p = parse_supervisor_status(line)
                if not p:
                    continue
                pname = p["name"]
                if p["status"] in ("FATAL","BACKOFF","EXITED"):
                    FAILURE_COUNTS[pname] += 1
                    if FAILURE_COUNTS[pname] >= MAX_FAILURES_BEFORE_PAUSE:
                        PAUSED_BY_SYSTEM.add(pname)
                    p["auto_paused"] = pname in PAUSED_BY_SYSTEM
                else:
                    if p["status"] == "RUNNING":
                        FAILURE_COUNTS[pname] = 0
                        PAUSED_BY_SYSTEM.discard(pname)
                    p["auto_paused"] = pname in PAUSED_BY_SYSTEM
                processes.append(p)
            # Attach latest metrics snapshot to each process
            for p in processes:
                pname = p["name"]
                safe  = pname.replace(' ', '_')
                dq = METRICS.get(pname)
                if dq:
                    latest = dq[-1]
                    p["cpu"]      = latest.get("cpu", 0.0)
                    p["mem_mb"]   = latest.get("mem_mb", 0.0)
                else:
                    p["cpu"]    = 0.0
                    p["mem_mb"] = 0.0
                p["restart_count"] = RESTART_COUNTS.get(pname, 0)

                # Rate limit info
                breg = BOT_REGISTRY.get(safe, {})
                rl   = breg.get('rate_limit')
                if rl:
                    limited, remaining, reset_in = _is_restart_rate_limited(pname)
                    p["rate_limit"] = {"max": rl["max_restarts"], "window": rl["window_sec"],
                                       "remaining": remaining, "limited": limited}

                # Health check state
                hcfg = HEALTH_CONFIG.get(safe, {})
                if hcfg.get('enabled'):
                    hs = HEALTH_STATE.get(safe, {})
                    p["health"] = {
                        "url":      hcfg.get('url',''),
                        "ok":       hs.get('consecutive_failures', 0) == 0 and hs.get('last_ok') is not None,
                        "failures": hs.get('consecutive_failures', 0),
                        "last_ok":  hs.get('last_ok'),
                    }

                # Deployment extras
                p["has_webhook"]  = bool(WEBHOOK_SECRETS.get(safe))
                sched = SCHEDULED_DEPLOYS.get(safe, {})
                p["scheduled"]    = sched.get('enabled', False)
                p["deploy_type"]  = breg.get('deploy_type', 'git')
                p["commit_count"] = len(COMMIT_HISTORY.get(safe, []))

            socketio.emit('status_update', {
                "status": "success", "processes": processes,
                "timestamp": datetime.utcnow().isoformat()
            }, broadcast=True)
            return True
    except Exception as e:
        logger.error(f"broadcast error: {e}")
        return False

def write_git_supervisord_config(process_name, command, bot_dir, env_vars=None):
    safe = process_name.replace(' ', '_')
    config_path = Path(SUPERVISORD_CONF_DIR) / f"{safe}.conf"
    env_str = ','.join(f'{k}="{v}"' for k, v in (env_vars or {}).items())
    content = f"""[program:{safe}]
command={command}
directory={bot_dir}
autostart=true
autorestart=true
startretries=12
stderr_logfile={SUPERVISOR_LOG_DIR}/{safe}_err.log
stdout_logfile={SUPERVISOR_LOG_DIR}/{safe}_out.log
{"environment="+env_str if env_str else ""}
""".strip()
    config_path.write_text(content)

def write_docker_supervisord_config(process_name, container_name):
    safe = process_name.replace(' ', '_')
    config_path = Path(SUPERVISORD_CONF_DIR) / f"{safe}.conf"
    content = f"""[program:{safe}]
command=docker start -a {container_name}
autostart=true
autorestart=true
startretries=12
stderr_logfile={SUPERVISOR_LOG_DIR}/{safe}_err.log
stdout_logfile={SUPERVISOR_LOG_DIR}/{safe}_out.log
""".strip()
    config_path.write_text(content)

def supervisord_reload():
    subprocess.run(["supervisorctl","reread"], capture_output=True)
    subprocess.run(["supervisorctl","update"],  capture_output=True)

def delete_supervisor_logs(process_name):
    safe = process_name.replace(' ', '_')
    for s in ('_out.log','_err.log','_combined.log'):
        f = Path(SUPERVISOR_LOG_DIR) / f"{safe}{s}"
        if f.exists():
            f.unlink()

def thoroughly_cleanup(process_name):
    subprocess.run(f"pkill -f {process_name}", shell=True, capture_output=True)
    config_path = Path(SUPERVISORD_CONF_DIR) / f"{process_name.replace(' ','_')}.conf"
    if config_path.exists():
        config = configparser.ConfigParser()
        config.read(config_path)
        section = 'program:' + process_name
        if section in config:
            directory = config[section].get('directory','')
            if directory and Path(directory).exists():
                for root, dirs, files in os.walk(directory):
                    for d in dirs:
                        if d == '__pycache__':
                            pd = Path(root)/d
                            for f in pd.glob('*.pyc'): f.unlink()
                            try: pd.rmdir()
                            except: pass
                    for f in files:
                        if f.endswith('.pyc'):
                            (Path(root)/f).unlink()

# ════════════════════════════════════════════════════════════════
#  Auth
# ════════════════════════════════════════════════════════════════

USERS = {
    os.environ.get('ADMIN_USER','admin'): os.environ.get('ADMIN_PASS','password123')
}

def login_required(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        if 'logged_in' not in session:
            return redirect(url_for('login'))
        return f(*args, **kwargs)
    return decorated

@app.route('/login', methods=['GET','POST'])
def login():
    if request.method == 'POST':
        u = request.form.get('username','')
        p = request.form.get('password','')
        if USERS.get(u) == p:
            session['logged_in'] = True
            return redirect(url_for('cluster'))
        flash('Invalid credentials.')
    return render_template('login.html')

@app.route('/logout')
def logout():
    session.pop('logged_in', None)
    return redirect(url_for('login'))

@app.route('/')
@login_required
def cluster():
    return render_template('cluster.html')

# ════════════════════════════════════════════════════════════════
#  Add / Deploy bot (Git or Dockerfile) — no env-var config needed
# ════════════════════════════════════════════════════════════════

def _docker_available() -> bool:
    """
    Check if a Docker-compatible CLI is available.
    Accepts either 'docker' (real Docker or our podman shim) or 'podman' directly.
    """
    import shutil
    return shutil.which('docker') is not None or shutil.which('podman') is not None


def _docker_cmd() -> str:
    """Return the docker-compatible command to use ('docker' shim or 'podman')."""
    import shutil
    if shutil.which('docker'):
        return 'docker'
    if shutil.which('podman'):
        return 'podman'
    return 'docker'  # will fail gracefully

def _docker_required(f):
    """Decorator: return 503 if Docker CLI is not available."""
    @wraps(f)
    def decorated(*args, **kwargs):
        if not _docker_available():
            return jsonify({
                "status": "error",
                "message": "No container runtime found (docker/podman). "
                           "The BotClusters image includes Podman — if you see this error "
                           "the container may need --privileged or --device /dev/fuse. "
                           "Git and ZIP bots work without any container runtime."
            }), 503
        return f(*args, **kwargs)
    return decorated


@app.route('/bot/add', methods=['POST'])
@login_required
def bot_add():
    """
    Deploy a new bot from the UI.
    Body: { process_name, git_url, branch, deploy_type (git|docker),
            run_command (git only), python_version (git only),
            web_port (docker only), env, build_args (docker only) }
    """
    data = request.get_json(silent=True) or {}
    process_name = data.get('process_name','').strip()
    git_url      = data.get('git_url','').strip()
    branch       = data.get('branch','main').strip()
    deploy_type  = data.get('deploy_type','git').strip()
    run_command  = data.get('run_command','').strip()
    python_ver   = data.get('python_version','').strip()
    web_port     = data.get('web_port')
    env_vars     = data.get('env', {})
    build_args   = data.get('build_args', {})

    if not process_name or not git_url:
        return jsonify({"status":"error","message":"process_name and git_url required"}), 400
    if not re.match(r'^[a-zA-Z0-9_ \-]+$', process_name):
        return jsonify({"status":"error","message":"Invalid characters in process_name"}), 400
    if not _bot_number_from_name(process_name):
        return jsonify({"status":"error","message":"process_name must end with botN (e.g. 'my bot1')"}), 400

    safe = process_name.replace(' ','_')
    clone_dir = Path('/app') / safe

    try:
        if clone_dir.exists():
            shutil.rmtree(clone_dir)
        subprocess.run(['git','clone','-b',branch,'--single-branch', git_url, str(clone_dir)], check=True)

        if deploy_type == 'docker':
            if not _docker_available():
                return jsonify({
                    "status": "error",
                    "message": "Docker is not available on this host. Use Git or ZIP deployment instead."
                }), 503
            # Build & create container
            container_name = f"botcluster_{safe}"
            build_cmd = [_docker_cmd(),'build','-t', container_name, str(clone_dir)]
            for k,v in build_args.items():
                build_cmd += ['--build-arg', f'{k}={v}']
            subprocess.run(build_cmd, check=True)

            host_port = None
            if web_port:
                host_port = _find_free_port(9100)

            subprocess.run([_docker_cmd(),'rm','-f', container_name], capture_output=True)
            run_cmd = [_docker_cmd(),'create','--name', container_name,'--restart','no']
            for k,v in env_vars.items():
                run_cmd += ['-e', f'{k}={v}']
            if host_port and web_port:
                run_cmd += ['-p', f'{host_port}:{web_port}']
            run_cmd.append(container_name)
            subprocess.run(run_cmd, check=True)

            DOCKER_REGISTRY[safe] = {'container_name': container_name, 'web_port': host_port,
                                      'internal_port': web_port, 'process_name': process_name}
            write_docker_supervisord_config(process_name, container_name)

        else:
            # Git / Python bot
            venv_dir = clone_dir / 'venv'
            req_file = clone_dir / 'requirements.txt'

            python_exec = shutil.which(f"python{python_ver}") or shutil.which("python3") or "python3"
            if req_file.exists():
                subprocess.run([python_exec,'-m','venv', str(venv_dir)], check=True)
                subprocess.run([str(venv_dir/'bin'/'pip'),'install','--no-cache-dir','-r',str(req_file)], check=True)

            bot_file = clone_dir / run_command
            py = venv_dir/'bin'/'python3'
            if bot_file.suffix == '.sh':
                command = f"bash {bot_file}"
            elif bot_file.suffix == '.py':
                command = f"{py} {bot_file}"
            else:
                command = f"{py} -m {bot_file.stem}"

            write_git_supervisord_config(process_name, command, str(clone_dir), env_vars)

        # Save to persistent registry
        BOT_REGISTRY[safe] = {
            'name': process_name, 'safe': safe, 'git_url': git_url,
            'branch': branch, 'deploy_type': deploy_type,
            'run_command': run_command, 'python_version': python_ver,
            'web_port': web_port, 'env': env_vars,
            'added_at': datetime.utcnow().isoformat()
        }
        _save_bot_registry()

        # Snapshot commit history for rollback support
        if deploy_type == 'git':
            _snapshot_commits(safe, clone_dir)
        supervisord_reload()
        broadcast_status_update()

        bot_num = _bot_number_from_name(process_name)
        return jsonify({
            "status": "success",
            "message": f"Bot '{process_name}' deployed successfully",
            "web_proxy": f"/bot{bot_num}" if web_port and deploy_type == 'docker' else None
        }), 200

    except subprocess.CalledProcessError as e:
        return jsonify({"status":"error","message":str(e)}), 500
    except Exception as e:
        logger.error(f"bot_add error: {e}")
        return jsonify({"status":"error","message":str(e)}), 500


@app.route('/bot/delete/<path:process_name>', methods=['DELETE'])
@login_required
def bot_delete(process_name):
    """Fully remove a bot: stop it, remove supervisord config, clean files."""
    safe = process_name.replace(' ','_')
    try:
        # Stop via supervisor
        run_supervisor_command("stop", process_name)
        time.sleep(1)

        # Remove Docker container if applicable
        dreg = DOCKER_REGISTRY.pop(safe, None)
        if dreg and _docker_available():
            subprocess.run([_docker_cmd(),'rm','-f', dreg['container_name']], capture_output=True)

        # Remove supervisord config
        conf = Path(SUPERVISORD_CONF_DIR) / f"{safe}.conf"
        if conf.exists():
            conf.unlink()
        supervisord_reload()

        # Remove bot directory
        bot_dir = Path('/app') / safe
        if bot_dir.exists():
            shutil.rmtree(bot_dir)

        # Remove from registry
        BOT_REGISTRY.pop(safe, None)
        BOT_REGISTRY.pop(process_name, None)
        _save_bot_registry()

        # Remove logs
        delete_supervisor_logs(process_name)
        broadcast_status_update()
        return jsonify({"status":"success","message":f"Bot '{process_name}' deleted"}), 200
    except Exception as e:
        logger.error(f"bot_delete error: {e}")
        return jsonify({"status":"error","message":str(e)}), 500


# ════════════════════════════════════════════════════════════════
#  Export / Import bot registry
# ════════════════════════════════════════════════════════════════

@app.route('/bot/export', methods=['GET'])
@login_required
def bot_export():
    """Export all bot configs + docker registry as a JSON file."""
    payload = {
        "version": "2",
        "exported_at": datetime.utcnow().isoformat(),
        "bots": BOT_REGISTRY,
        "docker_registry": DOCKER_REGISTRY,
    }
    resp = Response(json.dumps(payload, indent=2), mimetype='application/json')
    resp.headers['Content-Disposition'] = 'attachment; filename=botclusters_export.json'
    return resp


@app.route('/bot/import', methods=['POST'])
@login_required
def bot_import():
    """
    Import bot configs. Does NOT re-deploy — just registers them so they
    can be re-deployed with one click. Pass JSON body or multipart file.
    """
    try:
        if request.content_type and 'multipart' in request.content_type:
            f = request.files.get('file')
            if not f:
                return jsonify({"status":"error","message":"No file uploaded"}), 400
            data = json.loads(f.read())
        else:
            data = request.get_json(silent=True) or {}

        bots = data.get('bots', {})
        imported = 0
        for safe, info in bots.items():
            if safe not in BOT_REGISTRY:
                BOT_REGISTRY[safe] = info
                imported += 1
        _save_bot_registry()
        return jsonify({"status":"success","message":f"Imported {imported} bots","total": len(BOT_REGISTRY)}), 200
    except Exception as e:
        return jsonify({"status":"error","message":str(e)}), 500


@app.route('/bot/registry', methods=['GET'])
@login_required
def bot_registry_list():
    return jsonify({"status":"success","bots": BOT_REGISTRY}), 200


# ════════════════════════════════════════════════════════════════
#  Supervisor control endpoints
# ════════════════════════════════════════════════════════════════

@app.route('/supervisor/status', methods=['GET'])
def list_supervisor_processes():
    status = run_supervisor_command("status")
    if status["status"] == "success":
        processes = [p for line in status["message"].splitlines()
                     if (p := parse_supervisor_status(line))]
        return jsonify({"status":"success","processes":processes}), 200
    return jsonify(status), 500


@app.route('/supervisor/pause/<path:process_name>', methods=['POST'])
@login_required
def pause_supervisor_process(process_name):
    result = _pause_process(process_name)
    if result["status"] == "success":
        broadcast_status_update()
        return jsonify(result), 200
    return jsonify(result), 500


@app.route('/supervisor/resume/<path:process_name>', methods=['POST'])
@login_required
def resume_supervisor_process(process_name):
    result = _resume_process(process_name)
    if result["status"] == "success":
        broadcast_status_update()
        return jsonify(result), 200
    return jsonify(result), 500


def _pause_process(process_name):
    r = run_supervisor_command("status", process_name)
    if r["status"] == "success":
        p = parse_supervisor_status(r["message"])
        if p and p["pid"]:
            try:
                os.kill(int(p["pid"]), signal.SIGSTOP)
                return {"status":"success","message":f"Paused {process_name}"}
            except Exception as e:
                return {"status":"error","message":str(e)}
    return {"status":"error","message":"Process not running or PID not found"}


def _resume_process(process_name):
    r = run_supervisor_command("status", process_name)
    if r["status"] == "success":
        p = parse_supervisor_status(r["message"])
        if p and p["pid"]:
            try:
                os.kill(int(p["pid"]), signal.SIGCONT)
                return {"status":"success","message":f"Resumed {process_name}"}
            except Exception as e:
                return {"status":"error","message":str(e)}
    return {"status":"error","message":"Process not running or PID not found"}


@app.route('/supervisor/<action>/<path:process_name>', methods=['POST'])
@login_required
def manage_supervisor_process(action, process_name):
    if action not in ["start","stop","restart"]:
        return jsonify({"status":"error","message":"Invalid action"}), 400
    if not re.match(r'^[a-zA-Z0-9_\- ]+$', process_name):
        return jsonify({"status":"error","message":"Invalid process name"}), 400

    try:
        initial_status = verify_process_status(process_name)
        if initial_status is None:
            return jsonify({"status":"error","message":f"Process {process_name} not found"}), 404

        config_path = Path(SUPERVISORD_CONF_DIR) / f"{process_name.replace(' ','_')}.conf"

        if action == "stop":
            if "RUNNING" not in initial_status:
                return jsonify({"status":"error","message":"Process not running"}), 400
            result = run_supervisor_command("stop", process_name)
            if result["status"] == "success" and config_path.exists():
                TEMP_SUPERVISOR_CONFIGS[process_name] = config_path.read_text()
                config_path.unlink()
                supervisord_reload()
            safe = process_name.replace(' ','_')
            if safe in DOCKER_REGISTRY:
                subprocess.run([_docker_cmd(),'stop', DOCKER_REGISTRY[safe]['container_name']], capture_output=True)
            expected_status = "STOPPED"

        elif action == "start":
            # Rate-limit check
            limited, remaining, reset_in = _is_restart_rate_limited(process_name)
            if limited:
                return jsonify({"status": "error",
                                "message": f"Rate limit hit — max restarts reached. Resets in {reset_in}s."}), 429
            if process_name in TEMP_SUPERVISOR_CONFIGS:
                config_path.write_text(TEMP_SUPERVISOR_CONFIGS.pop(process_name))
                supervisord_reload()
            result = run_supervisor_command("start", process_name)
            _record_restart(process_name)
            expected_status = "RUNNING"

        elif action == "restart":
            # Rate-limit check
            limited, remaining, reset_in = _is_restart_rate_limited(process_name)
            if limited:
                return jsonify({"status": "error",
                                "message": f"Rate limit hit — max restarts reached. Resets in {reset_in}s."}), 429
            thoroughly_cleanup(process_name)
            delete_supervisor_logs(process_name)
            if config_path.exists():
                cfg = config_path.read_text()
                run_supervisor_command("stop", process_name)
                config_path.unlink()
                supervisord_reload()
                time.sleep(2)

                # Pull latest code before restarting
                safe = process_name.replace(' ','_')
                bot_dir = Path('/app') / safe
                if bot_dir.exists() and (bot_dir/'.git').exists():
                    subprocess.run(['git','pull'], cwd=str(bot_dir), capture_output=True)

                config_path.write_text(cfg)
                supervisord_reload()
                result = run_supervisor_command("start", process_name)
                _record_restart(process_name)
                expected_status = "RUNNING"
            else:
                return jsonify({"status":"error","message":"Config not found"}), 404

        if result["status"] != "success":
            return jsonify(result), 500

        for _ in range(MAX_STATUS_CHECK_ATTEMPTS):
            time.sleep(STATUS_CHECK_INTERVAL)
            cur = verify_process_status(process_name)
            if action == "stop" and cur is None:
                broadcast_status_update()
                return jsonify({"status":"success","message":f"Stopped {process_name}"}), 200
            if cur and expected_status in cur:
                broadcast_status_update()
                return jsonify({"status":"success","message":f"{action.capitalize()}ed {process_name}"}), 200

        return jsonify({"status":"error","message":f"Process did not reach {expected_status}"}), 500

    except Exception as e:
        logger.error(f"manage error: {e}")
        return jsonify({"status":"error","message":str(e)}), 500


@app.route('/supervisor/clear_failure/<path:process_name>', methods=['POST'])
@login_required
def clear_failure(process_name):
    FAILURE_COUNTS[process_name] = 0
    PAUSED_BY_SYSTEM.discard(process_name)
    run_supervisor_command("start", process_name)
    broadcast_status_update()
    return jsonify({"status":"success","message":f"Cleared failure for {process_name}"}), 200


@app.route('/supervisor/log/<path:process_name>', methods=['GET'])
@login_required
def download_supervisor_log(process_name):
    safe = process_name.replace(' ','_')
    stdout_log  = Path(SUPERVISOR_LOG_DIR) / f"{safe}_out.log"
    stderr_log  = Path(SUPERVISOR_LOG_DIR) / f"{safe}_err.log"
    combined    = Path(SUPERVISOR_LOG_DIR) / f"{safe}_combined.log"
    if stdout_log.exists() or stderr_log.exists():
        with combined.open('w') as out:
            out.write(f"=== Logs for {process_name} — {datetime.utcnow().isoformat()} ===\n\n")
            if stdout_log.exists():
                out.write("=== STDOUT ===\n")
                out.write(stdout_log.read_text(errors='replace'))
                out.write("\n\n")
            if stderr_log.exists():
                out.write("=== STDERR ===\n")
                out.write(stderr_log.read_text(errors='replace'))
        return send_file(str(combined), mimetype='text/plain', as_attachment=True,
                         download_name=f"{safe}_log.txt")
    return jsonify({"status":"error","message":"No logs found"}), 404


# ── Log tail endpoint (last N lines via SSE per-bot) ────────────
@app.route('/supervisor/logtail/<path:process_name>')
@login_required
def log_tail(process_name):
    safe = process_name.replace(' ','_')
    lines = int(request.args.get('lines', 200))
    def generate():
        for suffix in ('_out.log','_err.log'):
            lf = Path(SUPERVISOR_LOG_DIR) / f"{safe}{suffix}"
            if lf.exists():
                try:
                    content = lf.read_text(errors='replace')
                    tail = '\n'.join(content.splitlines()[-lines:])
                    yield f"data: {json.dumps({'file': lf.name, 'data': tail})}\n\n"
                except Exception:
                    pass
        pos = {}
        while True:
            for suffix in ('_out.log','_err.log'):
                lf = Path(SUPERVISOR_LOG_DIR) / f"{safe}{suffix}"
                if not lf.exists():
                    continue
                try:
                    p = pos.get(lf.name, lf.stat().st_size)
                    size = lf.stat().st_size
                    if size < p: p = 0
                    if size > p:
                        with lf.open('r', errors='replace') as fh:
                            fh.seek(p)
                            new = fh.read()
                            pos[lf.name] = fh.tell()
                        if new.strip():
                            yield f"data: {json.dumps({'file': lf.name, 'data': new})}\n\n"
                except Exception:
                    pass
            eventlet.sleep(1)
    return Response(stream_with_context(generate()), mimetype='text/event-stream',
                    headers={'Cache-Control':'no-cache','X-Accel-Buffering':'no'})


# ════════════════════════════════════════════════════════════════
#  /botN reverse proxy
# ════════════════════════════════════════════════════════════════

@app.route('/bot<int:bot_num>', defaults={'subpath':''})
@app.route('/bot<int:bot_num>/<path:subpath>')
@login_required
def bot_proxy(bot_num, subpath):
    if not _requests:
        return "requests library not available", 500
    target_port = None
    for key, info in DOCKER_REGISTRY.items():
        pname = info.get('process_name','')
        if re.search(rf'bot{bot_num}$', pname, re.IGNORECASE):
            target_port = info.get('web_port')
            break
    if not target_port:
        return (f"<h2 style='font-family:sans-serif;padding:2rem'>No web UI for bot{bot_num}</h2>"
                "<p style='font-family:sans-serif;padding:0 2rem'>Set a Web Port during Docker deployment to enable proxying.</p>"), 404
    target_url = f"http://localhost:{target_port}/{subpath}"
    qs = request.query_string.decode()
    if qs: target_url += f"?{qs}"
    try:
        resp = _requests.request(
            method=request.method, url=target_url,
            headers={k:v for k,v in request.headers if k.lower() not in ('host','content-length')},
            data=request.get_data(), cookies=request.cookies,
            allow_redirects=False, timeout=30)
        excluded = {'content-encoding','content-length','transfer-encoding','connection'}
        headers = {k:v for k,v in resp.headers.items() if k.lower() not in excluded}
        if 'location' in headers and headers['location'].startswith('/'):
            headers['location'] = f"/bot{bot_num}{headers['location']}"
        return Response(resp.content, status=resp.status_code, headers=headers)
    except _requests.exceptions.ConnectionError:
        return f"<h2 style='font-family:sans-serif;padding:2rem'>Bot {bot_num} web UI not reachable</h2>", 502
    except Exception as e:
        return jsonify({"status":"error","message":str(e)}), 500


# ════════════════════════════════════════════════════════════════
#  Socket events
# ════════════════════════════════════════════════════════════════

@socketio.on('connect')
def handle_connect():
    emit('connected', {'data':'Connected'})
    broadcast_status_update()

@socketio.on('disconnect')
def handle_disconnect():
    logger.info("Client disconnected")

@socketio.on('request_status')
def handle_status_request():
    try:
        status = run_supervisor_command("status")
        if status["status"] == "success":
            processes = []
            for line in status["message"].splitlines():
                p = parse_supervisor_status(line)
                if not p: continue
                pname = p["name"]
                if p["status"] in ("FATAL","BACKOFF","EXITED"):
                    FAILURE_COUNTS[pname] += 1
                    if FAILURE_COUNTS[pname] >= MAX_FAILURES_BEFORE_PAUSE:
                        PAUSED_BY_SYSTEM.add(pname)
                    p["auto_paused"] = pname in PAUSED_BY_SYSTEM
                else:
                    if p["status"] == "RUNNING":
                        FAILURE_COUNTS[pname] = 0
                        PAUSED_BY_SYSTEM.discard(pname)
                    p["auto_paused"] = pname in PAUSED_BY_SYSTEM
                processes.append(p)
            for p in processes:
                pname = p["name"]
                safe  = pname.replace(' ', '_')
                dq = METRICS.get(pname)
                if dq:
                    latest = dq[-1]
                    p["cpu"]    = latest.get("cpu", 0.0)
                    p["mem_mb"] = latest.get("mem_mb", 0.0)
                else:
                    p["cpu"]    = 0.0
                    p["mem_mb"] = 0.0
                p["restart_count"] = RESTART_COUNTS.get(pname, 0)
                breg = BOT_REGISTRY.get(safe, {})
                rl   = breg.get('rate_limit')
                if rl:
                    limited, remaining, _ = _is_restart_rate_limited(pname)
                    p["rate_limit"] = {"max": rl["max_restarts"], "window": rl["window_sec"],
                                       "remaining": remaining, "limited": limited}
                hcfg = HEALTH_CONFIG.get(safe, {})
                if hcfg.get('enabled'):
                    hs = HEALTH_STATE.get(safe, {})
                    p["health"] = {
                        "url":      hcfg.get('url',''),
                        "ok":       hs.get('consecutive_failures', 0) == 0 and hs.get('last_ok') is not None,
                        "failures": hs.get('consecutive_failures', 0),
                        "last_ok":  hs.get('last_ok'),
                    }
                p["has_webhook"]  = bool(WEBHOOK_SECRETS.get(safe))
                sched = SCHEDULED_DEPLOYS.get(safe, {})
                p["scheduled"]    = sched.get('enabled', False)
                p["deploy_type"]  = breg.get('deploy_type', 'git')
                p["commit_count"] = len(COMMIT_HISTORY.get(safe, []))
            emit('status_update', {"status":"success","processes":processes,"timestamp":datetime.utcnow().isoformat()})
        else:
            emit('status_update', {"status":"error","message":status["message"],"processes":[]})
    except Exception as e:
        emit('status_update', {"status":"error","message":str(e),"processes":[]})


# ════════════════════════════════════════════════════════════════
#  Log stream page + SSE
# ════════════════════════════════════════════════════════════════

@app.route('/logstream')
@login_required
def logstream_page():
    return render_template('logstream.html')

@app.route('/logstream/stream')
@login_required
def logstream_sse():
    def generate():
        log_dir = Path(SUPERVISOR_LOG_DIR)
        positions = {}
        while True:
            for lf in sorted(log_dir.glob("*.log")):
                if '_combined' in lf.name: continue
                try:
                    pos = positions.get(lf.name, 0)
                    size = lf.stat().st_size
                    if size < pos: pos = 0
                    if size > pos:
                        with lf.open('r', errors='replace') as fh:
                            fh.seek(pos)
                            data = fh.read()
                            positions[lf.name] = fh.tell()
                        if data.strip():
                            yield f"data: {json.dumps({'file': lf.name, 'data': data})}\n\n"
                except Exception:
                    pass
            eventlet.sleep(1)
    return Response(stream_with_context(generate()), mimetype='text/event-stream',
                    headers={'Cache-Control':'no-cache','X-Accel-Buffering':'no'})


# ════════════════════════════════════════════════════════════════
#  Cron / settings
# ════════════════════════════════════════════════════════════════

@app.route('/config/cron', methods=['GET','POST'])
@login_required
def config_cron():
    global CRON_RESTART_INTERVAL, _cron_thread
    if request.method == 'POST':
        h = int((request.get_json(silent=True) or {}).get('hours', 0))
        CRON_RESTART_INTERVAL = max(0, h)
        os.environ['CRON_RESTART_HOURS'] = str(CRON_RESTART_INTERVAL)
        _start_cron_thread()
        return jsonify({"status":"success","hours": CRON_RESTART_INTERVAL})
    return jsonify({"status":"success","hours": CRON_RESTART_INTERVAL})


# ════════════════════════════════════════════════════════════════
#  Background threads
# ════════════════════════════════════════════════════════════════

def _cron_restart_loop():
    while True:
        interval = CRON_RESTART_INTERVAL
        if interval <= 0:
            eventlet.sleep(60); continue
        eventlet.sleep(interval * 3600)
        if CRON_RESTART_INTERVAL <= 0: continue
        try:
            run_supervisor_command("restart","all")
            broadcast_status_update()
        except Exception as e:
            logger.error(f"Cron error: {e}")

def _start_cron_thread():
    global _cron_thread
    if not _cron_thread:
        _cron_thread = eventlet.spawn(_cron_restart_loop)

def _auto_delete_logs_loop():
    while True:
        eventlet.sleep(24*3600)
        for lf in Path(SUPERVISOR_LOG_DIR).glob("*.log"):
            try: lf.unlink()
            except: pass

eventlet.spawn(_auto_delete_logs_loop)
eventlet.spawn(_metrics_loop)
eventlet.spawn(_health_check_loop)
_start_cron_thread()
_start_sched_thread()


# ════════════════════════════════════════════════════════════════
#  Metrics API endpoints
# ════════════════════════════════════════════════════════════════

@app.route('/metrics/<path:process_name>', methods=['GET'])
@login_required
def get_metrics(process_name):
    """
    Return CPU/RAM time-series + uptime history + restart count for a bot.
    Query params:
      since=<unix_ts>   — return samples after this timestamp (default: last hour)
      window=<minutes>  — alternative to since; return last N minutes (default 60)
    """
    now = datetime.utcnow().timestamp()
    window_min = int(request.args.get('window', 60))
    since      = float(request.args.get('since', now - window_min * 60))

    samples = [s for s in METRICS.get(process_name, []) if s['ts'] >= since]

    # Uptime history for last 7 days
    history = UPTIME_HISTORY.get(process_name, [])

    # Compute uptime % over the window
    uptime_pct = _compute_uptime_pct(history, since, now)

    return jsonify({
        "status": "success",
        "bot": process_name,
        "samples": samples,
        "uptime_history": history,
        "restart_count": RESTART_COUNTS.get(process_name, 0),
        "uptime_pct": uptime_pct,
        "window_minutes": window_min,
    }), 200


@app.route('/metrics/all', methods=['GET'])
@login_required
def get_all_metrics():
    """Return latest single sample + restart count for every known bot."""
    result = {}
    for name, dq in METRICS.items():
        latest = dq[-1] if dq else {'cpu': 0.0, 'mem_mb': 0.0, 'ts': 0}
        result[name] = {
            'latest': latest,
            'restart_count': RESTART_COUNTS.get(name, 0),
            'uptime_pct': _compute_uptime_pct(
                UPTIME_HISTORY.get(name, []),
                datetime.utcnow().timestamp() - 86400,
                datetime.utcnow().timestamp()
            ),
        }
    return jsonify({"status": "success", "metrics": result}), 200


def _compute_uptime_pct(history: list, since: float, until: float) -> float:
    """Compute what fraction of the [since, until] window the bot was RUNNING."""
    if not history or until <= since:
        return 0.0
    window = until - since
    running_secs = 0.0
    last_start   = None

    # Walk events in chronological order
    events = sorted(history, key=lambda e: e['ts'])

    for ev in events:
        ts    = ev['ts']
        event = ev['event']
        if event == 'start':
            if ts >= since:
                last_start = max(ts, since)
            elif ts < since:
                last_start = since   # was already running at window start
        elif event in ('stop', 'crash'):
            if last_start is not None:
                end = min(ts, until)
                running_secs += max(0.0, end - last_start)
                last_start = None

    # Still running at window end
    if last_start is not None:
        running_secs += max(0.0, until - last_start)

    pct = (running_secs / window) * 100
    return round(min(pct, 100.0), 1)


# ── Alert configuration ──────────────────────────────────────────

@app.route('/config/alerts', methods=['GET', 'POST'])
@login_required
def config_alerts():
    if request.method == 'POST':
        data = request.get_json(silent=True) or {}
        allowed = {'telegram_token', 'telegram_chat_id', 'discord_webhook'}
        for k in allowed:
            if k in data:
                ALERT_CONFIG[k] = str(data[k]).strip()
        _save_alert_config()
        return jsonify({"status": "success", "config": {
            k: ('***' if 'token' in k else v)
            for k, v in ALERT_CONFIG.items()
        }}), 200
    safe_cfg = {k: ('***' if 'token' in k and v else v) for k, v in ALERT_CONFIG.items()}
    return jsonify({"status": "success", "config": safe_cfg}), 200


@app.route('/config/alerts/test', methods=['POST'])
@login_required
def test_alert():
    """Send a test alert to all configured channels."""
    _send_alert("test-bot", "Test Alert", "This is a test notification from BotClusters.")
    return jsonify({"status": "success", "message": "Test alert dispatched"}), 200


# ════════════════════════════════════════════════════════════════
#  1. Live stdin injection
# ════════════════════════════════════════════════════════════════

def _get_or_open_stdin_pipe(process_name: str):
    """
    Return a writable file handle to the running bot's stdin.
    For git bots we find the PID via supervisorctl and attach via /proc/<pid>/fd/0.
    For Docker bots we use `docker exec -i`.
    """
    safe = process_name.replace(' ', '_')

    # Try /proc/<pid>/fd/0 (git bots)
    r = run_supervisor_command("status", process_name)
    if r["status"] == "success":
        p = parse_supervisor_status(r["message"])
        if p and p.get("pid"):
            stdin_path = f"/proc/{p['pid']}/fd/0"
            if Path(stdin_path).exists():
                try:
                    return open(stdin_path, 'w')
                except Exception:
                    pass

    # Docker bot fallback — use docker exec -i
    dreg = DOCKER_REGISTRY.get(safe, {})
    if dreg:
        return None   # handled separately in the route

    return None


@app.route('/bot/stdin/<path:process_name>', methods=['POST'])
@login_required
def bot_stdin(process_name):
    """
    Send a line of text to a running bot's stdin.
    Body: { "line": "command to send" }
    Works for both git bots (via /proc/<pid>/fd/0) and Docker bots (docker exec).
    """
    data = request.get_json(silent=True) or {}
    line = data.get('line', '').strip()
    if not line:
        return jsonify({"status": "error", "message": "No line provided"}), 400

    safe = process_name.replace(' ', '_')

    # Check bot is running
    r = run_supervisor_command("status", process_name)
    if r["status"] != "success" or "RUNNING" not in r["message"]:
        return jsonify({"status": "error", "message": "Bot is not running"}), 400

    # Docker bot path
    dreg = DOCKER_REGISTRY.get(safe, {})
    if dreg:
        cname = dreg['container_name']
        try:
            result = subprocess.run(
                [_docker_cmd(), 'exec', '-i', cname, 'sh', '-c', f'echo {json.dumps(line)}'],
                capture_output=True, text=True, timeout=5)
            if result.returncode == 0:
                return jsonify({"status": "success", "message": f"Sent to {cname}"}), 200
            return jsonify({"status": "error", "message": result.stderr.strip()}), 500
        except Exception as e:
            return jsonify({"status": "error", "message": str(e)}), 500

    # Git bot path — write to /proc/<pid>/fd/0
    pr = parse_supervisor_status(r["message"])
    if not pr or not pr.get("pid"):
        return jsonify({"status": "error", "message": "Could not determine PID"}), 500
    stdin_path = f"/proc/{pr['pid']}/fd/0"
    try:
        with open(stdin_path, 'w') as fh:
            fh.write(line + '\n')
        return jsonify({"status": "success", "message": f"Sent: {line}"}), 200
    except Exception as e:
        return jsonify({"status": "error", "message": f"stdin write failed: {e}"}), 500


# ════════════════════════════════════════════════════════════════
#  2. Environment variable editor (live, no full redeploy)
# ════════════════════════════════════════════════════════════════

@app.route('/bot/env/<path:process_name>', methods=['GET'])
@login_required
def bot_env_get(process_name):
    """Return the current env vars stored in the registry for this bot."""
    safe = process_name.replace(' ', '_')
    breg = BOT_REGISTRY.get(safe) or BOT_REGISTRY.get(process_name, {})
    return jsonify({"status": "success",
                    "env": breg.get('env', {}),
                    "deploy_type": breg.get('deploy_type', 'git')}), 200


@app.route('/bot/env/<path:process_name>', methods=['POST'])
@login_required
def bot_env_set(process_name):
    """
    Update env vars for a bot and hot-reload it.
    Body: { "env": { "KEY": "VALUE", ... }, "restart": true }
    For git bots:  rewrites the supervisord .conf environment= line + restarts.
    For Docker bots: docker stop → docker rm → docker create with new env → start.
    """
    data = request.get_json(silent=True) or {}
    new_env  = data.get('env', {})
    do_restart = data.get('restart', True)

    if not isinstance(new_env, dict):
        return jsonify({"status": "error", "message": "env must be a JSON object"}), 400

    safe = process_name.replace(' ', '_')
    breg = BOT_REGISTRY.get(safe) or BOT_REGISTRY.get(process_name, {})
    if not breg:
        return jsonify({"status": "error", "message": "Bot not found in registry"}), 404

    deploy_type = breg.get('deploy_type', 'git')
    config_path = Path(SUPERVISORD_CONF_DIR) / f"{safe}.conf"

    try:
        if deploy_type == 'git':
            if not config_path.exists():
                return jsonify({"status": "error", "message": "Supervisor config not found"}), 404

            # Read existing config, replace/add environment line
            cfg = configparser.ConfigParser()
            cfg.read(config_path)
            section = f'program:{safe}'
            if section not in cfg:
                return jsonify({"status": "error", "message": "Config section not found"}), 404

            env_str = ','.join(f'{k}="{v}"' for k, v in new_env.items())
            if env_str:
                cfg[section]['environment'] = env_str
            elif 'environment' in cfg[section]:
                del cfg[section]['environment']

            with open(config_path, 'w') as f:
                cfg.write(f)

        elif deploy_type == 'docker':
            if not _docker_available():
                return jsonify({"status": "error",
                                "message": "Docker not available — env saved but container not recreated."}), 503
            dreg = DOCKER_REGISTRY.get(safe, {})
            if not dreg:
                return jsonify({"status": "error", "message": "Docker registry entry not found"}), 404

            cname      = dreg['container_name']
            host_port  = dreg.get('web_port')
            int_port   = dreg.get('internal_port')

            # Stop & remove old container, recreate with new env
            subprocess.run([_docker_cmd(), 'stop', cname], capture_output=True, timeout=15)
            subprocess.run([_docker_cmd(), 'rm',   cname], capture_output=True, timeout=15)

            # Rebuild create command
            image_name = cname   # image tag = container name in our scheme
            run_cmd = [_docker_cmd(), 'create', '--name', cname, '--restart', 'no']
            for k, v in new_env.items():
                run_cmd += ['-e', f'{k}={v}']
            if host_port and int_port:
                run_cmd += ['-p', f'{host_port}:{int_port}']
            run_cmd.append(image_name)
            subprocess.run(run_cmd, check=True)

        # Persist updated env in registry
        breg['env'] = new_env
        key = safe if safe in BOT_REGISTRY else process_name
        BOT_REGISTRY[key]['env'] = new_env
        _save_bot_registry()

        if do_restart:
            supervisord_reload()
            run_supervisor_command("stop",  process_name)
            time.sleep(1)
            run_supervisor_command("start", process_name)
            broadcast_status_update()
            return jsonify({"status": "success",
                            "message": f"Env updated and {process_name} restarted"}), 200
        else:
            supervisord_reload()
            return jsonify({"status": "success",
                            "message": "Env updated (restart skipped)"}), 200

    except subprocess.CalledProcessError as e:
        return jsonify({"status": "error", "message": str(e)}), 500
    except Exception as e:
        logger.error(f"bot_env_set error: {e}")
        return jsonify({"status": "error", "message": str(e)}), 500


# ════════════════════════════════════════════════════════════════
#  3. Custom health check URL
# ════════════════════════════════════════════════════════════════

@app.route('/bot/health/<path:process_name>', methods=['GET'])
@login_required
def bot_health_get(process_name):
    safe = process_name.replace(' ', '_')
    cfg   = HEALTH_CONFIG.get(safe, {})
    state = HEALTH_STATE.get(safe, {})
    return jsonify({"status": "success", "config": cfg, "state": {
        "last_ok":              state.get("last_ok"),
        "last_check":          state.get("last_check"),
        "consecutive_failures": state.get("consecutive_failures", 0),
    }}), 200


@app.route('/bot/health/<path:process_name>', methods=['POST'])
@login_required
def bot_health_set(process_name):
    """
    Configure a health check for this bot.
    Body: { url, interval_sec (default 30), timeout_sec (default 5), enabled (default true) }
    """
    data    = request.get_json(silent=True) or {}
    safe    = process_name.replace(' ', '_')
    url     = data.get('url', '').strip()
    enabled = data.get('enabled', True)

    if enabled and not url:
        return jsonify({"status": "error", "message": "url is required when enabled=true"}), 400
    if url and not url.startswith(('http://', 'https://')):
        return jsonify({"status": "error", "message": "url must start with http:// or https://"}), 400

    HEALTH_CONFIG[safe] = {
        "url":          url,
        "interval_sec": max(10, int(data.get('interval_sec', 30))),
        "timeout_sec":  max(2,  int(data.get('timeout_sec',  5))),
        "enabled":      bool(enabled),
        "process_name": process_name,
    }
    # Reset failure counter when config changes
    HEALTH_STATE[safe]['consecutive_failures'] = 0
    _save_health_config()
    return jsonify({"status": "success",
                    "message": f"Health check {'enabled' if enabled else 'disabled'} for {process_name}",
                    "config": HEALTH_CONFIG[safe]}), 200


@app.route('/bot/health/<path:process_name>', methods=['DELETE'])
@login_required
def bot_health_delete(process_name):
    safe = process_name.replace(' ', '_')
    HEALTH_CONFIG.pop(safe, None)
    HEALTH_STATE.pop(safe, None)
    _save_health_config()
    return jsonify({"status": "success", "message": "Health check removed"}), 200


def _health_check_loop():
    """Background loop: run health checks for all configured bots."""
    eventlet.sleep(15)
    while True:
        now = datetime.utcnow().timestamp()
        for safe, cfg in list(HEALTH_CONFIG.items()):
            if not cfg.get('enabled'):
                continue
            state    = HEALTH_STATE[safe]
            last_chk = state.get('last_check') or 0
            interval = cfg.get('interval_sec', 30)
            if now - last_chk < interval:
                continue

            url     = cfg.get('url', '')
            timeout = cfg.get('timeout_sec', 5)
            pname   = cfg.get('process_name', safe)
            ok      = False
            try:
                if _requests:
                    resp = _requests.get(url, timeout=timeout)
                    ok   = (200 <= resp.status_code < 400)
            except Exception:
                ok = False

            state['last_check'] = now
            if ok:
                state['last_ok'] = now
                state['consecutive_failures'] = 0
                logger.debug(f"Health OK: {pname} → {url}")
            else:
                state['consecutive_failures'] = state.get('consecutive_failures', 0) + 1
                logger.warning(f"Health FAIL #{state['consecutive_failures']}: {pname} → {url}")
                if state['consecutive_failures'] >= HEALTH_MAX_FAILURES:
                    logger.error(f"Health check: auto-restarting {pname}")
                    _send_alert(pname, "🏥 Health check failed — auto-restarting",
                                f"URL: {url}  Failures: {state['consecutive_failures']}")
                    run_supervisor_command("restart", pname)
                    state['consecutive_failures'] = 0
                    broadcast_status_update()

        eventlet.sleep(10)


# ════════════════════════════════════════════════════════════════
#  4. Restart rate limiter
# ════════════════════════════════════════════════════════════════

@app.route('/bot/ratelimit/<path:process_name>', methods=['GET'])
@login_required
def bot_ratelimit_get(process_name):
    safe = process_name.replace(' ', '_')
    breg = BOT_REGISTRY.get(safe) or BOT_REGISTRY.get(process_name, {})
    rl   = breg.get('rate_limit', {})
    ts   = list(RESTART_TIMESTAMPS.get(safe, []))
    return jsonify({"status": "success", "rate_limit": rl,
                    "recent_restarts": len(ts),
                    "oldest_in_window": ts[0] if ts else None}), 200


@app.route('/bot/ratelimit/<path:process_name>', methods=['POST'])
@login_required
def bot_ratelimit_set(process_name):
    """
    Set a restart rate limit for this bot.
    Body: { max_restarts: 5, window_sec: 3600 }
    """
    data = request.get_json(silent=True) or {}
    safe = process_name.replace(' ', '_')

    max_r  = int(data.get('max_restarts', 5))
    window = int(data.get('window_sec', 3600))

    if max_r < 1 or window < 60:
        return jsonify({"status": "error",
                        "message": "max_restarts ≥ 1 and window_sec ≥ 60 required"}), 400

    key = safe if safe in BOT_REGISTRY else process_name
    if key in BOT_REGISTRY:
        BOT_REGISTRY[key]['rate_limit'] = {'max_restarts': max_r, 'window_sec': window}
        _save_bot_registry()

    return jsonify({"status": "success",
                    "message": f"Rate limit set: max {max_r} restarts per {window}s",
                    "rate_limit": {'max_restarts': max_r, 'window_sec': window}}), 200


@app.route('/bot/ratelimit/<path:process_name>', methods=['DELETE'])
@login_required
def bot_ratelimit_delete(process_name):
    safe = process_name.replace(' ', '_')
    key  = safe if safe in BOT_REGISTRY else process_name
    if key in BOT_REGISTRY:
        BOT_REGISTRY[key].pop('rate_limit', None)
        _save_bot_registry()
    RESTART_TIMESTAMPS.pop(safe, None)
    return jsonify({"status": "success", "message": "Rate limit removed"}), 200


def _is_restart_rate_limited(process_name: str) -> tuple:
    """
    Returns (is_limited: bool, remaining: int, reset_in: int).
    Prunes stale timestamps from the window first.
    """
    safe = process_name.replace(' ', '_')
    breg = BOT_REGISTRY.get(safe) or BOT_REGISTRY.get(process_name, {})
    rl   = breg.get('rate_limit')
    if not rl:
        return False, 999, 0

    max_r  = rl.get('max_restarts', 5)
    window = rl.get('window_sec', 3600)
    now    = time.time()
    dq     = RESTART_TIMESTAMPS[safe]

    # Prune old timestamps
    while dq and now - dq[0] > window:
        dq.popleft()

    if len(dq) >= max_r:
        reset_in = int(window - (now - dq[0]))
        return True, 0, max(0, reset_in)

    return False, max_r - len(dq), 0


def _record_restart(process_name: str):
    safe = process_name.replace(' ', '_')
    RESTART_TIMESTAMPS[safe].append(time.time())


# ════════════════════════════════════════════════════════════════
#  Feature 1 — Auto-deploy on git push (webhook)
# ════════════════════════════════════════════════════════════════

import hmac, hashlib

def _verify_github_signature(payload_bytes: bytes, sig_header: str, secret: str) -> bool:
    """Verify X-Hub-Signature-256 from GitHub."""
    if not sig_header or not sig_header.startswith('sha256='):
        return False
    expected = 'sha256=' + hmac.new(
        secret.encode(), payload_bytes, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, sig_header)


@app.route('/webhook/<path:process_name>', methods=['POST'])
def git_webhook(process_name):
    """
    GitHub / GitLab push webhook endpoint.
    URL: POST /webhook/<bot-process-name>
    Configure in repo → Settings → Webhooks → Content-type: application/json
    Secret: the token shown in the Webhook Config modal.
    On a valid push event: git pull → pip install → restart bot.
    """
    safe   = process_name.replace(' ', '_')
    secret = WEBHOOK_SECRETS.get(safe, '')
    breg   = BOT_REGISTRY.get(safe) or BOT_REGISTRY.get(process_name, {})

    if not breg:
        return jsonify({"status": "error", "message": "Bot not registered"}), 404

    payload_bytes = request.get_data()

    # Verify secret if one is configured
    if secret:
        sig = request.headers.get('X-Hub-Signature-256', '') or \
              request.headers.get('X-Gitlab-Token', '')
        # GitLab sends the raw token, not HMAC
        if request.headers.get('X-Gitlab-Token'):
            if sig != secret:
                return jsonify({"status": "error", "message": "Invalid token"}), 401
        else:
            if not _verify_github_signature(payload_bytes, sig, secret):
                return jsonify({"status": "error", "message": "Invalid signature"}), 401

    # Only redeploy on push events; ignore others
    event = request.headers.get('X-GitHub-Event', '') or \
            request.headers.get('X-Gitlab-Event', '')
    if event and event not in ('push', 'Push Hook', ''):
        return jsonify({"status": "ok", "message": f"Ignored event: {event}"}), 200

    bot_dir = Path('/app') / safe
    if not bot_dir.exists():
        return jsonify({"status": "error", "message": "Bot directory not found"}), 500

    # Run git pull in background greenlet so webhook returns fast
    def _pull_and_redeploy():
        try:
            branch = breg.get('branch', 'main')
            subprocess.run(['git', 'fetch', 'origin'], cwd=str(bot_dir),
                           capture_output=True, timeout=60)
            subprocess.run(['git', 'reset', '--hard', f'origin/{branch}'],
                           cwd=str(bot_dir), capture_output=True, timeout=30)
            _snapshot_commits(safe, bot_dir)
            result = _redeploy_git_bot(process_name, bot_dir, breg)
            if result['status'] == 'success':
                _send_alert(process_name, "🚀 Auto-deployed via webhook",
                            f"Branch: {branch}")
            else:
                _send_alert(process_name, "❌ Webhook deploy failed", result['message'])
        except Exception as e:
            logger.error(f"webhook redeploy {process_name}: {e}")
            _send_alert(process_name, "❌ Webhook deploy error", str(e))

    eventlet.spawn(_pull_and_redeploy)
    return jsonify({"status": "ok", "message": "Webhook received — deploying…"}), 202


@app.route('/bot/webhook/config/<path:process_name>', methods=['GET'])
@login_required
def webhook_config_get(process_name):
    safe   = process_name.replace(' ', '_')
    secret = WEBHOOK_SECRETS.get(safe, '')
    webhook_url = request.host_url.rstrip('/') + f'/webhook/{process_name}'
    return jsonify({"status": "success", "webhook_url": webhook_url,
                    "has_secret": bool(secret)}), 200


@app.route('/bot/webhook/config/<path:process_name>', methods=['POST'])
@login_required
def webhook_config_set(process_name):
    """Set or regenerate the webhook secret for a bot."""
    data   = request.get_json(silent=True) or {}
    safe   = process_name.replace(' ', '_')
    secret = data.get('secret', '').strip()

    if data.get('regenerate') or not secret:
        import secrets as _secrets
        secret = _secrets.token_hex(24)

    WEBHOOK_SECRETS[safe] = secret
    _save_webhook_secrets()
    webhook_url = request.host_url.rstrip('/') + f'/webhook/{process_name}'
    return jsonify({"status": "success", "webhook_url": webhook_url,
                    "secret": secret}), 200


# ════════════════════════════════════════════════════════════════
#  Feature 2 — Deploy from ZIP upload
# ════════════════════════════════════════════════════════════════

@app.route('/bot/upload/<path:process_name>', methods=['POST'])
@login_required
def bot_upload_zip(process_name):
    """
    Upload a ZIP of bot source code and deploy it (no git URL required).
    Multipart form fields:
      file          — .zip file
      run_command   — entry point file (e.g. bot.py)
      python_version — optional
      env_json      — optional JSON string of env vars
    """
    safe = process_name.replace(' ', '_')
    if not re.match(r'^[a-zA-Z0-9_ \-]+$', process_name):
        return jsonify({"status": "error", "message": "Invalid process_name"}), 400
    if not _bot_number_from_name(process_name):
        return jsonify({"status": "error",
                        "message": "process_name must end with botN"}), 400

    uploaded = request.files.get('file')
    if not uploaded or not uploaded.filename.endswith('.zip'):
        return jsonify({"status": "error", "message": "A .zip file is required"}), 400

    run_command = request.form.get('run_command', 'bot.py').strip()
    python_ver  = request.form.get('python_version', '').strip()
    env_raw     = request.form.get('env_json', '{}')
    try:
        env_vars = json.loads(env_raw)
    except Exception:
        env_vars = {}

    import zipfile
    staging    = UPLOAD_STAGING / safe
    bot_dir    = Path('/app') / safe

    try:
        # Save and extract zip to staging
        staging.mkdir(parents=True, exist_ok=True)
        zip_path = staging / 'upload.zip'
        uploaded.save(str(zip_path))

        # Wipe old dir and extract fresh
        if bot_dir.exists():
            shutil.rmtree(bot_dir)
        bot_dir.mkdir(parents=True)

        with zipfile.ZipFile(zip_path, 'r') as zf:
            # Strip common top-level dir if present
            names   = zf.namelist()
            prefix  = names[0].split('/')[0] + '/' if names and '/' in names[0] else ''
            all_same_prefix = prefix and all(n.startswith(prefix) for n in names)
            for member in zf.infolist():
                target = member.filename
                if all_same_prefix:
                    target = target[len(prefix):]
                if not target:
                    continue
                dest = bot_dir / target
                if member.is_dir():
                    dest.mkdir(parents=True, exist_ok=True)
                else:
                    dest.parent.mkdir(parents=True, exist_ok=True)
                    with zf.open(member) as src, open(dest, 'wb') as dst:
                        shutil.copyfileobj(src, dst)

        # Install deps + write supervisord config
        python_exec = shutil.which(f"python{python_ver}") or shutil.which("python3") or "python3"
        venv_dir    = bot_dir / 'venv'
        req_file    = bot_dir / 'requirements.txt'
        if req_file.exists():
            subprocess.run([python_exec, '-m', 'venv', str(venv_dir)], check=True)
            subprocess.run([str(venv_dir/'bin'/'pip'), 'install', '--no-cache-dir',
                            '-r', str(req_file)], check=True)

        bot_file = bot_dir / run_command
        py       = venv_dir / 'bin' / 'python3'
        if bot_file.suffix == '.sh':
            command = f"bash {bot_file}"
        else:
            command = f"{py} {bot_file}"

        write_git_supervisord_config(process_name, command, str(bot_dir), env_vars)

        # Save to registry (no git_url for ZIP deploys)
        BOT_REGISTRY[safe] = {
            'name': process_name, 'safe': safe, 'git_url': '',
            'branch': '', 'deploy_type': 'zip',
            'run_command': run_command, 'python_version': python_ver,
            'web_port': None, 'env': env_vars,
            'added_at': datetime.utcnow().isoformat(),
            'zip_filename': uploaded.filename,
        }
        _save_bot_registry()
        supervisord_reload()
        broadcast_status_update()

        # Clean up staging
        shutil.rmtree(staging, ignore_errors=True)

        return jsonify({"status": "success",
                        "message": f"ZIP deployed: {process_name}"}), 200

    except zipfile.BadZipFile:
        return jsonify({"status": "error", "message": "Invalid or corrupt ZIP file"}), 400
    except subprocess.CalledProcessError as e:
        return jsonify({"status": "error", "message": str(e)}), 500
    except Exception as e:
        logger.error(f"zip upload error {process_name}: {e}")
        return jsonify({"status": "error", "message": str(e)}), 500


# ════════════════════════════════════════════════════════════════
#  Feature 3 — Rollback to previous git commit
# ════════════════════════════════════════════════════════════════

@app.route('/bot/commits/<path:process_name>', methods=['GET'])
@login_required
def bot_commits(process_name):
    """Return the last 10 git commits for this bot."""
    safe    = process_name.replace(' ', '_')
    bot_dir = Path('/app') / safe
    # Refresh from disk if dir exists
    if bot_dir.exists() and (bot_dir / '.git').exists():
        _snapshot_commits(safe, bot_dir)
    commits = COMMIT_HISTORY.get(safe, [])
    return jsonify({"status": "success", "commits": commits}), 200


@app.route('/bot/rollback/<path:process_name>', methods=['POST'])
@login_required
def bot_rollback(process_name):
    """
    Roll back a bot to a specific git commit SHA.
    Body: { "sha": "<full-or-short-sha>" }
    """
    data = request.get_json(silent=True) or {}
    sha  = data.get('sha', '').strip()
    if not sha or not re.match(r'^[0-9a-f]{6,40}$', sha, re.I):
        return jsonify({"status": "error", "message": "Valid SHA required"}), 400

    safe    = process_name.replace(' ', '_')
    breg    = BOT_REGISTRY.get(safe) or BOT_REGISTRY.get(process_name, {})
    bot_dir = Path('/app') / safe

    if not breg:
        return jsonify({"status": "error", "message": "Bot not in registry"}), 404
    if not bot_dir.exists() or not (bot_dir / '.git').exists():
        return jsonify({"status": "error",
                        "message": "Bot directory or .git not found"}), 404
    if breg.get('deploy_type') == 'zip':
        return jsonify({"status": "error",
                        "message": "Rollback not available for ZIP-deployed bots"}), 400

    try:
        # Hard reset to the requested commit
        subprocess.run(['git', 'checkout', sha], cwd=str(bot_dir), check=True,
                       capture_output=True, timeout=30)
        _snapshot_commits(safe, bot_dir)

        result = _redeploy_git_bot(process_name, bot_dir, breg)
        if result['status'] == 'success':
            _send_alert(process_name, f"⏪ Rolled back to {sha[:8]}", "")
        return jsonify({**result, "sha": sha}), 200 if result['status']=='success' else 500

    except subprocess.CalledProcessError as e:
        return jsonify({"status": "error", "message": str(e)}), 500
    except Exception as e:
        logger.error(f"rollback {process_name}: {e}")
        return jsonify({"status": "error", "message": str(e)}), 500


# ════════════════════════════════════════════════════════════════
#  Feature 4 — Scheduled deployments
# ════════════════════════════════════════════════════════════════

def _next_run_from_interval(interval_hours: float) -> float:
    return time.time() + interval_hours * 3600


@app.route('/bot/schedule/<path:process_name>', methods=['GET'])
@login_required
def bot_schedule_get(process_name):
    safe = process_name.replace(' ', '_')
    sched = SCHEDULED_DEPLOYS.get(safe, {})
    return jsonify({"status": "success", "schedule": sched}), 200


@app.route('/bot/schedule/<path:process_name>', methods=['POST'])
@login_required
def bot_schedule_set(process_name):
    """
    Schedule periodic auto-redeploy (git pull + restart).
    Body: { interval_hours: 6, enabled: true }
    interval_hours: how often to redeploy (min 0.25 = 15 min)
    """
    data   = request.get_json(silent=True) or {}
    safe   = process_name.replace(' ', '_')
    hours  = float(data.get('interval_hours', 24))
    enabled = bool(data.get('enabled', True))

    if hours < 0.25:
        return jsonify({"status": "error",
                        "message": "interval_hours must be ≥ 0.25 (15 min)"}), 400

    next_run = _next_run_from_interval(hours) if enabled else None
    SCHEDULED_DEPLOYS[safe] = {
        'process_name': process_name,
        'interval_hours': hours,
        'enabled': enabled,
        'next_run': next_run,
        'last_run': None,
    }
    _save_scheduled()
    _start_sched_thread()
    return jsonify({
        "status": "success",
        "message": f"Scheduled every {hours}h" if enabled else "Schedule disabled",
        "schedule": SCHEDULED_DEPLOYS[safe]
    }), 200


@app.route('/bot/schedule/<path:process_name>', methods=['DELETE'])
@login_required
def bot_schedule_delete(process_name):
    safe = process_name.replace(' ', '_')
    SCHEDULED_DEPLOYS.pop(safe, None)
    _save_scheduled()
    return jsonify({"status": "success", "message": "Schedule removed"}), 200


def _scheduled_deploy_loop():
    """Background loop: trigger scheduled redeployments."""
    eventlet.sleep(30)
    while True:
        now = time.time()
        for safe, sched in list(SCHEDULED_DEPLOYS.items()):
            if not sched.get('enabled'):
                continue
            next_run = sched.get('next_run') or 0
            if now < next_run:
                continue
            pname   = sched.get('process_name', safe)
            breg    = BOT_REGISTRY.get(safe, {})
            bot_dir = Path('/app') / safe

            if not breg or not bot_dir.exists():
                continue
            if breg.get('deploy_type') == 'zip':
                # Can't auto-redeploy ZIPs; skip
                continue

            logger.info(f"Scheduled deploy: {pname}")
            try:
                branch = breg.get('branch', 'main')
                subprocess.run(['git', 'fetch', 'origin'], cwd=str(bot_dir),
                               capture_output=True, timeout=60)
                subprocess.run(['git', 'reset', '--hard', f'origin/{branch}'],
                               cwd=str(bot_dir), capture_output=True, timeout=30)
                _snapshot_commits(safe, bot_dir)
                result = _redeploy_git_bot(pname, bot_dir, breg)
                sched['last_run'] = now
                sched['next_run'] = _next_run_from_interval(sched['interval_hours'])
                _save_scheduled()
                if result['status'] == 'success':
                    _send_alert(pname, "⏰ Scheduled auto-deploy complete",
                                f"Next: {sched['interval_hours']}h")
                else:
                    _send_alert(pname, "⏰ Scheduled deploy failed", result['message'])
            except Exception as e:
                logger.error(f"scheduled deploy {pname}: {e}")
                _send_alert(pname, "⏰ Scheduled deploy error", str(e))

        eventlet.sleep(60)


def _start_sched_thread():
    global _sched_thread
    if not _sched_thread:
        _sched_thread = eventlet.spawn(_scheduled_deploy_loop)


@app.route('/system/capabilities', methods=['GET'])
def system_capabilities():
    """Return what features are available on this host."""
    import shutil
    docker_bin  = shutil.which('docker')
    podman_bin  = shutil.which('podman')
    runtime     = 'docker' if docker_bin else ('podman' if podman_bin else None)
    return jsonify({
        "docker_available": _docker_available(),
        "container_runtime": runtime,   # 'docker', 'podman', or null
        "git_available": bool(shutil.which('git')),
    }), 200


@app.errorhandler(Exception)
def handle_error(e):
    logger.error(f"Unhandled: {e}")
    return jsonify({"status":"error","message":"Internal server error"}), 500


# ════════════════════════════════════════════════════════════════
#  Entry point — run directly with: python3 worker.py
# ════════════════════════════════════════════════════════════════

if __name__ == '__main__':
    import os as _os
    _port = int(_os.environ.get('PORT', 5000))
    print(f"[BotClusters Enhanced] Starting on port {_port}", flush=True)
    logger.info(f"Starting BotClusters Enhanced on port {_port}")
    _os.makedirs(SUPERVISOR_LOG_DIR, exist_ok=True)
    _os.makedirs('/app', exist_ok=True)
    socketio.run(
        app,
        host='0.0.0.0',
        port=_port,
        debug=False,
        use_reloader=False,
        allow_unsafe_werkzeug=True,
    )
