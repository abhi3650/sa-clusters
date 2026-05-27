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
    Flask, render_template, request, jsonify, Response,
    send_file, abort, redirect, url_for, session, flash, stream_with_context
)
from flask_socketio import SocketIO, emit

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    filename='app.log'
)
logger = logging.getLogger(__name__)

app.config['SECRET_KEY'] = os.urandom(24)

socketio = SocketIO(
    app,
    async_mode='eventlet',
    cors_allowed_origins="*",
    ping_timeout=60,
    ping_interval=25
)

SUPERVISOR_LOG_DIR = "/var/log/supervisor"
SUPERVISORD_CONF_DIR = "/etc/supervisor/conf.d"
STATUS_CHECK_INTERVAL = 2
MAX_STATUS_CHECK_ATTEMPTS = 10
TEMP_SUPERVISOR_CONFIGS = {}

FAILURE_COUNTS = defaultdict(int)
MAX_FAILURES_BEFORE_PAUSE = 5
PAUSED_BY_SYSTEM = set()

CRON_RESTART_INTERVAL = int(os.environ.get('CRON_RESTART_HOURS', 0))
_cron_thread = None

# ── Bot web-panel registry: { "bot1": {"port": 3001, "process_name": "xxx"} }
BOT_PANELS = {}
BOT_PANELS_FILE = "/app/bot_panels.json"

def load_bot_panels():
    global BOT_PANELS
    try:
        if Path(BOT_PANELS_FILE).exists():
            with open(BOT_PANELS_FILE) as f:
                BOT_PANELS = json.load(f)
    except Exception as e:
        logger.error(f"Error loading bot panels: {e}")
        BOT_PANELS = {}

def save_bot_panels():
    try:
        Path(BOT_PANELS_FILE).parent.mkdir(parents=True, exist_ok=True)
        with open(BOT_PANELS_FILE, 'w') as f:
            json.dump(BOT_PANELS, f, indent=2)
    except Exception as e:
        logger.error(f"Error saving bot panels: {e}")

load_bot_panels()

# ── Dynamic config management ──────────────────────────────────
DYNAMIC_BOTS_FILE = "/app/dynamic_bots.json"

def load_dynamic_bots():
    try:
        if Path(DYNAMIC_BOTS_FILE).exists():
            with open(DYNAMIC_BOTS_FILE) as f:
                return json.load(f)
    except Exception as e:
        logger.error(f"Error loading dynamic bots: {e}")
    return []

def save_dynamic_bots(bots):
    try:
        Path(DYNAMIC_BOTS_FILE).parent.mkdir(parents=True, exist_ok=True)
        with open(DYNAMIC_BOTS_FILE, 'w') as f:
            json.dump(bots, f, indent=2)
    except Exception as e:
        logger.error(f"Error saving dynamic bots: {e}")

# ── Helpers ────────────────────────────────────────────────────

def parse_supervisor_status(status_line):
    try:
        parts = status_line.strip().split()
        if len(parts) >= 2:
            name = parts[0]
            status = parts[1]
            pid_match = re.search(r'pid (\d+)', status_line)
            uptime_match = re.search(r'uptime ([\d:]+)', status_line)
            pid = pid_match.group(1) if pid_match else None
            paused = False
            if pid and is_process_paused(pid):
                paused = True
            return {
                "name": name,
                "status": status,
                "pid": pid,
                "uptime": uptime_match.group(1) if uptime_match else "0:00:00",
                "paused": paused
            }
    except Exception as e:
        logger.error(f"Error parsing supervisor status line: {e}")
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
        cmd = ["supervisorctl"]
        if command:
            cmd.append(command)
        if process_name:
            cmd.append(process_name)
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        if result.returncode == 0:
            return {"status": "success", "message": result.stdout.strip()}
        else:
            return {"status": "error", "message": result.stderr.strip()}
    except subprocess.TimeoutExpired:
        return {"status": "error", "message": f"Command timed out after {timeout} seconds"}
    except Exception as e:
        return {"status": "error", "message": str(e)}


def verify_process_status(process_name, expected_status=None):
    try:
        result = run_supervisor_command("status", process_name)
        if result["status"] == "success":
            if expected_status:
                return expected_status in result["message"]
            return result["message"]
        return None
    except Exception as e:
        logger.error(f"Error verifying process status: {str(e)}")
        return None


def pause_process(process_name):
    result = run_supervisor_command("status", process_name)
    if result["status"] == "success":
        proc = parse_supervisor_status(result["message"])
        if proc and proc["pid"]:
            try:
                os.kill(int(proc["pid"]), signal.SIGSTOP)
                return {"status": "success", "message": f"Paused process {process_name}"}
            except Exception as e:
                return {"status": "error", "message": str(e)}
    return {"status": "error", "message": "Process not running or PID not found"}


def resume_process(process_name):
    result = run_supervisor_command("status", process_name)
    if result["status"] == "success":
        proc = parse_supervisor_status(result["message"])
        if proc and proc["pid"]:
            try:
                os.kill(int(proc["pid"]), signal.SIGCONT)
                return {"status": "success", "message": f"Resumed process {process_name}"}
            except Exception as e:
                return {"status": "error", "message": str(e)}
    return {"status": "error", "message": "Process not running or PID not found"}


def delete_supervisor_logs(process_name):
    for suffix in ["_out.log", "_err.log", "_combined.log"]:
        p = Path(SUPERVISOR_LOG_DIR) / f"{process_name}{suffix}"
        if p.exists():
            p.unlink()


def thoroughly_cleanup(process_name):
    subprocess.run(f"pkill -f {process_name}", shell=True)
    config_path = Path(SUPERVISORD_CONF_DIR) / f"{process_name.replace(' ', '_')}.conf"
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
                            for file in pycache_dir.glob('*.pyc'):
                                file.unlink()
                            try:
                                pycache_dir.rmdir()
                            except Exception:
                                pass
                    for f in files:
                        if f.endswith('.pyc'):
                            (Path(root) / f).unlink()


def update_process_code(process_name, config_content=None):
    try:
        cfg = config_content
        if not cfg:
            config_path = Path(SUPERVISORD_CONF_DIR) / f"{process_name.replace(' ', '_')}.conf"
            if config_path.exists():
                cfg = config_path.read_text()
        if cfg:
            config = configparser.ConfigParser()
            config.read_string(cfg)
            section = 'program:' + process_name
            if section in config:
                directory = config[section].get('directory')
                if directory and Path(directory).exists():
                    subprocess.run(['git', 'pull'], cwd=directory, check=True)
    except Exception as e:
        logger.error(f"Error updating code for {process_name}: {str(e)}")


def broadcast_status_update():
    try:
        with app.app_context():
            status = run_supervisor_command("status")
            if status["status"] == "success":
                processes = []
                for proc_line in status["message"].splitlines():
                    parsed = parse_supervisor_status(proc_line)
                    if parsed:
                        pname = parsed["name"]
                        if parsed["status"] in ("FATAL", "BACKOFF", "EXITED"):
                            FAILURE_COUNTS[pname] += 1
                            if FAILURE_COUNTS[pname] >= MAX_FAILURES_BEFORE_PAUSE and pname not in PAUSED_BY_SYSTEM:
                                PAUSED_BY_SYSTEM.add(pname)
                                parsed["auto_paused"] = True
                            elif pname in PAUSED_BY_SYSTEM:
                                parsed["auto_paused"] = True
                            else:
                                parsed["auto_paused"] = False
                        else:
                            if parsed["status"] == "RUNNING":
                                FAILURE_COUNTS[pname] = 0
                                PAUSED_BY_SYSTEM.discard(pname)
                            parsed["auto_paused"] = pname in PAUSED_BY_SYSTEM
                        processes.append(parsed)

                # Also inject panel info
                for p in processes:
                    slug = _find_slug_for_process(p["name"])
                    p["panel_slug"] = slug

                socketio.emit('status_update', {
                    "status": "success",
                    "processes": processes,
                    "timestamp": datetime.utcnow().isoformat()
                }, broadcast=True)
                return True
    except Exception as e:
        logger.error(f"Error broadcasting status update: {str(e)}\"")
        return False


def _find_slug_for_process(process_name):
    """Return the /botN slug if this process has a registered web panel."""
    for slug, info in BOT_PANELS.items():
        if info.get("process_name") == process_name:
            return slug
    return None


# ── Auth ───────────────────────────────────────────────────────

users = {
    "admin": os.environ.get("ADMIN_PASSWORD", "password123"),
}


def login_required(f):
    @wraps(f)
    def decorated_function(*args, **kwargs):
        if 'logged_in' not in session:
            return redirect(url_for('login'))
        return f(*args, **kwargs)
    return decorated_function


@app.route('/login', methods=['GET', 'POST'])
def login():
    if request.method == 'POST':
        username = request.form['username']
        password = request.form['password']
        if username in users and users[username] == password:
            session['logged_in'] = True
            return redirect(url_for('cluster'))
        else:
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


# ── Bot web-panel proxy ────────────────────────────────────────
# Each bot that exposes a web UI is registered with a slug (bot1, bot2, …)
# and a local port. We reverse-proxy via /botN/...

@app.route('/bot<int:bot_num>', defaults={'path': ''})
@app.route('/bot<int:bot_num>/<path:path>')
@login_required
def bot_panel_proxy(bot_num, path):
    slug = f"bot{bot_num}"
    info = BOT_PANELS.get(slug)
    if not info:
        return f"<h2>No web panel registered for {slug}.</h2><p><a href='/'>← Back to dashboard</a></p>", 404

    port = info.get("port")
    if not port:
        return f"<h2>No port configured for {slug}.</h2>", 500

    target_url = f"http://127.0.0.1:{port}/{path}"
    if request.query_string:
        target_url += "?" + request.query_string.decode()

    try:
        import requests as req
        proxied = req.request(
            method=request.method,
            url=target_url,
            headers={k: v for k, v in request.headers if k.lower() not in ('host', 'content-length')},
            data=request.get_data(),
            cookies=request.cookies,
            allow_redirects=False,
            timeout=10,
        )
        # Rewrite Location headers so redirects stay in-proxy
        resp_headers = dict(proxied.headers)
        if 'Location' in resp_headers:
            loc = resp_headers['Location']
            if loc.startswith('/'):
                resp_headers['Location'] = f"/bot{bot_num}{loc}"
        excluded = {'content-encoding', 'transfer-encoding', 'connection'}
        clean_headers = {k: v for k, v in resp_headers.items() if k.lower() not in excluded}
        return Response(proxied.content, status=proxied.status_code, headers=clean_headers)
    except Exception as e:
        logger.error(f"Proxy error for {slug}: {e}")
        return f"<h2>Could not reach the bot panel (port {port}).</h2><p>{e}</p><p><a href='/'>← Back</a></p>", 502


# ── Bot panel registration API ─────────────────────────────────

@app.route('/api/panels', methods=['GET'])
@login_required
def list_panels():
    return jsonify({"status": "success", "panels": BOT_PANELS})


@app.route('/api/panels/<slug>', methods=['POST'])
@login_required
def register_panel(slug):
    """Register or update a bot web-panel. Body: {port, process_name, label}"""
    if not re.match(r'^bot\d+$', slug):
        return jsonify({"status": "error", "message": "Slug must be botN (e.g. bot1)"}), 400
    data = request.get_json(silent=True) or {}
    port = data.get("port")
    if not port:
        return jsonify({"status": "error", "message": "port is required"}), 400
    BOT_PANELS[slug] = {
        "port": int(port),
        "process_name": data.get("process_name", ""),
        "label": data.get("label", slug),
    }
    save_bot_panels()
    return jsonify({"status": "success", "slug": slug, "info": BOT_PANELS[slug]})


@app.route('/api/panels/<slug>', methods=['DELETE'])
@login_required
def unregister_panel(slug):
    if slug not in BOT_PANELS:
        return jsonify({"status": "error", "message": "Panel not found"}), 404
    del BOT_PANELS[slug]
    save_bot_panels()
    return jsonify({"status": "success", "message": f"Removed panel {slug}"})


# ── Dynamic bot add/remove ─────────────────────────────────────

@app.route('/api/bots', methods=['GET'])
@login_required
def list_dynamic_bots():
    return jsonify({"status": "success", "bots": load_dynamic_bots()})


@app.route('/api/bots', methods=['POST'])
@login_required
def add_bot():
    """
    Add and start a new bot at runtime.
    Body: {
        botname, git_url, branch, run_command,
        env (dict, optional), python_version (optional),
        panel_port (optional int – if the bot exposes a web UI),
        panel_label (optional str)
    }
    """
    data = request.get_json(silent=True) or {}
    required = ['botname', 'git_url', 'branch', 'run_command']
    for field in required:
        if not data.get(field):
            return jsonify({"status": "error", "message": f"Missing field: {field}"}), 400

    botname = data['botname'].strip().replace(' ', '_')
    git_url = data['git_url']
    branch = data['branch']
    run_command = data['run_command']
    env = data.get('env', {})
    python_version = data.get('python_version', None)
    panel_port = data.get('panel_port', None)
    panel_label = data.get('panel_label', botname)

    # Clone & setup
    bot_dir = Path('/app') / botname
    venv_dir = bot_dir / 'venv'
    try:
        if bot_dir.exists():
            shutil.rmtree(bot_dir)
        subprocess.run(['git', 'clone', '-b', branch, '--single-branch', git_url, str(bot_dir)], check=True)

        if python_version:
            import shutil as sh
            major_minor = '.'.join(python_version.split('.')[:2])
            py_exe = sh.which(f"python{major_minor}") or sh.which("python3") or "python3"
        else:
            import shutil as sh
            py_exe = sh.which("python3") or "python3"

        req_file = bot_dir / 'requirements.txt'
        if req_file.exists():
            subprocess.run([py_exe, '-m', 'venv', str(venv_dir)], check=True)
            pip = str(venv_dir / 'bin' / 'pip')
            subprocess.run([pip, 'install', '--no-cache-dir', '-r', str(req_file)], check=True)

    except subprocess.CalledProcessError as e:
        return jsonify({"status": "error", "message": f"Setup failed: {e}"}), 500

    # Write supervisord config
    bot_file = bot_dir / run_command
    if run_command.endswith('.sh'):
        command = f"bash {bot_file}"
    elif run_command.endswith('.py'):
        py_bin = str(venv_dir / 'bin' / 'python3')
        command = f"{py_bin} {bot_file}"
    else:
        py_bin = str(venv_dir / 'bin' / 'python3')
        command = f"{py_bin} -m {Path(run_command).stem}"

    env_line = ','.join([f'{k}="{v}"' for k, v in env.items()]) if env else ""
    conf_path = Path(SUPERVISORD_CONF_DIR) / f"{botname}.conf"
    conf_content = f"""[program:{botname}]
command={command}
directory={bot_dir}
autostart=true
autorestart=true
startretries=12
stderr_logfile={SUPERVISOR_LOG_DIR}/{botname}_err.log
stdout_logfile={SUPERVISOR_LOG_DIR}/{botname}_out.log
{f"environment={env_line}" if env_line else ""}
""".strip()
    conf_path.write_text(conf_content)

    subprocess.run(["supervisorctl", "reread"], check=False)
    subprocess.run(["supervisorctl", "update"], check=False)

    # Register panel if port given
    if panel_port:
        # Auto-pick next slug
        existing = [int(s[3:]) for s in BOT_PANELS if re.match(r'^bot\d+$', s)]
        next_num = max(existing, default=0) + 1
        slug = f"bot{next_num}"
        BOT_PANELS[slug] = {"port": int(panel_port), "process_name": botname, "label": panel_label}
        save_bot_panels()
    else:
        slug = None

    # Persist to dynamic bots list
    bots = load_dynamic_bots()
    bots.append({
        "botname": botname,
        "git_url": git_url,
        "branch": branch,
        "run_command": run_command,
        "env": env,
        "python_version": python_version,
        "panel_port": panel_port,
        "panel_slug": slug,
    })
    save_dynamic_bots(bots)

    broadcast_status_update()
    return jsonify({"status": "success", "message": f"Bot {botname} added", "panel_slug": slug}), 201


@app.route('/api/bots/<botname>', methods=['DELETE'])
@login_required
def remove_bot(botname):
    """Stop, remove supervisord config, and delete bot directory."""
    botname = botname.strip().replace(' ', '_')
    if not re.match(r'^[a-zA-Z0-9_\-]+$', botname):
        return jsonify({"status": "error", "message": "Invalid botname"}), 400

    # Stop via supervisorctl
    run_supervisor_command("stop", botname)
    time.sleep(1)

    conf_path = Path(SUPERVISORD_CONF_DIR) / f"{botname}.conf"
    if conf_path.exists():
        conf_path.unlink()

    subprocess.run(["supervisorctl", "reread"], check=False)
    subprocess.run(["supervisorctl", "update"], check=False)

    bot_dir = Path('/app') / botname
    if bot_dir.exists():
        shutil.rmtree(bot_dir)

    delete_supervisor_logs(botname)

    # Remove panel entry if any
    for slug, info in list(BOT_PANELS.items()):
        if info.get("process_name") == botname:
            del BOT_PANELS[slug]
    save_bot_panels()

    # Remove from dynamic bots list
    bots = [b for b in load_dynamic_bots() if b.get("botname") != botname]
    save_dynamic_bots(bots)

    broadcast_status_update()
    return jsonify({"status": "success", "message": f"Bot {botname} removed"})


# ── Supervisor process management (existing) ───────────────────

@app.route('/supervisor/status', methods=['GET'])
def list_supervisor_processes():
    status = run_supervisor_command("status")
    if status["status"] == "success":
        processes = []
        for line in status["message"].splitlines():
            process = parse_supervisor_status(line)
            if process:
                process["panel_slug"] = _find_slug_for_process(process["name"])
                processes.append(process)
        return jsonify({"status": "success", "processes": processes}), 200
    return jsonify(status), 500


@app.route('/supervisor/pause/<process_name>', methods=['POST'])
def pause_supervisor_process(process_name):
    result = pause_process(process_name)
    if result["status"] == "success":
        broadcast_status_update()
        return jsonify(result), 200
    return jsonify(result), 500


@app.route('/supervisor/resume/<process_name>', methods=['POST'])
def resume_supervisor_process(process_name):
    result = resume_process(process_name)
    if result["status"] == "success":
        broadcast_status_update()
        return jsonify(result), 200
    return jsonify(result), 500


@app.route('/supervisor/<action>/<process_name>', methods=['POST'])
def manage_supervisor_process(action, process_name):
    if action not in ["start", "stop", "restart"]:
        return jsonify({"status": "error", "message": "Invalid action"}), 400
    if not re.match(r'^[a-zA-Z0-9_\- ]+$', process_name):
        return jsonify({"status": "error", "message": "Invalid process name"}), 400

    try:
        initial_status = verify_process_status(process_name)
        if initial_status is None:
            return jsonify({"status": "error", "message": f"Process {process_name} not found"}), 404

        config_path = Path(SUPERVISORD_CONF_DIR) / f"{process_name.replace(' ', '_')}.conf"

        if action == "stop":
            if "RUNNING" not in initial_status:
                return jsonify({"status": "error", "message": f"Process {process_name} is not running"}), 400
            result = run_supervisor_command("stop", process_name)
            expected_status = "STOPPED"
            if result["status"] == "success":
                try:
                    if config_path.exists():
                        TEMP_SUPERVISOR_CONFIGS[process_name] = config_path.read_text()
                        config_path.unlink()
                        subprocess.run(["supervisorctl", "reread"], check=True)
                        subprocess.run(["supervisorctl", "update"], check=True)
                except Exception as e:
                    logger.error(f"Error handling supervisor config for {process_name}: {e}")

        elif action == "start":
            try:
                if process_name in TEMP_SUPERVISOR_CONFIGS:
                    config_content = TEMP_SUPERVISOR_CONFIGS[process_name]
                    update_process_code(process_name, config_content)
                    config_path.write_text(config_content)
                    subprocess.run(["supervisorctl", "reread"], check=True)
                    subprocess.run(["supervisorctl", "update"], check=True)
                    del TEMP_SUPERVISOR_CONFIGS[process_name]
                else:
                    update_process_code(process_name)
                result = run_supervisor_command("start", process_name)
                expected_status = "RUNNING"
            except Exception as e:
                return jsonify({"status": "error", "message": f"Error restoring configuration: {str(e)}"}), 500

        elif action == "restart":
            try:
                thoroughly_cleanup(process_name)
                delete_supervisor_logs(process_name)
                if config_path.exists():
                    config_content = config_path.read_text()
                    result = run_supervisor_command("stop", process_name)
                    if result["status"] == "success":
                        config_path.unlink()
                        subprocess.run(["supervisorctl", "reread"], check=True)
                        subprocess.run(["supervisorctl", "update"], check=True)
                        time.sleep(2)
                        update_process_code(process_name, config_content)
                        config_path.write_text(config_content)
                        subprocess.run(["supervisorctl", "reread"], check=True)
                        subprocess.run(["supervisorctl", "update"], check=True)
                        result = run_supervisor_command("start", process_name)
                        expected_status = "RUNNING"
                else:
                    return jsonify({"status": "error", "message": f"Config file not found for {process_name}"}), 404
            except Exception as e:
                return jsonify({"status": "error", "message": f"Error during restart: {str(e)}"}), 500

        if result["status"] != "success":
            return jsonify(result), 500

        for _ in range(MAX_STATUS_CHECK_ATTEMPTS):
            time.sleep(STATUS_CHECK_INTERVAL)
            current_status = verify_process_status(process_name)
            if action == "stop" and current_status is None:
                broadcast_status_update()
                return jsonify({"status": "success", "message": f"Successfully stopped {process_name}"}), 200
            if current_status and expected_status in current_status:
                broadcast_status_update()
                return jsonify({"status": "success", "message": f"Successfully {action}ed {process_name}"}), 200

        return jsonify({"status": "error", "message": f"Process did not reach {expected_status} state"}), 500

    except Exception as e:
        return jsonify({"status": "error", "message": f"Error managing process: {str(e)}"}), 500


@app.route('/supervisor/log/<process_name>', methods=['GET'])
def download_supervisor_log(process_name):
    try:
        if not re.match(r'^[a-zA-Z0-9_\- ]+$', process_name):
            return jsonify({"status": "error", "message": "Invalid process name"}), 400
        stdout_log = Path(SUPERVISOR_LOG_DIR) / f"{process_name}_out.log"
        stderr_log = Path(SUPERVISOR_LOG_DIR) / f"{process_name}_err.log"
        combined_log = Path(SUPERVISOR_LOG_DIR) / f"{process_name}_combined.log"
        if stdout_log.exists() or stderr_log.exists():
            with combined_log.open('w') as outfile:
                outfile.write(f"=== Combined logs for {process_name} ===\n")
                outfile.write(f"Generated at: {datetime.utcnow().isoformat()}\n\n")
                if stdout_log.exists():
                    outfile.write("=== STDOUT LOG ===\n")
                    outfile.write(stdout_log.read_text())
                    outfile.write("\n\n")
                if stderr_log.exists():
                    outfile.write("=== STDERR LOG ===\n")
                    outfile.write(stderr_log.read_text())
            return send_file(str(combined_log), mimetype='text/plain', as_attachment=True,
                             download_name=f"{process_name}_combined.log")
        else:
            return jsonify({"status": "error", "message": "No log files found"}), 404
    except Exception as e:
        return jsonify({"status": "error", "message": str(e)}), 500


@app.route('/supervisor/clear_failure/<process_name>', methods=['POST'])
@login_required
def clear_failure(process_name):
    FAILURE_COUNTS[process_name] = 0
    PAUSED_BY_SYSTEM.discard(process_name)
    run_supervisor_command("start", process_name)
    broadcast_status_update()
    return jsonify({"status": "success", "message": f"Cleared failure state for {process_name}"})


# ── Log stream ─────────────────────────────────────────────────

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
            for log_file in sorted(log_dir.glob("*.log")):
                if '_combined' in log_file.name:
                    continue
                try:
                    pos = positions.get(log_file.name, 0)
                    size = log_file.stat().st_size
                    if size < pos:
                        pos = 0
                    if size > pos:
                        with log_file.open('r', errors='replace') as fh:
                            fh.seek(pos)
                            new_data = fh.read()
                            positions[log_file.name] = fh.tell()
                        if new_data.strip():
                            payload = json.dumps({"file": log_file.name, "data": new_data})
                            yield f"data: {payload}\n\n"
                except Exception:
                    pass
            eventlet.sleep(1)
    return Response(stream_with_context(generate()), mimetype='text/event-stream',
                    headers={'Cache-Control': 'no-cache', 'X-Accel-Buffering': 'no'})


# ── Cron config ────────────────────────────────────────────────

@app.route('/config/cron', methods=['GET', 'POST'])
@login_required
def config_cron():
    global CRON_RESTART_INTERVAL, _cron_thread
    if request.method == 'POST':
        data = request.get_json(silent=True) or {}
        hours = int(data.get('hours', 0))
        CRON_RESTART_INTERVAL = max(0, hours)
        os.environ['CRON_RESTART_HOURS'] = str(CRON_RESTART_INTERVAL)
        _start_cron_thread()
        return jsonify({"status": "success", "hours": CRON_RESTART_INTERVAL})
    return jsonify({"status": "success", "hours": CRON_RESTART_INTERVAL})


# ── SocketIO events ────────────────────────────────────────────

@socketio.on('connect')
def handle_connect():
    emit('connected', {'data': 'Connected'})
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
            for proc in status["message"].splitlines():
                parsed_proc = parse_supervisor_status(proc)
                if parsed_proc:
                    pname = parsed_proc["name"]
                    if parsed_proc["status"] in ("FATAL", "BACKOFF", "EXITED"):
                        FAILURE_COUNTS[pname] += 1
                        if FAILURE_COUNTS[pname] >= MAX_FAILURES_BEFORE_PAUSE:
                            PAUSED_BY_SYSTEM.add(pname)
                        parsed_proc["auto_paused"] = pname in PAUSED_BY_SYSTEM
                    else:
                        if parsed_proc["status"] == "RUNNING":
                            FAILURE_COUNTS[pname] = 0
                            PAUSED_BY_SYSTEM.discard(pname)
                        parsed_proc["auto_paused"] = pname in PAUSED_BY_SYSTEM
                    parsed_proc["panel_slug"] = _find_slug_for_process(pname)
                    processes.append(parsed_proc)
            emit('status_update', {
                "status": "success",
                "processes": processes,
                "timestamp": datetime.utcnow().isoformat()
            })
        else:
            emit('status_update', {"status": "error", "message": status["message"], "processes": []})
    except Exception as e:
        emit('status_update', {"status": "error", "message": str(e), "processes": []})


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
            run_supervisor_command("restart", "all")
            broadcast_status_update()
        except Exception as e:
            logger.error(f"Cron restart error: {e}")


def _start_cron_thread():
    global _cron_thread
    if _cron_thread is None:
        _cron_thread = eventlet.spawn(_cron_restart_loop)


def _auto_delete_logs_loop():
    while True:
        eventlet.sleep(24 * 3600)
        try:
            log_dir = Path(SUPERVISOR_LOG_DIR)
            for log_file in log_dir.glob("*.log"):
                try:
                    log_file.unlink()
                except Exception:
                    pass
        except Exception as e:
            logger.error(f"Auto log cleanup error: {e}")


_log_cleanup_thread = None


def _start_log_cleanup_thread():
    global _log_cleanup_thread
    if _log_cleanup_thread is None:
        _log_cleanup_thread = eventlet.spawn(_auto_delete_logs_loop)


@app.errorhandler(Exception)
def handle_error(e):
    logger.error(f"Unhandled error: {str(e)}")
    return jsonify({"status": "error", "message": "An internal server error occurred"}), 500


_start_cron_thread()
_start_log_cleanup_thread()
