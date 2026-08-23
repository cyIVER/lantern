# LANtern CS2 control UI
FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1

WORKDIR /srv
RUN pip install --no-cache-dir \
      "fastapi>=0.110" "uvicorn[standard]>=0.29" "httpx>=0.27" "pydantic>=2" "pymysql>=1.1" "websockets>=12"

COPY app/    /srv/app/
COPY static/ /srv/static/

EXPOSE 8090
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8090"]
