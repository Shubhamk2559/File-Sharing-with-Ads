FROM python:3.12-slim

# No secrets here: every credential is a runtime environment variable set in the Koyeb service (see KOYEB.md). Nothing below COPYs an env file.
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PORT=8000

WORKDIR /app

COPY requirements.txt .
RUN pip install -r requirements.txt

# .dockerignore keeps .env*, tests, docs and VCS data out of the image
COPY . .

# Run as an unprivileged user
RUN useradd --system --no-create-home --uid 10001 app && chown -R app /app
USER app

# Koyeb injects PORT (the port you configure on the service); serve.py binds 0.0.0.0:$PORT
EXPOSE 8000

# Liveness only (no database): lets `docker run` report health locally. Koyeb uses its own check from the service settings (KOYEB.md).
HEALTHCHECK --interval=30s --timeout=5s --start-period=30s --retries=3 \
  CMD python -c "import os,urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:%s/' % os.environ.get('PORT','8000'), timeout=4).status == 200 else 1)"

# exec form: python is PID 1 and receives SIGTERM directly => graceful shutdown (a `sh -c` wrapper would swallow the signal)
CMD ["python", "serve.py"]
