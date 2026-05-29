import eventlet
eventlet.monkey_patch()

import os
import json
import signal
import subprocess
import re
import shutil
from datetime import datetime
from functools import wraps
from pathlib import Path
import logging
import time
import threading
import configparser
from collections import defaultdict

from app import app
from flask import (
    render_template, request, jsonify, Response,
    send_file, redirect, url_for, session, flash, stream_with_context
)
from flask_socketio import SocketIO, emit

# ── Logging — stream only (no file lock issues in eventlet) ────
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
)
logger = logging.getLogger(__name__)

app.config['SECRET_KEY'] = os.environ.get('SECRET_KEY', os.urandom(24))

socketio = SocketIO(
    app,
    async_mode='eventlet',
    cors_allowed_origins='*',
    ping_timeout=60,
    ping_interval=25,
)

SUPERVISOR_LOG_DIR   = '/var/log/supervisor'
SUPERVISORD_CONF_DIR = '/etc/supervisor/conf.d'
STATUS_CHECK_INTERVAL     = 2
MAX_STATUS_CHECK_ATTEMPTS = 10
# Temporary store of configs for stopped-but-not-removed processes
TEMP_SUPERVISOR_CONFIGS = {}

FAILURE_COUNTS          = defaultdict(int)
MAX_FAILURES_BEFORE_PAUSE = 5
PAUSED_BY_SYSTEM        = set()

CRON_RESTART_INTERVAL = int(os.environ.get('CRON_RESTART_HOURS', 0))
_cron_thread          = None

# ── Bot web-panel registry ─────────────────────────────────────
BOT_PANELS      = {}
BOT_PANELS_FILE = '/app/bot_panels.json'


def load_bot_panels():
    global BOT_PANELS
    try:
        if Path(BOT_PANELS_FILE).exists():
            with open(BOT_PANELS_FILE) as f:
                BOT_PANELS = json.load(f)
    except Exception as e:
        logger.error(f'Error loading bot panels: {e}')
        BOT_PANELS = {}


def save_bot_panels():
    try:
        Path(BOT_PANELS_FILE).parent.mkdir(parents=True, exist_ok=True)
        with open(BOT_PANELS_FILE, 'w') as f:
            json.dump(BOT_PANELS, f, indent=2)
    except Exception as e:
        logger.error(f'Error saving bot panels: {e}')


load_bot_panels()

# ── Dynamic bots persistence ───────────────────────────────────
DYNAMIC_BOTS_FILE = '/app/dynamic_bots.json'


def load_dynamic_bots():
    try:
        if Path(DYNAMIC_BOTS_FILE).exists():
            with open(DYNAMIC_BOTS_FILE) as f:
                return json.load(f)
    except Exception as e:
        logger.error(f'Error loading dynamic bots: {e}')
    return []


def save_dynamic_bots(bots):
    try:
        Path(DYNAMIC_BOTS_FILE).parent.mkdir(parents=True, exist_ok=True)
        with open(DYNAMIC_BOTS_FILE, 'w') as f:
            json.dump(bots, f, indent=2)
    except Exception as e:
        logger.error(f'Error saving dynamic bots: {e}')


# ── Helpers ────────────────────────────────────────────────────

def parse_supervisor_status(status_line):
    try:
        parts = status_line.strip().split()
        if len(parts) >= 2:
            name   = parts[0]
            status = parts[1]
            pid_match    = re.search(r'pid (\d+)',       status_line)
            uptime_match = re.search(r'uptime ([\d:]+)', status_line)
            pid    = pid_match.group(1) if pid_match else None
            paused = bool(pid and is_process_paused(pid))
            return {
                'name':   name,
                'status': status,
                'pid':    pid,
                'uptime': uptime_match.group(1) if uptime_match else '0:00:00',
                'paused': paused,
            }
    except Exception as e:
        logger.error(f'Error parsing supervisor status line: {e}')
    return None


def is_process_paused(pid):
    try:
        with open(f'/proc/{pid}/status') as f:
            for line in f:
                if line.startswith('State:') and '\tT' in line:
                    return True
    except Exception:
        pass
    return False


def run_supervisor_command(command, process_name=None, timeout=30):
    try:
        cmd = ['supervisorctl']
        if command:
            cmd.append(command)
        if process_name:
            cmd.append(process_name)
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        if result.returncode == 0:
            return {'status': 'success', 'message': result.stdout.strip()}
        # supervisorctl returns non-zero for some informational states — check stderr
        combined = (result.stdout + result.stderr).strip()
        return {'status': 'error', 'message': combined}
    except subprocess.TimeoutExpired:
        return {'status': 'error', 'message': f'Command timed out after {timeout}s'}
    except Exception as e:
        return {'status': 'error', 'message': str(e)}


def verify_process_status(process_name, expected_status=None):
    try:
        result = run_supervisor_command('status', process_name)
        # supervisorctl status exits 0 for RUNNING, non-zero for others — treat both as valid data
        msg = result['message']
        if not msg:
            return None
        if expected_status:
            return expected_status in msg
        return msg
    except Exception as e:
        logger.error(f'Error verifying process status: {e}')
        return None


def pause_process(process_name):
    result = run_supervisor_command('status', process_name)
    msg = result.get('message', '')
    proc = parse_supervisor_status(msg) if msg else None
    if proc and proc.get('pid'):
        try:
            os.kill(int(proc['pid']), signal.SIGSTOP)
            return {'status': 'success', 'message': f'Paused {process_name}'}
        except Exception as e:
            return {'status': 'error', 'message': str(e)}
    return {'status': 'error', 'message': 'Process not running or PID not found'}


def resume_process(process_name):
    result = run_supervisor_command('status', process_name)
    msg = result.get('message', '')
    proc = parse_supervisor_status(msg) if msg else None
    if proc and proc.get('pid'):
        try:
            os.kill(int(proc['pid']), signal.SIGCONT)
            return {'status': 'success', 'message': f'Resumed {process_name}'}
        except Exception as e:
            return {'status': 'error', 'message': str(e)}
    return {'status': 'error', 'message': 'Process not running or PID not found'}


def delete_supervisor_logs(process_name):
    for suffix in ['_out.log', '_err.log', '_combined.log']:
        p = Path(SUPERVISOR_LOG_DIR) / f'{process_name}{suffix}'
        if p.exists():
            try:
                p.unlink()
            except Exception:
                pass


def thoroughly_cleanup(process_name):
    subprocess.run(f'pkill -f {re.escape(process_name)}', shell=True)
    config_path = Path(SUPERVISORD_CONF_DIR) / f'{process_name.replace(" ", "_")}.conf'
    if config_path.exists():
        config = configparser.ConfigParser()
        config.read(config_path)
        section = 'program:' + process_name
        if section in config:
            directory = config[section].get('directory')
            if directory and Path(directory).exists():
                for root, dirs, files in os.walk(directory):
                    for d in dirs:
                        if d == '__pycache__':
                            pycache_dir = Path(root) / d
                            for f in pycache_dir.glob('*.pyc'):
                                f.unlink(missing_ok=True)
                            try:
                                pycache_dir.rmdir()
                            except Exception:
                                pass
                    for f in files:
                        if f.endswith('.pyc'):
                            (Path(root) / f).unlink(missing_ok=True)


def update_process_code(process_name, config_content=None):
    try:
        cfg = config_content
        if not cfg:
            config_path = Path(SUPERVISORD_CONF_DIR) / f'{process_name.replace(" ", "_")}.conf'
            if config_path.exists():
                cfg = config_path.read_text()
        if cfg:
            config = configparser.ConfigParser()
            config.read_string(cfg)
            section = 'program:' + process_name
            if section in config:
                directory = config[section].get('directory')
                if directory and Path(directory).exists():
                    subprocess.run(['git', 'pull'], cwd=directory, check=False)
    except Exception as e:
        logger.error(f'Error updating code for {process_name}: {e}')


def _find_slug_for_process(process_name):
    for slug, info in BOT_PANELS.items():
        if info.get('process_name') == process_name:
            return slug
    return None


def broadcast_status_update():
    try:
        with app.app_context():
            status = run_supervisor_command('status')
            # "status" returns non-zero when any process is not RUNNING — that is normal
            msg = status.get('message', '')
            processes = []
            for proc_line in msg.splitlines():
                parsed = parse_supervisor_status(proc_line)
                if not parsed:
                    continue
                pname = parsed['name']
                if parsed['status'] in ('FATAL', 'BACKOFF', 'EXITED'):
                    FAILURE_COUNTS[pname] += 1
                    if FAILURE_COUNTS[pname] >= MAX_FAILURES_BEFORE_PAUSE:
                        PAUSED_BY_SYSTEM.add(pname)
                    parsed['auto_paused'] = pname in PAUSED_BY_SYSTEM
                else:
                    if parsed['status'] == 'RUNNING':
                        FAILURE_COUNTS[pname] = 0
                        PAUSED_BY_SYSTEM.discard(pname)
                    parsed['auto_paused'] = pname in PAUSED_BY_SYSTEM
                parsed['panel_slug'] = _find_slug_for_process(pname)
                processes.append(parsed)

            socketio.emit('status_update', {
                'status': 'success',
                'processes': processes,
                'timestamp': datetime.utcnow().isoformat(),
            }, broadcast=True)
            return True
    except Exception as e:
        logger.error(f'Error broadcasting status update: {e}')
        return False


# ── Find a working Python executable ──────────────────────────

def find_python(version=None):
    """Return a python executable that actually responds to --version."""
    candidates = []
    if version:
        major_minor = '.'.join(version.strip().split('.')[:2])
        candidates.append(f'python{major_minor}')
    candidates += ['python3', 'python']
    for name in candidates:
        exe = shutil.which(name)
        if not exe:
            continue
        try:
            r = subprocess.run([exe, '--version'], capture_output=True, timeout=5)
            if r.returncode == 0:
                logger.info(f'Using Python: {exe}')
                return exe
        except Exception:
            continue
    return 'python3'


# ── Auth ───────────────────────────────────────────────────────

users = {'admin': os.environ.get('ADMIN_PASSWORD', 'password123')}


def login_required(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        if 'logged_in' not in session:
            return redirect(url_for('login'))
        return f(*args, **kwargs)
    return decorated


@app.route('/login', methods=['GET', 'POST'])
def login():
    if request.method == 'POST':
        username = request.form.get('username', '')
        password = request.form.get('password', '')
        if username in users and users[username] == password:
            session['logged_in'] = True
            return redirect(url_for('cluster'))
        flash('Invalid credentials. Please try again.')
    return render_template('login.html')


@app.route('/logout')
def logout():
    session.pop('logged_in', None)
    return redirect(url_for('login'))


# ── Main dashboard ─────────────────────────────────────────────

@app.route('/')
@login_required
def cluster():
    return render_template('cluster.html')


# ── Bot web-panel reverse proxy ────────────────────────────────

@app.route('/bot<int:bot_num>', defaults={'path': ''})
@app.route('/bot<int:bot_num>/<path:path>')
@login_required
def bot_panel_proxy(bot_num, path):
    slug = f'bot{bot_num}'
    info = BOT_PANELS.get(slug)
    if not info:
        return (f"<h2>No web panel registered for {slug}.</h2>"
                f"<p><a href='/'>← Back to dashboard</a></p>"), 404

    port = info.get('port')
    if not port:
        return f'<h2>No port configured for {slug}.</h2>', 500

    target_url = f'http://127.0.0.1:{port}/{path}'
    if request.query_string:
        target_url += '?' + request.query_string.decode()

    try:
        import requests as req
        proxied = req.request(
            method=request.method,
            url=target_url,
            headers={k: v for k, v in request.headers
                     if k.lower() not in ('host', 'content-length')},
            data=request.get_data(),
            cookies=request.cookies,
            allow_redirects=False,
            timeout=10,
        )
        resp_headers = dict(proxied.headers)
        if 'Location' in resp_headers:
            loc = resp_headers['Location']
            if loc.startswith('/'):
                resp_headers['Location'] = f'/bot{bot_num}{loc}'
        excluded = {'content-encoding', 'transfer-encoding', 'connection'}
        clean_headers = {k: v for k, v in resp_headers.items()
                         if k.lower() not in excluded}
        return Response(proxied.content,
                        status=proxied.status_code,
                        headers=clean_headers)
    except Exception as e:
        logger.error(f'Proxy error for {slug}: {e}')
        return (f'<h2>Could not reach the bot panel (port {port}).</h2>'
                f'<p>{e}</p><p><a href="/">← Back</a></p>'), 502


# ── Bot panel registration API ─────────────────────────────────

@app.route('/api/panels', methods=['GET'])
@login_required
def list_panels():
    return jsonify({'status': 'success', 'panels': BOT_PANELS})


@app.route('/api/panels/<slug>', methods=['POST'])
@login_required
def register_panel(slug):
    if not re.match(r'^bot\d+$', slug):
        return jsonify({'status': 'error', 'message': 'Slug must be botN'}), 400
    data = request.get_json(silent=True) or {}
    port = data.get('port')
    if not port:
        return jsonify({'status': 'error', 'message': 'port is required'}), 400
    BOT_PANELS[slug] = {
        'port': int(port),
        'process_name': data.get('process_name', ''),
        'label': data.get('label', slug),
    }
    save_bot_panels()
    return jsonify({'status': 'success', 'slug': slug, 'info': BOT_PANELS[slug]})


@app.route('/api/panels/<slug>', methods=['DELETE'])
@login_required
def unregister_panel(slug):
    if slug not in BOT_PANELS:
        return jsonify({'status': 'error', 'message': 'Panel not found'}), 404
    del BOT_PANELS[slug]
    save_bot_panels()
    return jsonify({'status': 'success', 'message': f'Removed panel {slug}'})


# ── Add Bot — streaming build logs via SSE ─────────────────────
# The actual build runs in a background thread and pushes log lines
# to the client via Server-Sent Events on /api/bots/stream/<job_id>.

import uuid
from queue import Queue, Empty

_build_jobs: dict[str, Queue] = {}   # job_id -> Queue of log strings


def _run_build(job_id: str, botname: str, git_url: str, branch: str,
               run_command: str, env: dict, python_version,
               panel_port, panel_label: str):
    """Execute the full bot setup in a background thread, pushing log lines."""
    q = _build_jobs[job_id]

    def log(msg: str):
        logger.info(msg)
        q.put(('log', msg))

    def done(success: bool, payload: dict):
        q.put(('done', {'success': success, **payload}))

    bot_dir  = Path('/app') / botname
    venv_dir = bot_dir / 'venv'

    try:
        # ── Clone ──────────────────────────────────────────────
        if bot_dir.exists():
            log(f'Removing existing directory {bot_dir} …')
            shutil.rmtree(bot_dir)

        log(f'Cloning {git_url} (branch: {branch}) …')
        clone = subprocess.run(
            ['git', 'clone', '-b', branch, '--single-branch', git_url, str(bot_dir)],
            capture_output=True, text=True
        )
        for line in (clone.stdout + clone.stderr).splitlines():
            if line.strip():
                log(line)
        if clone.returncode != 0:
            done(False, {'message': f'git clone failed (exit {clone.returncode})'})
            return

        # ── Resolve Python ─────────────────────────────────────
        py_exe = find_python(python_version)
        log(f'Using Python: {py_exe}')

        # ── Create venv ────────────────────────────────────────
        req_file = bot_dir / 'requirements.txt'
        if req_file.exists():
            log('Creating virtual environment …')
            venv_result = subprocess.run(
                [py_exe, '-m', 'venv', str(venv_dir)],
                capture_output=True, text=True
            )
            if venv_result.returncode != 0:
                log(f'venv failed: {venv_result.stderr.strip()}')
                log('Trying to install python3-venv …')
                subprocess.run(['apt-get', 'install', '-y', 'python3-venv'],
                               capture_output=True)
                subprocess.run(['dnf', 'install', '-y', 'python3-venv'],
                               capture_output=True)
                venv_retry = subprocess.run(
                    [py_exe, '-m', 'venv', str(venv_dir)],
                    capture_output=True, text=True
                )
                if venv_retry.returncode != 0:
                    done(False, {'message': f'venv creation failed: {venv_retry.stderr.strip()}'})
                    return
                log('Virtual environment created successfully.')
            else:
                log('Virtual environment created.')

            # ── pip install ────────────────────────────────────
            log('Installing dependencies …')
            pip = str(venv_dir / 'bin' / 'pip')
            pip_proc = subprocess.Popen(
                [pip, 'install', '--no-cache-dir', '-r', str(req_file)],
                stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                text=True
            )
            for line in pip_proc.stdout:
                line = line.rstrip()
                if line:
                    log(line)
            pip_proc.wait()
            if pip_proc.returncode != 0:
                done(False, {'message': f'pip install failed (exit {pip_proc.returncode})'})
                return
            log('Dependencies installed successfully.')
        else:
            log('No requirements.txt found — skipping pip install.')
            # Still create venv for running the bot
            subprocess.run([py_exe, '-m', 'venv', str(venv_dir)], capture_output=True)

        # ── Write supervisord config ───────────────────────────
        bot_file = bot_dir / run_command
        py_bin   = str(venv_dir / 'bin' / 'python3')
        if run_command.endswith('.sh'):
            command = f'bash {bot_file}'
        elif run_command.endswith('.py'):
            command = f'{py_bin} {bot_file}'
        else:
            command = f'{py_bin} -m {Path(run_command).stem}'

        env_line  = ','.join([f'{k}="{v}"' for k, v in env.items()]) if env else ''
        conf_path = Path(SUPERVISORD_CONF_DIR) / f'{botname}.conf'
        conf_content = (
            f'[program:{botname}]\n'
            f'command={command}\n'
            f'directory={bot_dir}\n'
            f'autostart=true\n'
            f'autorestart=true\n'
            f'startretries=12\n'
            f'stderr_logfile={SUPERVISOR_LOG_DIR}/{botname}_err.log\n'
            f'stdout_logfile={SUPERVISOR_LOG_DIR}/{botname}_out.log\n'
        )
        if env_line:
            conf_content += f'environment={env_line}\n'
        conf_path.write_text(conf_content)
        log('Supervisor config written.')

        # ── Register & start ───────────────────────────────────
        log('Registering with supervisord …')
        subprocess.run(['supervisorctl', 'reread'], capture_output=True)
        subprocess.run(['supervisorctl', 'update'], capture_output=True)

        # Register panel if port given
        slug = None
        if panel_port:
            existing = [int(s[3:]) for s in BOT_PANELS if re.match(r'^bot\d+$', s)]
            next_num = max(existing, default=0) + 1
            slug = f'bot{next_num}'
            BOT_PANELS[slug] = {
                'port': int(panel_port),
                'process_name': botname,
                'label': panel_label,
            }
            save_bot_panels()
            log(f'Web panel registered at /{slug} (port {panel_port})')

        # Persist
        bots = load_dynamic_bots()
        # Avoid duplicates if re-adding
        bots = [b for b in bots if b.get('botname') != botname]
        bots.append({
            'botname': botname, 'git_url': git_url, 'branch': branch,
            'run_command': run_command, 'env': env,
            'python_version': python_version, 'panel_port': panel_port,
            'panel_slug': slug,
        })
        save_dynamic_bots(bots)

        log(f'Bot {botname} started successfully!')
        broadcast_status_update()
        done(True, {'message': f'Bot {botname} added', 'panel_slug': slug})

    except Exception as e:
        logger.exception(f'Build job {job_id} failed')
        done(False, {'message': str(e)})


@app.route('/api/bots', methods=['POST'])
@login_required
def add_bot():
    """
    Start a build job and return a job_id.
    Body: { botname, git_url, branch, run_command, env?, python_version?, panel_port?, panel_label? }
    """
    data = request.get_json(silent=True) or {}
    for field in ['botname', 'git_url', 'branch', 'run_command']:
        if not data.get(field):
            return jsonify({'status': 'error', 'message': f'Missing field: {field}'}), 400

    botname      = re.sub(r'[^a-zA-Z0-9_\-]', '_', data['botname'].strip())
    git_url      = data['git_url']
    branch       = data['branch']
    run_command  = data['run_command']
    env          = data.get('env', {})
    python_version = data.get('python_version') or None
    panel_port   = data.get('panel_port') or None
    panel_label  = data.get('panel_label') or botname

    job_id = str(uuid.uuid4())
    _build_jobs[job_id] = Queue()

    t = threading.Thread(
        target=_run_build,
        args=(job_id, botname, git_url, branch, run_command,
              env, python_version, panel_port, panel_label),
        daemon=True,
    )
    t.start()

    return jsonify({'status': 'started', 'job_id': job_id}), 202


@app.route('/api/bots/stream/<job_id>')
@login_required
def stream_build_log(job_id):
    """SSE endpoint — streams build log lines until done."""
    if job_id not in _build_jobs:
        return jsonify({'error': 'Unknown job'}), 404

    def generate():
        q = _build_jobs[job_id]
        try:
            while True:
                try:
                    kind, data = q.get(timeout=60)
                except Empty:
                    yield 'data: {"type":"heartbeat"}\n\n'
                    continue

                if kind == 'log':
                    payload = json.dumps({'type': 'log', 'line': data})
                    yield f'data: {payload}\n\n'
                elif kind == 'done':
                    payload = json.dumps({'type': 'done', **data})
                    yield f'data: {payload}\n\n'
                    break
        finally:
            _build_jobs.pop(job_id, None)

    return Response(
        stream_with_context(generate()),
        mimetype='text/event-stream',
        headers={'Cache-Control': 'no-cache', 'X-Accel-Buffering': 'no'},
    )


@app.route('/api/bots', methods=['GET'])
@login_required
def list_dynamic_bots():
    return jsonify({'status': 'success', 'bots': load_dynamic_bots()})


@app.route('/api/bots/<botname>', methods=['DELETE'])
@login_required
def remove_bot(botname):
    botname = re.sub(r'[^a-zA-Z0-9_\-]', '_', botname.strip())
    if not botname:
        return jsonify({'status': 'error', 'message': 'Invalid botname'}), 400

    run_supervisor_command('stop', botname)
    time.sleep(1)

    conf_path = Path(SUPERVISORD_CONF_DIR) / f'{botname}.conf'
    if conf_path.exists():
        conf_path.unlink()

    subprocess.run(['supervisorctl', 'reread'], capture_output=True)
    subprocess.run(['supervisorctl', 'update'], capture_output=True)

    bot_dir = Path('/app') / botname
    if bot_dir.exists():
        shutil.rmtree(bot_dir)

    delete_supervisor_logs(botname)

    for slug, info in list(BOT_PANELS.items()):
        if info.get('process_name') == botname:
            del BOT_PANELS[slug]
    save_bot_panels()

    bots = [b for b in load_dynamic_bots() if b.get('botname') != botname]
    save_dynamic_bots(bots)

    broadcast_status_update()
    return jsonify({'status': 'success', 'message': f'Bot {botname} removed'})


# ── Supervisor process management ─────────────────────────────

@app.route('/supervisor/status', methods=['GET'])
@login_required
def list_supervisor_processes():
    status = run_supervisor_command('status')
    processes = []
    for line in status.get('message', '').splitlines():
        p = parse_supervisor_status(line)
        if p:
            p['panel_slug'] = _find_slug_for_process(p['name'])
            processes.append(p)
    return jsonify({'status': 'success', 'processes': processes}), 200


@app.route('/supervisor/pause/<process_name>', methods=['POST'])
@login_required
def pause_supervisor_process(process_name):
    result = pause_process(process_name)
    if result['status'] == 'success':
        broadcast_status_update()
    return jsonify(result), 200 if result['status'] == 'success' else 500


@app.route('/supervisor/resume/<process_name>', methods=['POST'])
@login_required
def resume_supervisor_process(process_name):
    result = resume_process(process_name)
    if result['status'] == 'success':
        broadcast_status_update()
    return jsonify(result), 200 if result['status'] == 'success' else 500


@app.route('/supervisor/<action>/<process_name>', methods=['POST'])
@login_required
def manage_supervisor_process(action, process_name):
    if action not in ('start', 'stop', 'restart'):
        return jsonify({'status': 'error', 'message': 'Invalid action'}), 400
    if not re.match(r'^[a-zA-Z0-9_\- ]+$', process_name):
        return jsonify({'status': 'error', 'message': 'Invalid process name'}), 400

    try:
        initial_status = verify_process_status(process_name)
        # initial_status is the raw status line or None
        if initial_status is None:
            return jsonify({'status': 'error',
                            'message': f'Process {process_name} not found'}), 404

        config_path    = Path(SUPERVISORD_CONF_DIR) / f'{process_name.replace(" ", "_")}.conf'
        expected_status = 'RUNNING'
        result          = None

        if action == 'stop':
            if 'RUNNING' not in str(initial_status):
                return jsonify({'status': 'error',
                                'message': f'{process_name} is not running'}), 400
            result = run_supervisor_command('stop', process_name)
            expected_status = 'STOPPED'
            if result['status'] == 'success' and config_path.exists():
                TEMP_SUPERVISOR_CONFIGS[process_name] = config_path.read_text()
                config_path.unlink()
                subprocess.run(['supervisorctl', 'reread'], capture_output=True)
                subprocess.run(['supervisorctl', 'update'], capture_output=True)

        elif action == 'start':
            if process_name in TEMP_SUPERVISOR_CONFIGS:
                config_content = TEMP_SUPERVISOR_CONFIGS.pop(process_name)
                update_process_code(process_name, config_content)
                config_path.write_text(config_content)
                subprocess.run(['supervisorctl', 'reread'], capture_output=True)
                subprocess.run(['supervisorctl', 'update'], capture_output=True)
            else:
                update_process_code(process_name)
            result = run_supervisor_command('start', process_name)

        elif action == 'restart':
            thoroughly_cleanup(process_name)
            delete_supervisor_logs(process_name)
            if not config_path.exists():
                return jsonify({'status': 'error',
                                'message': f'Config not found for {process_name}'}), 404
            config_content = config_path.read_text()
            run_supervisor_command('stop', process_name)
            config_path.unlink()
            subprocess.run(['supervisorctl', 'reread'], capture_output=True)
            subprocess.run(['supervisorctl', 'update'], capture_output=True)
            time.sleep(2)
            update_process_code(process_name, config_content)
            config_path.write_text(config_content)
            subprocess.run(['supervisorctl', 'reread'], capture_output=True)
            subprocess.run(['supervisorctl', 'update'], capture_output=True)
            result = run_supervisor_command('start', process_name)

        if not result or result['status'] != 'success':
            return jsonify(result or {'status': 'error', 'message': 'Unknown error'}), 500

        for _ in range(MAX_STATUS_CHECK_ATTEMPTS):
            time.sleep(STATUS_CHECK_INTERVAL)
            current = verify_process_status(process_name)
            if action == 'stop' and (current is None or 'STOPPED' in str(current)):
                broadcast_status_update()
                return jsonify({'status': 'success',
                                'message': f'Successfully stopped {process_name}'}), 200
            if current and expected_status in str(current):
                broadcast_status_update()
                return jsonify({'status': 'success',
                                'message': f'Successfully {action}ed {process_name}'}), 200

        return jsonify({'status': 'error',
                        'message': f'Process did not reach {expected_status}'}), 500

    except Exception as e:
        logger.exception(f'Error managing process {process_name}')
        return jsonify({'status': 'error', 'message': str(e)}), 500


@app.route('/supervisor/log/<process_name>', methods=['GET'])
@login_required
def download_supervisor_log(process_name):
    if not re.match(r'^[a-zA-Z0-9_\- ]+$', process_name):
        return jsonify({'status': 'error', 'message': 'Invalid process name'}), 400
    stdout_log   = Path(SUPERVISOR_LOG_DIR) / f'{process_name}_out.log'
    stderr_log   = Path(SUPERVISOR_LOG_DIR) / f'{process_name}_err.log'
    combined_log = Path(SUPERVISOR_LOG_DIR) / f'{process_name}_combined.log'
    if not stdout_log.exists() and not stderr_log.exists():
        return jsonify({'status': 'error', 'message': 'No log files found'}), 404
    with combined_log.open('w') as out:
        out.write(f'=== Combined logs for {process_name} ===\n')
        out.write(f'Generated at: {datetime.utcnow().isoformat()}\n\n')
        if stdout_log.exists():
            out.write('=== STDOUT ===\n')
            out.write(stdout_log.read_text(errors='replace'))
            out.write('\n\n')
        if stderr_log.exists():
            out.write('=== STDERR ===\n')
            out.write(stderr_log.read_text(errors='replace'))
    return send_file(str(combined_log), mimetype='text/plain',
                     as_attachment=True,
                     download_name=f'{process_name}_combined.log')


@app.route('/supervisor/clear_failure/<process_name>', methods=['POST'])
@login_required
def clear_failure(process_name):
    FAILURE_COUNTS[process_name] = 0
    PAUSED_BY_SYSTEM.discard(process_name)
    run_supervisor_command('start', process_name)
    broadcast_status_update()
    return jsonify({'status': 'success',
                    'message': f'Cleared failure state for {process_name}'})


# ── Log stream page ────────────────────────────────────────────

@app.route('/logstream')
@login_required
def logstream_page():
    return render_template('logstream.html')


@app.route('/logstream/stream')
@login_required
def logstream_sse():
    def generate():
        log_dir   = Path(SUPERVISOR_LOG_DIR)
        positions = {}
        while True:
            for log_file in sorted(log_dir.glob('*.log')):
                if '_combined' in log_file.name:
                    continue
                try:
                    pos  = positions.get(log_file.name, 0)
                    size = log_file.stat().st_size
                    if size < pos:
                        pos = 0
                    if size > pos:
                        with log_file.open('r', errors='replace') as fh:
                            fh.seek(pos)
                            new_data = fh.read()
                            positions[log_file.name] = fh.tell()
                        if new_data.strip():
                            yield f'data: {json.dumps({"file": log_file.name, "data": new_data})}\n\n'
                except Exception:
                    pass
            eventlet.sleep(1)

    return Response(stream_with_context(generate()),
                    mimetype='text/event-stream',
                    headers={'Cache-Control': 'no-cache', 'X-Accel-Buffering': 'no'})


# ── Cron ───────────────────────────────────────────────────────

@app.route('/config/cron', methods=['GET', 'POST'])
@login_required
def config_cron():
    global CRON_RESTART_INTERVAL, _cron_thread
    if request.method == 'POST':
        data  = request.get_json(silent=True) or {}
        hours = max(0, int(data.get('hours', 0)))
        CRON_RESTART_INTERVAL = hours
        os.environ['CRON_RESTART_HOURS'] = str(hours)
        _start_cron_thread()
        return jsonify({'status': 'success', 'hours': hours})
    return jsonify({'status': 'success', 'hours': CRON_RESTART_INTERVAL})


# ── SocketIO events ────────────────────────────────────────────

@socketio.on('connect')
def handle_connect():
    emit('connected', {'data': 'Connected'})
    broadcast_status_update()


@socketio.on('disconnect')
def handle_disconnect():
    logger.info('Client disconnected')


@socketio.on('request_status')
def handle_status_request():
    try:
        status    = run_supervisor_command('status')
        processes = []
        for proc in status.get('message', '').splitlines():
            parsed = parse_supervisor_status(proc)
            if not parsed:
                continue
            pname = parsed['name']
            if parsed['status'] in ('FATAL', 'BACKOFF', 'EXITED'):
                FAILURE_COUNTS[pname] += 1
                if FAILURE_COUNTS[pname] >= MAX_FAILURES_BEFORE_PAUSE:
                    PAUSED_BY_SYSTEM.add(pname)
                parsed['auto_paused'] = pname in PAUSED_BY_SYSTEM
            else:
                if parsed['status'] == 'RUNNING':
                    FAILURE_COUNTS[pname] = 0
                    PAUSED_BY_SYSTEM.discard(pname)
                parsed['auto_paused'] = pname in PAUSED_BY_SYSTEM
            parsed['panel_slug'] = _find_slug_for_process(pname)
            processes.append(parsed)
        emit('status_update', {
            'status': 'success',
            'processes': processes,
            'timestamp': datetime.utcnow().isoformat(),
        })
    except Exception as e:
        emit('status_update', {'status': 'error', 'message': str(e), 'processes': []})


# ── Background threads ─────────────────────────────────────────

def _cron_restart_loop():
    while True:
        interval = CRON_RESTART_INTERVAL
        if interval <= 0:
            eventlet.sleep(60)
            continue
        eventlet.sleep(interval * 3600)
        if CRON_RESTART_INTERVAL <= 0:
            continue
        try:
            run_supervisor_command('restart', 'all')
            broadcast_status_update()
        except Exception as e:
            logger.error(f'Cron restart error: {e}')


def _start_cron_thread():
    global _cron_thread
    if _cron_thread is None:
        _cron_thread = eventlet.spawn(_cron_restart_loop)


def _auto_delete_logs_loop():
    while True:
        eventlet.sleep(24 * 3600)
        for log_file in Path(SUPERVISOR_LOG_DIR).glob('*.log'):
            try:
                log_file.unlink()
            except Exception:
                pass


_log_cleanup_thread = None


def _start_log_cleanup_thread():
    global _log_cleanup_thread
    if _log_cleanup_thread is None:
        _log_cleanup_thread = eventlet.spawn(_auto_delete_logs_loop)


@app.errorhandler(Exception)
def handle_error(e):
    logger.error(f'Unhandled error: {e}')
    return jsonify({'status': 'error', 'message': 'Internal server error'}), 500


_start_cron_thread()
_start_log_cleanup_thread()
