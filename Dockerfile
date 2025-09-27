# Dockerfile for Render (Flask + Ghostscript)
FROM python:3.11-slim

ENV PYTHONUNBUFFERED=1
ARG DEBIAN_FRONTEND=noninteractive

# Install Ghostscript (and clean apt cache)
RUN apt-get update && \
    apt-get install -y --no-install-recommends ghostscript && \
    rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Copy and install Python deps
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy app source
COPY . .

# Expose default port (documentation: Render gives PORT env var)
EXPOSE 5000

# Bind gunicorn to the PORT env var Render provides (fallback to 5000 locally)
CMD ["sh", "-c", "gunicorn -w 4 -b 0.0.0.0:${PORT:-5000} app:app"]
