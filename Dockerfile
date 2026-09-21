FROM python:3.12-slim

WORKDIR /app
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    HOST=0.0.0.0 \
    PORT=5522 \
    RECORDS_PATH=/data/records.json

COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

COPY server.py ./
COPY web ./web
COPY res/parole.txt res/paroleRaddoppio.txt ./res/
COPY res/img ./res/img
COPY res/lemon_milk ./res/lemon_milk
COPY res/sounds ./res/sounds

RUN useradd --create-home --uid 10001 app && mkdir -p /data && chown -R app:app /app /data
USER app

EXPOSE 5522
HEALTHCHECK --interval=30s --timeout=3s --start-period=5s --retries=3 CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:5522/health').read()"

CMD ["python", "server.py"]
