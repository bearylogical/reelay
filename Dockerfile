FROM python:3.11-slim-bookworm

WORKDIR /app

# transmission-remote for the optional Transmission speed controls
RUN apt-get update \
    && apt-get install -y --no-install-recommends transmission-cli \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

ENTRYPOINT ["python", "-m", "reelay"]
