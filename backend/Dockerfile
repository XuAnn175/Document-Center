FROM python:3.10.1-slim

# Create a non-root user so the application does not run as root.
# Fixed uid 10001 to match securityContext.runAsUser in the k8s manifest.
RUN useradd --create-home --uid 10001 --shell /usr/sbin/nologin appuser

WORKDIR /app

# Install dependencies first: as long as requirements.txt is unchanged this
# layer is cached, so editing source code does not trigger a reinstall.
COPY backend/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy source last, since it changes most frequently.
COPY --chown=appuser:appuser backend /app/backend

# The uploads directory must exist and be writable by appuser.
RUN mkdir -p /app/backend/uploads && chown -R appuser:appuser /app

# PYTHONPATH points at /app, the PARENT of backend/.
# app.py uses relative imports (from .models import ...), so it must be loaded
# as part of a package. The entrypoint is therefore backend.app:app, not app:app.
ENV PYTHONPATH=/app
ENV FLASK_APP=backend.app
ENV FLASK_ENV=production
ENV PYTHONUNBUFFERED=1

# WORKDIR stays at backend/ so the relative UPLOAD_FOLDER='uploads' in config.py
# still resolves to /app/backend/uploads, matching the compose bind mount.
WORKDIR /app/backend

USER appuser

EXPOSE 5001

# Exec form (JSON array) makes the gunicorn master PID 1, so it receives
# SIGTERM directly and can shut down gracefully.
# Worker count rule of thumb: (2 x CPU cores) + 1. This workload is I/O bound,
# so gevent workers are worth evaluating.
# --access-logfile - sends access logs to stdout for the platform to collect
# (12-factor, factor XI).
CMD ["gunicorn", \
     "--bind", "0.0.0.0:5001", \
     "--workers", "4", \
     "--timeout", "120", \
     "--graceful-timeout", "30", \
     "--access-logfile", "-", \
     "backend.app:app"]
