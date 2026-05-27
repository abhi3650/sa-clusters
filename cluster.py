import eventlet
eventlet.monkey_patch()

import os
import logging
import threading
import subprocess
import time

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

def run_update():
    subprocess.run(["python3", "update.py"])

def run_supervisord():
    subprocess.run(["supervisord", "-n", "-c", "supervisord.conf"])

def run_worker():
    subprocess.run(["python3", "worker.py"])

def run_ping_server():
    subprocess.run(["python3", "ping_server.py"])

if __name__ == "__main__":
    # Step 1: update (runs git pull from upstream if UPSTREAM_REPO is set)
    run_update()
    time.sleep(2)

    # Step 2: start background services as daemon threads
    for target in [run_supervisord, run_worker, run_ping_server]:
        t = threading.Thread(target=target, daemon=True)
        t.start()

    # Step 3: start Flask-SocketIO in the MAIN thread (eventlet needs this)
    os.makedirs("/var/log/supervisor", exist_ok=True)
    port = int(os.environ.get("PORT", 5000))
    logger.info(f"Starting Flask-SocketIO on 0.0.0.0:{port}")

    from app import app
    from app.routes.routes import socketio

    socketio.run(
        app,
        host="0.0.0.0",
        port=port,
        debug=False,
        use_reloader=False,
    )
