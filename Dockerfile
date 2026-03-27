FROM python:3.11-slim

# Install system dependencies for Playwright + lxml
RUN apt-get update && apt-get install -y --no-install-recommends \
    curl wget gnupg ca-certificates \
    libglib2.0-0 libnss3 libnspr4 libatk1.0-0 libatk-bridge2.0-0 \
    libcups2 libdrm2 libxkbcommon0 libxcomposite1 libxdamage1 \
    libxfixes3 libxrandr2 libgbm1 libasound2 libpango-1.0-0 \
    libcairo2 libx11-6 libxext6 libxshmfence1 \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Install Python dependencies
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Install Playwright browsers
RUN playwright install chromium --with-deps

# Copy source
COPY . .

# Create data directories
RUN mkdir -p data/screenshots logs

ENV PYTHONUNBUFFERED=1
ENV PYTHONPATH=/app

# Default: start the API server. Override with `command:` in docker-compose.
ENTRYPOINT []
CMD ["uvicorn", "src.api.main:app", "--host", "0.0.0.0", "--port", "8000"]
