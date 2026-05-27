# eventlet.monkey_patch() is called once in cluster.py before any imports.
# Calling it again here is a no-op but harmless.
import eventlet
eventlet.monkey_patch()

from flask import Flask

app = Flask(__name__)

from app import routes
