# LANtern Stardew control UI
FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1

WORKDIR /srv
# `docker` is here only to restart the game container after a mod toggle --
# SMAPI reads the mods folder once at startup, so nothing else can apply one.
RUN pip install --no-cache-dir \
      "fastapi>=0.110" "uvicorn[standard]>=0.29" "httpx>=0.27" "pydantic>=2" "docker>=7"

COPY app/    /srv/app/
COPY static/ /srv/static/

EXPOSE 8092
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8092"]
