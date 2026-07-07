"""TLS resolution shared by gunicorn.conf.py and app.py.

BookNexus serves HTTPS out of the box: without any configuration it
generates a self-signed certificate on first boot (the camera barcode
scanner requires a secure origin, so plain HTTP would silently cripple
it). The Settings page can replace the certificate, regenerate it, or
disable HTTPS entirely.

Priority order:
  1. TLS_CERT/TLS_KEY environment variables (manual/advanced) — the
     Settings page then shows the cert as environment-managed.
  2. HTTPS=true (external TLS-terminating proxy) — serve plain HTTP.
  3. "disabled" marker (user turned HTTPS off in Settings) — plain HTTP.
  4. Previously uploaded/generated pair in <data dir>/certs/.
  5. Nothing yet — auto-generate a self-signed pair.
"""
import os
import subprocess


def tls_dir():
    data_dir = os.path.dirname(os.environ.get('DB_PATH', 'data/books.db')) or '.'
    return os.path.join(data_dir, 'certs')


def cert_file():
    return os.path.join(tls_dir(), 'cert.pem')


def key_file():
    return os.path.join(tls_dir(), 'key.pem')


def disabled_marker():
    return os.path.join(tls_dir(), 'disabled')


def generate_self_signed(hostname='booknexus', extra_ip=None):
    """Generate a 10-year self-signed pair into the data certs dir."""
    os.makedirs(tls_dir(), exist_ok=True)
    san = f'DNS:{hostname},DNS:localhost,IP:127.0.0.1'
    if extra_ip:
        san += f',IP:{extra_ip}'
    subprocess.run(
        ['openssl', 'req', '-x509', '-newkey', 'rsa:2048', '-sha256',
         '-days', '3650', '-nodes',
         '-keyout', key_file(), '-out', cert_file(),
         '-subj', f'/CN={hostname}', '-addext', f'subjectAltName={san}'],
        check=True, capture_output=True)
    os.chmod(key_file(), 0o600)


def resolve_tls(auto_generate=True):
    """Return (certfile, keyfile) to serve, or (None, None) for plain HTTP."""
    env_cert = os.environ.get('TLS_CERT')
    env_key = os.environ.get('TLS_KEY')
    if env_cert and env_key:
        return env_cert, env_key
    if os.environ.get('HTTPS', '').lower() == 'true':
        return None, None
    if os.path.exists(disabled_marker()):
        return None, None
    if os.path.exists(cert_file()) and os.path.exists(key_file()):
        return cert_file(), key_file()
    if auto_generate:
        try:
            generate_self_signed()
            return cert_file(), key_file()
        except Exception:
            return None, None
    return None, None
