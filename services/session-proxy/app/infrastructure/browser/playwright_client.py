import asyncio
import base64
import contextlib
import logging
import os
from urllib.parse import parse_qs, quote, urlparse

import httpx

# patchright: Playwright parcheado que elimina las huellas de automatización
# (navigator.webdriver, CDP runtime leaks, --enable-automation) que detectan los
# challenges de Cloudflare Turnstile del portal DIAN. Reemplaza a playwright +
# playwright-stealth en ambos flujos (company_login y descargas).
from patchright.async_api import async_playwright

from app.domain.exceptions.base import BrowserLoginException

logger = logging.getLogger(__name__)

CLOUDFLARE_TITLES = ["just a moment", "attention required"]

_BROWSER_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/124.0.0.0 Safari/537.36"
)

_CHROMIUM_ARGS = [
    "--no-sandbox",
    "--disable-setuid-sandbox",
    "--disable-blink-features=AutomationControlled",
    "--disable-dev-shm-usage",
]

# ── Turnstile (widget de descarga del portal DIAN) ─────────────────────────
# El endpoint DownloadZipFiles exige ?captcha={token_turnstile}. El token se genera
# in-browser (Chrome real) o, como fallback, vía CapSolver.
TURNSTILE_SITEKEY = os.getenv("TURNSTILE_SITEKEY", "0x4AAAAAAAg1WuNb-OnOa76z")
TURNSTILE_PAGE_PATH = os.getenv("TURNSTILE_PAGE_PATH", "/Document/Received")
CAPSOLVER_API_KEY = os.getenv("CAPSOLVER_API_KEY", "")

# ── Clasificación del resultado de una descarga ────────────────────────────
VALID_ZIP = "VALID_ZIP"
AZURE_WAF = "AZURE_WAF"
CLOUDFLARE = "CLOUDFLARE"
DIAN_INVALID = "DIAN_INVALID"
AUTH_LOST = "AUTH_LOST"
EMPTY_OR_HTML = "EMPTY_OR_HTML"

# Firmas de archivo ZIP: local file header, empty archive, spanned archive.
_ZIP_MAGICS = (b"PK\x03\x04", b"PK\x05\x06", b"PK\x07\x08")


def is_valid_zip(data: bytes) -> bool:
    """True si los primeros bytes corresponden a la firma de un archivo ZIP."""
    return len(data) >= 4 and data[:4] in _ZIP_MAGICS


class PlaywrightBrowserClient:
    def __init__(self, representative: str = "", nit: str = "", timeout: int = 60000):
        self._representative = representative
        self._nit = nit
        self._timeout = timeout  # ms

    async def company_login(self, login_url: str) -> tuple[dict[str, str], list[str]]:
        steps: list[str] = []
        async with async_playwright() as p:
            browser = await p.chromium.launch(
                headless=True,
                args=[
                    "--no-sandbox",
                    "--disable-setuid-sandbox",
                    "--disable-blink-features=AutomationControlled",
                    "--disable-dev-shm-usage",
                ],
            )
            context = await browser.new_context(
                user_agent=(
                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) "
                    "Chrome/120.0.0.0 Safari/537.36"
                ),
                viewport={"width": 1280, "height": 720},
            )
            page = await context.new_page()
            try:
                logger.info("Navegando a: %s", login_url)
                await page.goto(login_url, wait_until="networkidle", timeout=self._timeout)
                steps.append(f"Navegación completada — URL: {page.url}")

                await page.click("#legalRepresentative")
                steps.append("Clic en #legalRepresentative completado")
                logger.info("Clic en #legalRepresentative")

                await self._handle_cloudflare(page, steps)

                await page.fill("#UserCode", self._representative)
                steps.append("Campo #UserCode completado")
                logger.info("Campo #UserCode completado")

                await page.fill("#CompanyCode", self._nit)
                steps.append("Campo #CompanyCode completado")
                logger.info("Campo #CompanyCode completado")

                await page.click("[type=submit]")
                steps.append("Clic en botón Entrar completado")
                logger.info("Clic en botón Entrar")

                await page.wait_for_load_state("networkidle", timeout=self._timeout)
                steps.append(f"Carga post-login completada — URL: {page.url}")

                current_url = page.url
                if "Login" in current_url or "login" in current_url:
                    raise BrowserLoginException(
                        "Login fallido: credenciales inválidas o Cloudflare bloqueó el acceso",
                        steps,
                    )

                raw_cookies = await context.cookies()
                cookies = {c["name"]: c["value"] for c in raw_cookies}
                steps.append(f"Cookies capturadas: {len(cookies)} ({', '.join(cookies.keys())})")
                logger.info("Cookies capturadas por browser: %d", len(cookies))
                return cookies, steps
            except BrowserLoginException:
                raise
            except Exception as e:
                raise BrowserLoginException(
                    f"Error inesperado en paso {len(steps) + 1}: {type(e).__name__}: {e}",
                    steps,
                ) from e
            finally:
                await browser.close()

    async def _handle_cloudflare(self, page, steps: list[str]) -> None:
        """Detecta y resuelve Cloudflare challenge (JS automático o widget interactivo con clic)."""
        for attempt in range(15):
            title = (await page.title()).lower()
            cf_by_title = any(cf in title for cf in CLOUDFLARE_TITLES)

            # Detectar iframe de Cloudflare Turnstile / Managed Challenge
            cf_frames = [f for f in page.frames if "challenges.cloudflare.com" in f.url]

            if not cf_by_title and not cf_frames:
                steps.append(f"Sin challenge Cloudflare — título: '{title}'")
                logger.info("Sin challenge Cloudflare — título: %s", title)
                break

            steps.append(
                f"Cloudflare detectado (intento {attempt + 1}/15) — "
                f"título: '{title}', iframes: {len(cf_frames)}"
            )
            logger.info(
                "Cloudflare detectado (intento %d/15) — iframes: %d", attempt + 1, len(cf_frames)
            )

            if cf_frames:
                cf_frame = cf_frames[0]

                # Esperar a que el iframe cargue su contenido completamente
                await page.wait_for_timeout(3000)

                # El checkbox de Cloudflare es un input nativo oculto dentro de un div
                # con ID dinámico — se busca por estructura: div > input[type='checkbox']
                clicked = False
                for selector in [
                    "div input[type='checkbox']",
                    "input[type='checkbox']",
                ]:
                    try:
                        checkbox = cf_frame.locator(selector).first
                        await checkbox.wait_for(timeout=5000, state="attached")
                        await checkbox.click(force=True)
                        steps.append(
                            f"Clic en checkbox Cloudflare completado (selector: '{selector}')"
                        )
                        logger.info("Clic en checkbox Cloudflare completado: %s", selector)
                        clicked = True
                        break
                    except Exception as e:
                        steps.append(f"Selector '{selector}' falló — {type(e).__name__}: {e}")
                        logger.warning("Selector '%s' falló: %s", selector, e)

                if not clicked:
                    steps.append("Esperando auto-resolución Cloudflare")

            await page.wait_for_timeout(3000)
            await page.wait_for_load_state("networkidle", timeout=30000)


class DianDownloadError(Exception):
    """Error terminal de descarga (documento no disponible o sesión perdida)."""

    def __init__(self, message: str, kind: str):
        super().__init__(message)
        self.kind = kind


# JS que ejecuta un fetch() nativo dentro de la página y devuelve el cuerpo en base64.
# Al correr en el contexto del navegador comparte cookies, origen y TLS con el SPA,
# por lo que pasa el WAF de Azure y evita el redirect a SearchInvalidQR.
_INPAGE_FETCH_JS = """
async (url) => {
    try {
        const resp = await fetch(url, {
            credentials: 'include',
            headers: {
                'X-Requested-With': 'XMLHttpRequest',
                'Accept': 'application/zip,application/octet-stream,*/*'
            }
        });
        const buf = await resp.arrayBuffer();
        const bytes = new Uint8Array(buf);
        let binary = '';
        const chunk = 0x8000;
        for (let i = 0; i < bytes.length; i += chunk) {
            binary += String.fromCharCode.apply(null, bytes.subarray(i, i + chunk));
        }
        return {
            status: resp.status,
            url: resp.url,
            contentType: resp.headers.get('content-type') || '',
            bodyB64: btoa(binary)
        };
    } catch (e) {
        return { status: -1, url: url, contentType: '', bodyB64: '', error: String(e) };
    }
}
"""


_TURNSTILE_SOLVE_JS = """
async ([siteKey, timeoutMs]) => {
  return await new Promise((resolve) => {
    let done = false;
    const finish = (v) => { if (!done) { done = true; resolve(v); } };
    try {
      if (typeof window.turnstile === 'undefined') return finish('NO_TURNSTILE');
      const holder = document.createElement('div');
      holder.style.position = 'fixed'; holder.style.bottom = '0'; holder.style.left = '0';
      document.body.appendChild(holder);
      const wid = window.turnstile.render(holder, {
        sitekey: siteKey,
        callback: (tok) => finish(tok),
        'error-callback': (e) => finish('ERR:' + e),
        'timeout-callback': () => finish('TIMEOUT_CB'),
      });
      try { window.turnstile.execute(wid, {sitekey: siteKey}); } catch (e) {}
      setTimeout(() => {
        try { const r = window.turnstile.getResponse(wid); if (r) return finish(r); } catch (e) {}
        finish('TIMEOUT');
      }, timeoutMs);
    } catch (e) { finish('ERR:' + e); }
  });
}
"""


class BrowserDownloadSession:
    """
    Sesión de navegador reutilizable para descargar múltiples ZIPs del portal DIAN
    dentro de un solo Chromium. Autentica una vez, "calienta" el WAF navegando a una
    página HTML, y descarga cada documento con un resolver adaptativo que detecta y
    reacciona a distintos bloqueos (Azure WAF, Cloudflare, redirect de error, sesión).

    Uso:
        async with BrowserDownloadSession(login_url, timeout=60000) as sess:
            data = await sess.download(track_id)
    """

    def __init__(self, login_url: str, timeout: int = 60000, max_retries: int = 3):
        self._login_url = login_url
        self._base_url = login_url.split("/User/")[0]
        self._timeout = timeout
        self._max_retries = max_retries
        self._channel = os.getenv("BROWSER_CHANNEL", "chrome") or "chromium"
        # Perfil separado por binario: un user-data-dir de Chromium no es compatible
        # con Google Chrome (contamina el perfil y rompe la clearance de Cloudflare).
        base_profile = os.getenv("BROWSER_PROFILE_DIR", "/app/downloads/.pw-profile")
        self._profile_dir = f"{base_profile}-{self._channel}"
        self._pw = None
        self._context = None
        self._page = None
        # None = no probado; True/False = si el solver in-browser funciona en este entorno.
        self._inbrowser_works = None

    async def __aenter__(self) -> "BrowserDownloadSession":
        await self.open()
        return self

    async def __aexit__(self, *exc) -> None:
        await self.close()

    # ── Ciclo de vida ──────────────────────────────────────────────────────
    async def open(self) -> None:
        # Contexto persistente: la cookie cf_clearance de Cloudflare sobrevive entre
        # ejecuciones, evitando re-resolver el Turnstile en cada batch.
        # headless=False bajo Xvfb reduce drásticamente la detección de Cloudflare Turnstile.
        headless = os.getenv("BROWSER_HEADLESS", "true").lower() != "false"
        # Limpiar locks de sesión previa (si un Chromium crasheó, deja el perfil bloqueado).
        for lock in ("SingletonLock", "SingletonCookie", "SingletonSocket"):
            with contextlib.suppress(Exception):
                os.remove(os.path.join(self._profile_dir, lock))
        # channel="chrome" usa Google Chrome real (mejor evasión de Turnstile que Chromium).
        channel = self._channel if self._channel != "chromium" else None
        logger.info(
            "[Session] Lanzando browser patchright (channel=%s, headless=%s, profile=%s)",
            channel,
            headless,
            self._profile_dir,
        )
        # patchright recomienda perfil persistente + sin args que delaten automatización.
        self._pw = await async_playwright().start()
        # Args mínimos: solo lo necesario para correr en contenedor. NO se incluye
        # --disable-blink-features=AutomationControlled (patchright lo maneja; pasarlo delata).
        #
        # Bajo Xvfb no hay GPU real: Chrome lo detecta y bloquea WebGL ("WebGL1/2
        # blocklisted" en el log), y el proceso de GPU — intentando compositar de todos
        # modos — revienta (crashpad, SIGSEGV/SIGTRAP) justo al renderizar el widget de
        # Turnstile una segunda vez, tumbando todo el browser a mitad de la descarga
        # ("Target page, context or browser has been closed"). --disable-gpu por sí solo
        # NO bastó — seguía crasheando, solo que con otra señal—: hace falta forzar un
        # backend GL de software explícito (SwiftShader vía ANGLE) para que el proceso GPU
        # tenga una implementación real y estable en vez de intentar hardware inexistente.
        session_args = [
            "--no-sandbox",
            "--disable-setuid-sandbox",
            "--disable-dev-shm-usage",
            "--use-gl=angle",
            "--use-angle=swiftshader",
            "--disable-gpu-sandbox",
        ]
        launch_kwargs = {
            "headless": headless,
            "args": session_args,
            "user_agent": _BROWSER_UA,
            "viewport": {"width": 1280, "height": 720},
            "locale": "es-CO",
            "timezone_id": "America/Bogota",
            "accept_downloads": True,
        }
        if channel:
            launch_kwargs["channel"] = channel
        try:
            self._context = await self._pw.chromium.launch_persistent_context(
                self._profile_dir, **launch_kwargs
            )
        except Exception as e:
            # Si Chrome real no está disponible, caer a Chromium bundled.
            logger.warning("[Session] channel=%s falló (%s) — fallback a Chromium", channel, e)
            launch_kwargs.pop("channel", None)
            self._context = await self._pw.chromium.launch_persistent_context(
                self._profile_dir, **launch_kwargs
            )
        self._page = (
            self._context.pages[0] if self._context.pages else await self._context.new_page()
        )

        # Autenticar vía httpx para evitar el Cloudflare Turnstile de /User/AuthToken.
        # La API DIAN emite .AspNet.ApplicationCookie sin challenge de WAF. La inyectamos
        # en el browser context, evitando que el browser navegue a /User/Login.
        logger.info("[Session] Autenticando via httpx: %s", self._login_url)
        try:
            http_cookies = await self._auth_via_httpx()
            app_cookie = http_cookies.get(".AspNet.ApplicationCookie", "")
            if not app_cookie:
                raise DianDownloadError(
                    "Token DIAN inválido o expirado — httpx no obtuvo .AspNet.ApplicationCookie",
                    AUTH_LOST,
                )
            domain = urlparse(self._login_url).netloc
            await self._context.add_cookies(
                [
                    {
                        "name": ".AspNet.ApplicationCookie",
                        "value": app_cookie,
                        "domain": domain,
                        "path": "/",
                        "secure": True,
                        "httpOnly": True,
                    }
                ]
            )
            logger.info("[Session] Cookie de auth inyectada en browser (domain=%s)", domain)
        except DianDownloadError:
            raise
        except Exception as e:
            raise DianDownloadError(
                f"Auth httpx fallida: {type(e).__name__}: {e}", AUTH_LOST
            ) from e

        # Navegar a la homepage autenticada. El Cloudflare managed challenge aquí
        # se auto-resuelve en Chrome legítimo (a diferencia de /User/Login).
        logger.info("[Session] Navegando a homepage autenticada: %s", self._base_url)
        with contextlib.suppress(Exception):
            await self._page.goto(self._base_url, wait_until="networkidle", timeout=self._timeout)
        await self._handle_cloudflare(max_wait_s=45)
        with contextlib.suppress(Exception):
            await self._page.wait_for_load_state("networkidle", timeout=10000)

        url = self._page.url
        title = await self._page.title()
        cf_present = any("challenges.cloudflare.com" in f.url for f in self._page.frames)
        on_login = "/user/login" in url.lower() or "acceder" in title.lower()
        if on_login and cf_present:
            logger.error("[Session] Cloudflare bloqueó homepage — URL: %s", url)
            raise DianDownloadError(
                f"Cloudflare bloqueó el acceso al portal DIAN — URL: {url}", AUTH_LOST
            )
        elif on_login:
            logger.error("[Session] Sesión no reconocida en homepage — URL: %s", url)
            raise DianDownloadError(
                f"Cookie de auth rechazada por el portal — URL: {url}", AUTH_LOST
            )
        else:
            logger.info("[Session] Auth OK — URL: %s title: %s", url, title)

    async def _auth_via_httpx(self) -> dict:
        """Obtiene .AspNet.ApplicationCookie vía httpx sin pasar por Cloudflare."""
        from app.infrastructure.clients.external_client import HttpxExternalClient

        parsed = urlparse(self._login_url)
        qs = parse_qs(parsed.query)
        token = (qs.get("token") or [""])[0]
        pk = (qs.get("pk") or [""])[0]
        rk = (qs.get("rk") or [""])[0]
        login_base = f"{parsed.scheme}://{parsed.netloc}{parsed.path}"
        client = HttpxExternalClient(timeout=20.0)
        return await client.login(login_base, {"token": token, "pk": pk, "rk": rk})

    async def warm_up(self) -> None:
        """
        Navega a la raíz del portal (HTML) para que Chromium resuelva el challenge del
        WAF (Azure WAF JS y/o Cloudflare) y fije la cookie de clearance del dominio.
        El WAF por endpoint de descarga se resuelve además con _nav_prime por trackId.
        """
        root_url = f"{self._base_url}/"
        logger.info("[Session] Warm-up WAF en %s", root_url)
        try:
            await self._page.goto(root_url, wait_until="networkidle", timeout=self._timeout)
        except Exception as e:
            logger.info("[Session] warm-up goto: %s", type(e).__name__)
        await self._handle_cloudflare()
        logger.info(
            "[Session] Warm-up completo — URL: %s title: %s",
            self._page.url,
            await self._page.title(),
        )

    @property
    def profile_dir(self) -> str:
        return self._profile_dir

    def is_alive(self) -> bool:
        """False cuando Chrome murió a media sesión (crash real, no un timeout de descarga).

        Bajo Xvfb, Chrome crashea de forma intermitente y no determinística — a veces
        completa un batch de 24 documentos sin problema, a veces revienta en el primero,
        con la misma configuración. Cuando el proceso muere, la página y el contexto quedan
        cerrados para siempre y CADA descarga posterior fallará con el mismo
        "Target page, context or browser has been closed", aunque el documento sí exista en
        DIAN. Sin esta señal, un crash a mitad de lote condenaba el resto del batch entero a
        fallar uno por uno; con ella, el worker puede relanzar el navegador y seguir.
        """
        return self._page is not None and not self._page.is_closed()

    async def close(self) -> None:
        try:
            if self._context:
                await self._context.close()
        finally:
            if self._pw:
                await self._pw.stop()

    # ── Descarga adaptativa ────────────────────────────────────────────────
    async def download(self, track_id: str) -> bytes:
        """
        Descarga el ZIP de un documento.

        El endpoint DownloadZipFiles exige un token Cloudflare Turnstile en el param
        `captcha`. Por cada intento:
          1. _solve_turnstile: obtiene un token fresco (in-browser Chrome, o CapSolver).
          2. GET DownloadZipFiles?trackId=..&captcha=token → captura el ZIP (attachment
             vía nav, o fetch en página).
          3. Valida firma ZIP. Un token es de un solo uso, así que cada reintento pide
             uno nuevo.

        Lanza DianDownloadError si el documento no está disponible o se agotan intentos.
        """
        last_reason = "sin intentos"

        for attempt in range(1, self._max_retries + 1):
            token = await self._solve_turnstile()
            if not token:
                last_reason = "SIN_TOKEN_TURNSTILE"
                logger.warning(
                    "[Session] sin token Turnstile (intento %d/%d, trackId=%s)",
                    attempt,
                    self._max_retries,
                    track_id,
                )
                await asyncio.sleep(2**attempt)
                continue

            download_url = (
                f"{self._base_url}/Document/DownloadZipFiles"
                f"?trackId={track_id}&captcha={quote(token, safe='')}"
            )

            # Estrategia 1: navegación top-level con expect_download.
            # El browser ejecuta el JS challenge de Azure WAF, la cookie WAF queda fijada,
            # y Azure WAF hace redirect al URL original (captcha intacto) donde DIAN
            # valida el token Turnstile y envía el ZIP como Content-Disposition attachment.
            data, kind = await self._strategy_nav_download(download_url)
            if kind == VALID_ZIP and is_valid_zip(data):
                logger.info("[Session] ZIP via nav: %d bytes (trackId=%.32s…)", len(data), track_id)
                return data

            if kind == AUTH_LOST:
                raise DianDownloadError(f"Sesión perdida (AUTH_LOST) trackId={track_id}", AUTH_LOST)

            # Estrategia 2: inpage fetch con token fresco.
            # La navegación anterior fijó la cookie WAF en el contexto del browser;
            # el fetch ahora puede pasar Azure WAF y llegar a DIAN con el captcha.
            token2 = await self._solve_turnstile()
            if token2:
                download_url2 = (
                    f"{self._base_url}/Document/DownloadZipFiles"
                    f"?trackId={track_id}&captcha={quote(token2, safe='')}"
                )
                try:
                    data2, kind2 = await self._strategy_inpage_fetch(download_url2)
                    if kind2 == VALID_ZIP and is_valid_zip(data2):
                        logger.info(
                            "[Session] ZIP via fetch (post-nav): %d bytes (trackId=%.32s…)",
                            len(data2),
                            track_id,
                        )
                        return data2
                    last_reason = kind2
                except Exception as e:
                    logger.warning("[Session] fetch post-nav error: %s", e)
                    last_reason = "FETCH_ERROR"
            else:
                last_reason = kind

            if last_reason == AUTH_LOST:
                raise DianDownloadError(f"Sesión perdida (AUTH_LOST) trackId={track_id}", AUTH_LOST)

            # Si el documento no existe en DIAN y ya agotamos reintentos → terminal.
            page_url = self._page.url
            if (
                "searchinvalidqr" in page_url.lower() or "invalidqr" in page_url.lower()
            ) and attempt >= self._max_retries:
                raise DianDownloadError(
                    f"Documento no disponible en DIAN (SearchInvalidQR) trackId={track_id}",
                    DIAN_INVALID,
                )

            logger.info(
                "[Session] intento %d/%d sin ZIP (%s) trackId=%s",
                attempt,
                self._max_retries,
                last_reason,
                track_id,
            )
            await asyncio.sleep(2**attempt)

        raise DianDownloadError(
            f"Descarga fallida tras {self._max_retries} intentos "
            f"(último: {last_reason}) trackId={track_id}",
            last_reason,
        )

    # ── Resolución de Turnstile (token del param captcha) ──────────────────
    async def _solve_turnstile(self):
        """Obtiene un token Turnstile: primero in-browser (Chrome), luego CapSolver."""
        # Intento in-browser (solo si no se ha descartado ya en esta sesión).
        inbrowser_enabled = os.getenv("TURNSTILE_INBROWSER", "true").lower() != "false"
        if inbrowser_enabled and self._inbrowser_works is not False:
            token = await self._turnstile_inbrowser()
            if token:
                self._inbrowser_works = True
                logger.info("[Session] token Turnstile in-browser (len=%d)", len(token))
                return token
            # No funciona en este entorno: no reintentar in-browser el resto de la sesión.
            if self._inbrowser_works is None:
                self._inbrowser_works = False
                logger.info("[Session] in-browser no resuelve Turnstile — se usará CapSolver")
        if CAPSOLVER_API_KEY:
            token = await self._turnstile_capsolver()
            if token:
                logger.info("[Session] token Turnstile CapSolver (len=%d)", len(token))
                return token
        return None

    async def _turnstile_inbrowser(self, wait_s: int = 30):
        """
        Genera un token Turnstile in-browser navegando a la página del portal que carga
        el widget. El token se genera desde el mismo Chrome/IP que hará la descarga,
        por lo que DIAN lo acepta en la validación server-side con Cloudflare.
        """
        page_url = f"{self._base_url}{TURNSTILE_PAGE_PATH}"
        try:
            cur_url = self._page.url
            if TURNSTILE_PAGE_PATH not in cur_url:
                await self._page.goto(page_url, wait_until="networkidle", timeout=self._timeout)
            after_url = self._page.url
            logger.info("[Session] in-browser: navegó a %s", after_url)
            if "/user/login" in after_url.lower():
                logger.warning("[Session] in-browser: sesión perdida — redirigió a login")
                return None
            # Turnstile API.js carga de forma asíncrona; esperar hasta 45s.
            try:
                await self._page.wait_for_function(
                    "() => typeof window.turnstile !== 'undefined'", timeout=45000
                )
                logger.info("[Session] in-browser: window.turnstile disponible")
            except Exception as te:
                logger.warning(
                    "[Session] in-browser: window.turnstile no disponible tras 45s: %s",
                    type(te).__name__,
                )
                return None
            token = await self._page.evaluate(
                _TURNSTILE_SOLVE_JS, [TURNSTILE_SITEKEY, wait_s * 1000]
            )
            if token and not str(token).startswith(("ERR", "TIMEOUT", "NO_")):
                return token
            logger.info("[Session] turnstile in-browser sin token: %s", str(token)[:40])
        except Exception as e:
            logger.info("[Session] turnstile in-browser: %s", type(e).__name__)
        return None

    async def _turnstile_capsolver(self):
        """Resuelve el Turnstile vía CapSolver (AntiTurnstileTaskProxyLess)."""
        page_url = f"{self._base_url}{TURNSTILE_PAGE_PATH}"
        try:
            async with httpx.AsyncClient(timeout=httpx.Timeout(30.0, read=120.0)) as client:
                create = await client.post(
                    "https://api.capsolver.com/createTask",
                    json={
                        "clientKey": CAPSOLVER_API_KEY,
                        "task": {
                            "type": "AntiTurnstileTaskProxyLess",
                            "websiteURL": page_url,
                            "websiteKey": TURNSTILE_SITEKEY,
                        },
                    },
                )
                task_id = create.json().get("taskId")
                if not task_id:
                    logger.warning(
                        "[Session] CapSolver createTask sin taskId: %s", create.text[:200]
                    )
                    return None
                for _ in range(40):
                    await asyncio.sleep(3)
                    res = await client.post(
                        "https://api.capsolver.com/getTaskResult",
                        json={"clientKey": CAPSOLVER_API_KEY, "taskId": task_id},
                    )
                    body = res.json()
                    if body.get("status") == "ready":
                        return (body.get("solution") or {}).get("token")
                    if body.get("errorId"):
                        logger.warning("[Session] CapSolver error: %s", body)
                        return None
        except Exception as e:
            logger.warning("[Session] CapSolver excepción: %s", e)
        return None

    async def _nav_prime(self, download_url: str):
        """
        Navegación top-level al endpoint de descarga. Chromium ejecuta el JS del Azure
        WAF challenge y fija la cookie de clearance del dominio. Si el server responde
        con Content-Disposition attachment, captura el ZIP directamente.
        Devuelve (bytes_del_zip | None, url_resultante).
        """
        try:
            async with self._page.expect_download(timeout=6000) as dl_info:
                await self._page.goto(download_url, wait_until="commit", timeout=self._timeout)
            download = await dl_info.value
            path = await download.path()
            if path:
                with open(path, "rb") as f:  # noqa: ASYNC230 — fichero temporal local ya escrito, pocos KB
                    return f.read(), self._page.url
        except Exception as e:
            logger.debug("[Session] nav-prime sin attachment: %s", type(e).__name__)

        with contextlib.suppress(Exception):
            await self._page.wait_for_load_state("networkidle", timeout=self._timeout)
        logger.info("[Session] WAF primed via nav — page: %s", self._page.url)
        return None, self._page.url

    async def _strategy_inpage_fetch(self, download_url: str) -> tuple[bytes, str]:
        """Estrategia primaria: fetch() nativo dentro de la página (misma TLS/cookies)."""
        result = await self._page.evaluate(_INPAGE_FETCH_JS, download_url)
        status = result.get("status")
        ct = result.get("contentType", "")
        final_url = result.get("url", "")
        b64 = result.get("bodyB64", "")
        if result.get("error"):
            logger.info("[Session] inpage fetch error JS: %s", result.get("error"))
        data = base64.b64decode(b64) if b64 else b""
        kind = self._classify(status, ct, data[:512], final_url)
        logger.info(
            "[Session] inpage fetch status=%s ct=%s bytes=%d kind=%s url=%s",
            status,
            ct,
            len(data),
            kind,
            (final_url or "")[:100],
        )
        if kind != VALID_ZIP and data and len(data) < 600:
            logger.info("[Session] cuerpo no-ZIP (%d b): %r", len(data), data[:300])
        return data, kind

    async def _strategy_cookie_handoff(self, download_url: str) -> tuple[bytes, str]:
        """Fallback: entrega las cookies del navegador a httpx (con headers XHR)."""
        raw = await self._context.cookies()
        cookies = {c["name"]: c["value"] for c in raw}
        timeout = httpx.Timeout(30.0, read=120.0)
        async with httpx.AsyncClient(
            timeout=timeout,
            follow_redirects=True,
            cookies=cookies,
            headers={"User-Agent": _BROWSER_UA},
        ) as client:
            resp = await client.get(
                download_url,
                headers={
                    "X-Requested-With": "XMLHttpRequest",
                    "Referer": f"{self._base_url}/Document",
                    "Accept": "application/zip,application/octet-stream,*/*",
                },
            )
            data = resp.content
            return data, self._classify(
                resp.status_code, resp.headers.get("content-type", ""), data[:512], str(resp.url)
            )

    async def _strategy_nav_download(self, download_url: str) -> tuple[bytes, str]:
        """Último recurso: navegación con expect_download (Content-Disposition attachment)."""
        try:
            async with self._page.expect_download(timeout=self._timeout) as dl_info:
                await self._page.goto(download_url, wait_until="commit", timeout=self._timeout)
            download = await dl_info.value
            path = await download.path()
            if path:
                with open(path, "rb") as f:  # noqa: ASYNC230 — fichero temporal local ya escrito, pocos KB
                    data = f.read()
                return data, (VALID_ZIP if is_valid_zip(data) else EMPTY_OR_HTML)
        except Exception as e:
            logger.info("[Session] nav_download sin descarga: %s", type(e).__name__)
        # No se disparó descarga — clasificar según la página resultante.
        return b"", self._classify(200, "", b"", self._page.url)

    def _classify(self, status, content_type: str, body_head: bytes, final_url: str) -> str:
        """Clasifica el resultado de una descarga para decidir la reacción adaptativa."""
        url_l = (final_url or "").lower()
        if "searchinvalidqr" in url_l or "invalidqr" in url_l:
            return DIAN_INVALID
        if "/user/login" in url_l:
            return AUTH_LOST
        if is_valid_zip(body_head):
            return VALID_ZIP

        head_txt = ""
        with contextlib.suppress(Exception):
            head_txt = body_head.decode("utf-8", "ignore").lower()

        if "azure waf" in head_txt or "jschallenge" in head_txt or "azwaf" in head_txt:
            return AZURE_WAF
        if any(
            t in head_txt
            for t in ("just a moment", "attention required", "challenges.cloudflare.com", "cf-chl")
        ):
            return CLOUDFLARE
        return EMPTY_OR_HTML

    async def _handle_cloudflare(self, max_wait_s: int = 30) -> bool:
        """
        Espera a que un challenge Cloudflare Turnstile se auto-resuelva.

        Los "managed challenges" de Cloudflare se resuelven solos si el navegador parece
        legítimo; interactuar (clics forzados) tiende a resetear el widget. Por eso aquí
        se espera de forma pasiva a que el iframe del challenge desaparezca o cambie la URL,
        con un único clic suave al widget como empujón si sigue presente a mitad de camino.

        Devuelve True si el challenge se resolvió (o no había), False si persiste.
        """
        page = self._page
        ticks = max_wait_s * 2  # polling cada 500ms
        nudged = False
        for i in range(ticks):
            cf_frames = [f for f in page.frames if "challenges.cloudflare.com" in f.url]
            title = (await page.title()).lower()
            cf_by_title = any(cf in title for cf in CLOUDFLARE_TITLES)

            if not cf_frames and not cf_by_title:
                if i > 0:
                    logger.info("[Session] Cloudflare resuelto tras %.1fs", i * 0.5)
                return True

            # Empujón único a mitad de camino: click por coordenadas del iframe del widget.
            if cf_frames and not nudged and i >= ticks // 3:
                nudged = True
                with contextlib.suppress(Exception):
                    box = await cf_frames[0].frame_element.bounding_box()
                    if box:
                        # El checkbox del widget queda cerca del borde izquierdo, centrado vertical.
                        await page.mouse.click(box["x"] + 30, box["y"] + box["height"] / 2)
                        logger.info("[Session] Empujón a Turnstile por coordenadas")

            await page.wait_for_timeout(500)

        logger.warning("[Session] Cloudflare persiste tras %ds", max_wait_s)
        return False
