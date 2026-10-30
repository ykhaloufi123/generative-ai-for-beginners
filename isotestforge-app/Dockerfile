# Image unique : interface web + API + CLI + simulateur hôte intégré (port 8583)
FROM python:3.12-slim
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 ITF_HOME=/app PORT=8080 ITF_AUTOSTART_SIMULATOR=1
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY . .
RUN pip install --no-cache-dir -e . \
 && mkdir -p data/workflows data/cache data/traces \
 && useradd -m -u 1000 itf && chown -R itf:itf /app
USER itf
EXPOSE 8080 8583
HEALTHCHECK --interval=30s --timeout=5s CMD python -c "import urllib.request,os;urllib.request.urlopen(f'http://127.0.0.1:{os.environ.get(\"PORT\",\"8080\")}/health')"
CMD ["itf", "serve"]
