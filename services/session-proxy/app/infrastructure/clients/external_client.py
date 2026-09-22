import logging
import os
import re
from typing import Any, Optional

import httpx

from app.domain.exceptions.base import ExternalAuthException, ExternalRequestException
from app.domain.ports.services import ExternalClientPort

logger = logging.getLogger(__name__)


def _parse_cookie_from_headers(response, name: str) -> str:
    """Extrae el valor de una cookie directamente de los headers Set-Cookie de la response.
    Evita httpx.CookieConflict cuando el jar acumula múltiples cookies con el mismo nombre."""
    for header_val in response.headers.get_list("set-cookie"):
        prefix = f"{name}="
        if prefix in header_val:
            value = header_val.split(prefix, 1)[1].split(";")[0].strip()
            if value:
                return value
    return ""


_BROWSER_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "es-CO,es;q=0.9,en;q=0.8",
}


class HttpxExternalClient(ExternalClientPort):
    """
    Cliente httpx sin estado. Se crea un AsyncClient fresco por llamada para
    evitar fugas de cookies entre sesiones distintas.
    """

    def __init__(self, timeout: float = 15.0):
        self._timeout = timeout
        self._fixed_pk = os.getenv("EXTERNAL_FIXED_PK", "")
        self._fixed_rk = os.getenv("EXTERNAL_FIXED_RK", "")

    def _build_login_params(self, token: str, pk: str = "", rk: str = "") -> dict[str, str]:
        params: dict[str, str] = {"token": token}
        effective_pk = pk or self._fixed_pk
        effective_rk = rk or self._fixed_rk
        if effective_pk:
            params["pk"] = effective_pk
        if effective_rk:
            params["rk"] = effective_rk
        return params

    async def login(self, login_url: str, credentials: dict[str, Any]) -> dict[str, str]:
        """
        Autentica contra el portal externo vía GET con query params pk, rk y token.
        Captura y retorna las cookies del response como dict {nombre: valor}.
        """
        params = self._build_login_params(
            credentials["token"], pk=credentials.get("pk", ""), rk=credentials.get("rk", "")
        )
        base_url = login_url.split("/User/")[0]
        try:
            async with httpx.AsyncClient(
                timeout=self._timeout,
                follow_redirects=True,
                headers=_BROWSER_HEADERS,
            ) as client:
                # Paso 1: visitar el portal para obtener cookies de infraestructura
                # (ARRAffinity, ASP.NET_SessionId, __RequestVerificationToken)
                pre = await client.get(base_url)
                if pre.status_code == 403:
                    raise ExternalAuthException(
                        f"El portal bloqueó el acceso (403 Forbidden) desde esta IP. "
                        f"Verifica conectividad de red o usa un proxy/VPN con IP colombiana. "
                        f"URL: {base_url}"
                    )
                logger.info("Cookies de infraestructura capturadas: %d", len(client.cookies))

                # Paso 2: autenticar sin seguir redirects — el segundo redirect borra
                # .AspNet.ApplicationCookie, por lo que hay que capturarla aquí antes.
                response = await client.get(login_url, params=params, follow_redirects=False)
                if response.status_code == 403:
                    raise ExternalAuthException(
                        "El portal bloqueó la autenticación (403 Forbidden). "
                        "Verifica conectividad de red con el portal DIAN."
                    )
                # Parsear desde Set-Cookie header — evita CookieConflict cuando el jar
                # acumula múltiples cookies con el mismo nombre tras redirects.
                app_cookie = _parse_cookie_from_headers(response, ".AspNet.ApplicationCookie")
                if not app_cookie:
                    raise ExternalAuthException(
                        f"Token inválido o expirado: el portal retornó {response.status_code} "
                        f"pero no emitió .AspNet.ApplicationCookie"
                    )
                # Construir el dict de cookies iterando el jar directamente para evitar
                # CookieConflict — httpx lanza esa excepción si hay dos cookies con el
                # mismo nombre (puede ocurrir tras el redirect chain del paso 1).
                cookies: dict[str, str] = {}
                for c in client.cookies.jar:
                    cookies[c.name] = c.value
                # Asegurar que la cookie de auth (parseada de headers) es la correcta.
                cookies[".AspNet.ApplicationCookie"] = app_cookie
                logger.info(
                    "Login externo exitoso: %s — %d cookie(s) capturadas",
                    login_url,
                    len(cookies),
                )
                return cookies
        except ExternalAuthException:
            raise
        except httpx.HTTPStatusError as exc:
            raise ExternalAuthException(
                f"El portal externo retornó {exc.response.status_code}"
            ) from exc
        except httpx.RequestError as exc:
            raise ExternalAuthException(f"No se pudo conectar al portal externo: {exc}") from exc

    async def login_and_request(
        self,
        login_url: str,
        credentials: dict[str, Any],
        method: str,
        url: str,
        body: Optional[dict[str, Any]] = None,
        params: Optional[dict[str, Any]] = None,
    ) -> dict[str, Any]:
        """
        Autentica y hace la petición en un único AsyncClient para que las cookies
        conserven sus atributos originales (dominio, path, flags Secure/HttpOnly).
        El portal DIAN requiere form-encoding y el token CSRF como campo del body.
        """
        login_params = self._build_login_params(
            credentials["token"], pk=credentials.get("pk", ""), rk=credentials.get("rk", "")
        )
        base_url = login_url.split("/User/")[0]
        # Timeout escalonado: auth/navegación 15s, lectura del endpoint DIAN hasta 60s.
        request_timeout = httpx.Timeout(self._timeout, read=60.0)
        try:
            async with httpx.AsyncClient(
                timeout=request_timeout,
                follow_redirects=True,
                headers=_BROWSER_HEADERS,
            ) as client:
                # Paso 1: visitar el portal para obtener cookies de infraestructura
                logger.info("[DIAN paso 1] GET %s", base_url)
                pre = await client.get(base_url)
                logger.info(
                    "[DIAN paso 1] status=%s cookies=%d", pre.status_code, len(client.cookies)
                )
                if pre.status_code == 403:
                    raise ExternalAuthException(
                        f"El portal bloqueó el acceso (403 Forbidden) desde esta IP. "
                        f"Verifica conectividad de red o usa un proxy/VPN con IP colombiana. "
                        f"URL: {base_url}"
                    )

                # Paso 2: autenticar sin seguir redirects — el segundo redirect borra
                # .AspNet.ApplicationCookie, por lo que hay que capturarla aquí antes.
                logger.info("[DIAN paso 2] GET %s", login_url)
                auth_response = await client.get(
                    login_url, params=login_params, follow_redirects=False
                )
                logger.info("[DIAN paso 2] status=%s", auth_response.status_code)
                if auth_response.status_code == 403:
                    raise ExternalAuthException(
                        "El portal bloqueó la autenticación (403 Forbidden). "
                        "Verifica conectividad de red con el portal DIAN."
                    )
                app_cookie = _parse_cookie_from_headers(auth_response, ".AspNet.ApplicationCookie")
                if not app_cookie:
                    raise ExternalAuthException(
                        f"Token inválido o expirado: el portal retornó {auth_response.status_code} "
                        f"pero no emitió .AspNet.ApplicationCookie"
                    )
                client.cookies.set(".AspNet.ApplicationCookie", app_cookie)

                # Paso 3: visitar la raíz autenticada para obtener un __RequestVerificationToken
                # válido para la sesión activa. El token del paso 1 queda obsoleto tras el login.
                logger.info("[DIAN paso 3] GET %s (dashboard CSRF)", base_url)
                dashboard = await client.get(base_url)
                logger.info(
                    "[DIAN paso 3] status=%s len=%d", dashboard.status_code, len(dashboard.text)
                )
                csrf_match = re.search(
                    r'name="__RequestVerificationToken"\s+type="hidden"\s+value="([^"]+)"',
                    dashboard.text,
                )
                if not csrf_match:
                    raise ExternalAuthException(
                        "No se encontró __RequestVerificationToken en la página autenticada"
                    )
                csrf_token = csrf_match.group(1)
                logger.info(
                    "Login exitoso: ApplicationCookie + CSRF token obtenidos — realizando %s %s",
                    method,
                    url,
                )

                # Paso 4: petición como form-encoded (el portal rechaza JSON en estos endpoints).
                # El token CSRF va como campo del body, no como header. Los booleanos se
                # serializan en minúscula porque el model binder de ASP.NET MVC espera
                # "true"/"false" — Python str(bool) produce "True"/"False" y rompe el binding.
                form_data = {
                    k: (str(v).lower() if isinstance(v, bool) else str(v))
                    for k, v in (body or {}).items()
                }
                form_data["__RequestVerificationToken"] = csrf_token

                logger.info("[DIAN paso 4] %s %s", method, url)
                response = await client.request(
                    method=method,
                    url=url,
                    data=form_data,
                    params=params,
                    headers={"X-Requested-With": "XMLHttpRequest"},
                    follow_redirects=False,
                )
                logger.info("[DIAN paso 4] status=%s", response.status_code)

                # Si el portal retorna 302, seguimos el redirect manualmente con GET
                if response.status_code in (301, 302, 303, 307, 308):
                    redirect_url = response.headers.get("location", "")
                    if not redirect_url.startswith("http"):
                        redirect_url = f"{base_url}{redirect_url}"
                    logger.info("Redirect %s → GET %s", response.status_code, redirect_url)
                    response = await client.get(redirect_url)

                response.raise_for_status()

                try:
                    response_body = response.json()
                except Exception:
                    response_body = response.text

                return {
                    "status_code": response.status_code,
                    "body": response_body,
                    "headers": dict(response.headers),
                    "request_body": body,
                }
        except ExternalAuthException:
            raise
        except httpx.HTTPStatusError as exc:
            raise ExternalRequestException(
                f"El portal retornó {exc.response.status_code} para {method} {url}"
            ) from exc
        except httpx.RequestError as exc:
            raise ExternalRequestException(
                f"Fallo la petición al portal externo ({type(exc).__name__}): {exc or url}"
            ) from exc

    async def login_and_download(
        self,
        login_url: str,
        credentials: dict[str, Any],
        download_url: str,
    ) -> bytes:
        """
        Autentica y descarga contenido binario en un único AsyncClient.
        Timeout de lectura extendido a 120s para ZIPs grandes.
        """
        login_params = self._build_login_params(
            credentials["token"], pk=credentials.get("pk", ""), rk=credentials.get("rk", "")
        )
        base_url = login_url.split("/User/")[0]
        timeout = httpx.Timeout(self._timeout, read=120.0)
        try:
            async with httpx.AsyncClient(
                timeout=timeout,
                follow_redirects=True,
                headers=_BROWSER_HEADERS,
            ) as client:
                pre = await client.get(base_url)
                if pre.status_code == 403:
                    raise ExternalAuthException(
                        f"El portal bloqueó el acceso (403 Forbidden) desde esta IP. "
                        f"Verifica conectividad de red o usa un proxy/VPN con IP colombiana. "
                        f"URL: {base_url}"
                    )
                # Sin seguir redirects — el segundo redirect borra ApplicationCookie
                auth_response = await client.get(
                    login_url, params=login_params, follow_redirects=False
                )
                if auth_response.status_code == 403:
                    raise ExternalAuthException(
                        "El portal bloqueó la autenticación (403 Forbidden). "
                        "Verifica conectividad de red con el portal DIAN."
                    )
                app_cookie = _parse_cookie_from_headers(auth_response, ".AspNet.ApplicationCookie")
                if not app_cookie:
                    raise ExternalAuthException(
                        f"Token inválido o expirado: el portal retornó {auth_response.status_code} "
                        f"pero no emitió .AspNet.ApplicationCookie"
                    )
                client.cookies.set(".AspNet.ApplicationCookie", app_cookie)

                # Visitar el listado de documentos para establecer contexto de sesión.
                # El portal verifica que el cliente haya navegado a esta sección antes de
                # permitir descargas; sin este paso devuelve 403.
                doc_list_url = f"{base_url}/Document"
                await client.get(doc_list_url)
                logger.info("Login exitoso para descarga: %s", download_url)

                response = await client.get(
                    download_url,
                    headers={
                        "Referer": doc_list_url,
                        "Accept": "application/zip,application/octet-stream,*/*",
                    },
                )
                logger.info(
                    "Download status: %s ct: %s",
                    response.status_code,
                    response.headers.get("content-type"),
                )
                response.raise_for_status()

                if len(response.content) == 0:
                    raise ExternalRequestException(f"Respuesta vacía al descargar {download_url}")

                logger.info("ZIP descargado: %s (%d bytes)", download_url, len(response.content))
                return response.content
        except (ExternalAuthException, ExternalRequestException):
            raise
        except httpx.HTTPStatusError as exc:
            body_snippet = exc.response.text[:400] if exc.response.text else ""
            logger.error(
                "Download 403 body: %s | url: %s | cookies: %s",
                body_snippet,
                download_url,
                list(exc.response.request.headers.get("cookie", "")[:100]),
            )
            raise ExternalRequestException(
                f"El portal retornó {exc.response.status_code} para GET {download_url} — body: {body_snippet[:200]}"
            ) from exc
        except httpx.RequestError as exc:
            raise ExternalRequestException(f"Falló la descarga del portal externo: {exc}") from exc

    async def login_debug(self, login_url: str, credentials: dict[str, Any]) -> dict[str, Any]:
        """[DEBUG] Ejecuta todos los pasos del login mostrando cookies por etapa."""
        params = self._build_login_params(
            credentials["token"], pk=credentials.get("pk", ""), rk=credentials.get("rk", "")
        )
        base_url = login_url.split("/User/")[0]
        try:
            async with httpx.AsyncClient(
                timeout=self._timeout,
                follow_redirects=True,
                headers=_BROWSER_HEADERS,
            ) as client:
                # Paso 1: visita previa al portal
                pre_response = await client.get(base_url)
                cookies_step1 = dict(client.cookies)

                # Paso 2a: auth sin seguir redirect (captura cookie del primer redirect)
                auth_no_follow = await client.get(login_url, params=params, follow_redirects=False)
                raw_cookies_step2a = dict(auth_no_follow.cookies)

                # Paso 2b: auth siguiendo todos los redirects (ve estado final)
                auth_follow = await client.get(login_url, params=params, follow_redirects=True)
                cookies_step2b = dict(client.cookies)

                return {
                    "step1_pre_visit": {
                        "url": str(pre_response.url),
                        "status": pre_response.status_code,
                        "cookies": cookies_step1,
                        "cookie_count": len(cookies_step1),
                        "set_cookie_headers": pre_response.headers.get_list("set-cookie"),
                    },
                    "step2a_auth_no_follow": {
                        "url": str(auth_no_follow.url),
                        "status": auth_no_follow.status_code,
                        "location_header": auth_no_follow.headers.get("location"),
                        "set_cookie_headers": auth_no_follow.headers.get_list("set-cookie"),
                        "cookies_in_response": raw_cookies_step2a,
                        "has_app_cookie": ".AspNet.ApplicationCookie" in raw_cookies_step2a,
                        "response_body_snippet": auth_no_follow.text[:500],
                    },
                    "step2b_auth_follow_all": {
                        "url": str(auth_follow.url),
                        "status": auth_follow.status_code,
                        "set_cookie_headers": auth_follow.headers.get_list("set-cookie"),
                        "cookies_after_follow": cookies_step2b,
                        "has_app_cookie": ".AspNet.ApplicationCookie" in cookies_step2b,
                        "response_body_snippet": auth_follow.text[:500],
                    },
                    "request_params": params,
                    "total_cookies": cookies_step2b,
                    "total_cookie_count": len(cookies_step2b),
                }
        except httpx.RequestError as exc:
            return {"error": str(exc)}

    async def request(
        self,
        method: str,
        url: str,
        cookies: dict[str, str],
        body: Optional[dict[str, Any]] = None,
        params: Optional[dict[str, Any]] = None,
    ) -> dict[str, Any]:
        """
        Reenvía una petición al portal externo inyectando las cookies de sesión.
        """
        try:
            _no_cache_headers = {
                "Cache-Control": "no-cache, no-store, must-revalidate",
                "Pragma": "no-cache",
                "X-Requested-With": "XMLHttpRequest",
            }
            async with httpx.AsyncClient(
                timeout=self._timeout,
                cookies=cookies,
                follow_redirects=True,
                headers=_no_cache_headers,
            ) as client:
                response = await client.request(
                    method=method,
                    url=url,
                    json=body,
                    params=params,
                )
                response.raise_for_status()
                try:
                    response_body = response.json()
                except Exception:
                    response_body = response.text

                return {
                    "status_code": response.status_code,
                    "body": response_body,
                    "headers": dict(response.headers),
                    "request_body": body,
                }
        except httpx.HTTPStatusError as exc:
            raise ExternalRequestException(
                f"El portal retornó {exc.response.status_code} para {method} {url}"
            ) from exc
        except httpx.RequestError as exc:
            raise ExternalRequestException(f"Fallo la petición al portal externo: {exc}") from exc
