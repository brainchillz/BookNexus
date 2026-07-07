FROM python:3.12-slim

WORKDIR /app

# Install dependencies first so this layer is cached on rebuilds
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy application code
COPY app.py gunicorn.conf.py export_data.py ./
COPY templates/ templates/
COPY static/ static/

# Run as a non-root user. Fixed high uid so host files exposed to the
# container (e.g. the TLS key) can be chowned to it without colliding
# with a real host user.
RUN adduser --disabled-password --gecos '' --uid 10001 appuser \
    && chown -R appuser:appuser /app
USER appuser

EXPOSE 8000

CMD ["gunicorn", "-c", "gunicorn.conf.py", "app:app"]
