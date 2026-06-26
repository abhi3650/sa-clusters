import eventlet
eventlet.monkey_patch()

import os
import logging
import sys

# Force stdout to be unbuffered so Render sees logs immediately
sys.stdout.reconfigure(line_buffering=True)

from app import app
from app.routes.routes import socketio

SUPERVISOR_LOG_DIR = "/var/log/supervisor"

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
)
logger = logging.getLogger(__name__)

if __name__ == "__main__":
    try:
        os.makedirs(SUPERVISOR_LOG_DIR, exist_ok=True)
        os.makedirs('/app', exist_ok=True)

        port = int(os.environ.get("PORT", 5000))

        print(f"[BotClusters] Starting on 0.0.0.0:{port}", flush=True)
        logger.info(f"Starting BotClusters Enhanced on port {port}")

        socketio.run(
            app,
            host="0.0.0.0",
            port=port,
            debug=False,
            use_reloader=False,
        )
    except Exception as e:
        logger.error(f"Failed to start: {e}", exc_info=True)
        raise
