FROM python:3.12-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY auth.py api.py sniper.py webapp.py config.json ./
COPY static ./static

# Persist the auth token cache outside the image via a mounted volume.
VOLUME ["/data"]
ENV AUTH_CACHE=/data/.auth_cache.json

EXPOSE 8000
CMD ["uvicorn", "webapp:app", "--host", "0.0.0.0", "--port", "8000"]
