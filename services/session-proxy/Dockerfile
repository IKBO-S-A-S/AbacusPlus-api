FROM python:3.11-slim

WORKDIR /app

RUN apt-get update && apt-get install -y \
    wget curl gnupg \
    xvfb \
    libglib2.0-0 libnss3 libnspr4 libatk1.0-0 libatk-bridge2.0-0 \
    libcups2 libdrm2 libdbus-1-3 libxcb1 libxkbcommon0 \
    libx11-6 libxcomposite1 libxdamage1 libxext6 libxfixes3 \
    libxrandr2 libgbm1 libpango-1.0-0 libcairo2 libasound2 \
    fonts-liberation fonts-noto-core \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

# Los navegadores van a una ruta compartida en vez de a /root/.cache, que es el destino por
# defecto y queda ilegible en cuanto el proceso deja de ser root.
ENV PLAYWRIGHT_BROWSERS_PATH=/ms-playwright

RUN patchright install chromium
# Google Chrome real (channel="chrome") — el Turnstile invisible se auto-resuelve
# mejor en Chrome estable que en Chromium (fingerprint GPU/WebGL más realista).
RUN patchright install chrome || playwright install chrome || true

COPY . .

ENV PYTHONPATH=/app
ENV ENVIRONMENT=prod

EXPOSE 8004

# Usuario sin privilegios: el servicio no necesita root en ejecucion y limitarlo contiene el
# alcance de una eventual ejecucion remota de codigo. Importa mas aqui que en el resto de
# servicios, porque este es el unico que ejecuta un navegador contra un portal externo.
# El UID 1000 coincide con el usuario habitual del host para que los bind mounts de
# docker-compose (./services/session-proxy/app y ./downloads) conserven permisos de escritura.
RUN groupadd -g 1000 appuser \
    && useradd -u 1000 -g appuser -m appuser \
    && chown -R appuser:appuser /app /ms-playwright
# Xvfb (usado por session-proxy-worker para Chrome headful) rechaza crear
# /tmp/.X11-unix cuando no corre como root ("_XSERVTransmkdir: euid != 0"). Sin él,
# el DISPLAY nunca queda listo: Chrome arranca igual pero sin servidor X detrás, y
# revienta ("Target page, context or browser has been closed") a los pocos segundos
# de la primera navegación, justo cuando toca renderizar. Se crea aquí, en la capa de
# imagen (persiste entre reinicios porque /tmp no es un tmpfs en este compose), con el
# sticky bit 1777 estándar de X11 para que appuser pueda crear el socket dentro.
RUN mkdir -p /tmp/.X11-unix && chmod 1777 /tmp/.X11-unix
USER appuser

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8004"]
