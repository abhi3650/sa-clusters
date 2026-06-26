"""
worker.py — shim for backwards compatibility.
All enhanced route logic now lives in app/routes/routes.py
The real entry point is run.py
"""
import eventlet
eventlet.monkey_patch()

import os, sys
from app import app
from app.routes.routes import socketio

if __name__ == '__main__':
    port = int(os.environ.get('PORT', 5000))
    print(f"[BotClusters] worker.py starting on port {port}", flush=True)
    os.makedirs('/var/log/supervisor', exist_ok=True)
    os.makedirs('/app', exist_ok=True)
    socketio.run(
        app,
        host='0.0.0.0',
        port=port,
        debug=False,
        use_reloader=False,
    )
