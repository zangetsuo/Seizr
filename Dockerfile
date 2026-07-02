FROM python:3.12-slim

WORKDIR /app

# Core deps only — the optional NameMC autofill (Playwright/Chromium) is not
# installed: it can't run from a datacenter IP anyway. Look windows up at home.
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# The seizr package plus the two entry-point shims (webapp.py for uvicorn,
# sniper.py for the CLI).
COPY seizr ./seizr
COPY webapp.py sniper.py config.json ./
COPY static ./static

# Persist the SQLite DB + auth token cache on a mounted volume so logins survive
# restarts.
VOLUME ["/data"]
ENV SEIZR_DB=/data/seizr.db
ENV AUTH_CACHE=/data/.auth_cache.json

EXPOSE 8000
CMD ["uvicorn", "webapp:app", "--host", "0.0.0.0", "--port", "8000"]
