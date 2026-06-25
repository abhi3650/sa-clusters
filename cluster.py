import subprocess
import threading
import time
import os

def run_update():
    subprocess.run(["python3", "update.py"])

def run_server():
    """
    Run the Flask-SocketIO server directly via eventlet (no gunicorn needed).
    worker.py initialises the socketio instance with eventlet and serves the app.
    """
    port = os.environ.get("PORT", "5000")
    env = os.environ.copy()
    env["PORT"] = str(port)
    subprocess.run(["python3", "worker.py"], env=env)

def run_supervisord():
    subprocess.run(["supervisord", "-n", "-c", "supervisord.conf"])

def run_ping_server():
    subprocess.run(["python3", "ping_server.py"])

if __name__ == "__main__":
    # Step 1: pull latest code
    update_thread = threading.Thread(target=run_update)
    update_thread.start()
    update_thread.join()
    time.sleep(2)

    # Step 2: launch all services in parallel
    server_thread     = threading.Thread(target=run_server,     daemon=False)
    supervisord_thread = threading.Thread(target=run_supervisord, daemon=False)
    ping_thread       = threading.Thread(target=run_ping_server, daemon=False)

    server_thread.start()
    supervisord_thread.start()
    ping_thread.start()

    server_thread.join()
    supervisord_thread.join()
    ping_thread.join()
