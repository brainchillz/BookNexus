"""Gunicorn config shared by the Docker image and the bare-metal systemd unit.

When TLS_CERT/TLS_KEY are set in the environment (or .env), gunicorn serves
HTTPS directly; otherwise it serves plain HTTP as before.
"""
import os

from dotenv import load_dotenv

load_dotenv()

bind = '0.0.0.0:8000'
workers = 4
timeout = 60
accesslog = '-'

certfile = os.environ.get('TLS_CERT') or None
keyfile = os.environ.get('TLS_KEY') or None
