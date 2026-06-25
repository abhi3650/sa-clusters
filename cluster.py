import subprocess
import threading
import time
import os
import sys
import logging

logging.basicConfig(
    format="[%(asctime)s] [%(name)s | %(levelname)s] - %(message)s [%(filename)s:%(lineno)d]",
    datefmt="%m/%d/%Y, %H:%M:%S %p",
    handlers=[logging.StreamHandler(sys.stdout)],
    level=logging.INFO,
)
logger = logging.getLogger("cluster")

def run_update():
    logger.info("Running update.py...")
    result = subprocess.run(["python3", "update.py"])
    if result.returncode != 0:
        logger.warning("update.py exited non-zero — continuing anyway")

def run_server():
    """Start the Flask-SocketIO server via eventlet (worker.py)."""
    port = os.environ.get("PORT", "5000")
    logger.info(f"Starting BotClusters web server on port {port}...")
    env = os.environ.copy()
    env["PORT"] = str(port)
    result = subprocess.run(["python3", "worker.py"], env=env)
    logger.error(f"worker.py exited with code {result.returncode}")

def run_supervisord():
    logger.info("Starting supervisord...")
    subprocess.run(["supervisord", "-n", "-c", "supervisord.conf"])

def run_ping_server():
    logger.info("Starting ping server...")
    subprocess.run(["python3", "ping_server.py"])

if __name__ == "__main__":
    # Step 1: pull latest code (update.py now protects our enhanced files)
    run_update()
    time.sleep(2)

    # Step 2: launch all services concurrently
    threads = [
        threading.Thread(target=run_server,      daemon=False, name="server"),
        threading.Thread(target=run_supervisord,  daemon=False, name="supervisord"),
        threading.Thread(target=run_ping_server,  daemon=False, name="ping"),
    ]
    for t in threads:
        t.start()

    for t in threads:
        t.join()
