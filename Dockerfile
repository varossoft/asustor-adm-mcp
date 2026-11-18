FROM python:3.13-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONPATH=/app/src

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY src/ src/

# Default UID/GID (1000:100); overridden by `user:` in the compose file.
USER 1000:100
EXPOSE 8088
CMD ["python", "-m", "adm_mcp.server"]
