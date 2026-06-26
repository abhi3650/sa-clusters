import subprocess
import threading
import time
import os
import sys
import logging

logging.basicConfig(
    format="[%(asctime)s] [%(levelname)s] - %(message)s",
    datefmt="%m/%d/%Y, %H:%M:%S",
    handlers=[logging.StreamHandler(sys.stdout)],
    level=logging.INFO,
)
logger = logging.getLogger("cluster")

def run_update():
    logger.info("Running update.py...")
    subprocess.run(["python3", "update.py"])

def run_server():
    port = os.environ.get("PORT", "5000")
    logger.info(f"Starting web server on port {port}...")
    subprocess.run(["python3", "run.py"])
    logger.error("run.py exited — web server stopped")

def run_supervisord():
    logger.info("Starting supervisord...")
    subprocess.run(["supervisord", "-n", "-c", "supervisord.conf"])

def run_ping_server():
    logger.info("Starting ping server...")
    subprocess.run(["python3", "ping_server.py"])

if __name__ == "__main__":
    # 1. Pull latest code (update.py protects our enhanced files)
    run_update()
    time.sleep(2)

    # 2. Launch all services
    threads = [
        threading.Thread(target=run_server,     daemon=False, name="server"),
        threading.Thread(target=run_supervisord, daemon=False, name="supervisord"),
        threading.Thread(target=run_ping_server, daemon=False, name="ping"),
    ]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
