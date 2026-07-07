"""Gunicorn config shared by the Docker image and bare-metal runs.

TLS is on by default: a self-signed certificate is generated on first boot
when nothing else is configured (see tls_config.py for the priority order).
The Settings page swaps certificates at runtime by signaling gunicorn to
gracefully reload (SIGHUP), which re-evaluates this file.
"""
from dotenv import load_dotenv

load_dotenv()

from tls_config import resolve_tls  # noqa: E402 — needs env loaded first

bind = '0.0.0.0:8000'
workers = 4
timeout = 60
accesslog = '-'

certfile, keyfile = resolve_tls()
