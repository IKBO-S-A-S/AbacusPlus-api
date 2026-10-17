import contextlib
import logging
import os
from typing import Any, Optional

from openai import AsyncOpenAI, AuthenticationError

from app.domain.exceptions.base import AIAuthenticationError
from app.domain.ports.services import AIServicePort

logger = logging.getLogger(__name__)


#: Tope de espera de una llamada al modelo, en segundos.
#:
#: El SDK de OpenAI trae 600 s por defecto, y eso aquí no es un valor conservador sino una
#: trampa: la sugerencia de retenciones corre con un contador esperando delante y también en
#: el disparo automático al procesar un XML. Diez minutos colgado ocupa una conexión, deja la
#: interfaz girando sin explicación y, en un lote, arrastra a los documentos que vienen
#: detrás. Es preferible fallar en un minuto y que el contador reintente.
_TIMEOUT_SEGUNDOS = float(os.getenv("OPENAI_TIMEOUT_SECONDS", "60"))

#: Reintentos del propio SDK ante errores de red o 429. Dos son los que trae por defecto; se
#: declara explícito para que se vea que la decisión está tomada y no heredada.
_MAX_RETRIES = int(os.getenv("OPENAI_MAX_RETRIES", "2"))


class OpenAIService(AIServicePort):
    def __init__(self, api_key: str):
        self._client = AsyncOpenAI(
            api_key=api_key, timeout=_TIMEOUT_SEGUNDOS, max_retries=_MAX_RETRIES
        )

    async def complete(
        self,
        prompt: str,
        model: str = "gpt-4o-mini",
        system_prompt: Optional[str] = None,
        *,
        temperature: Optional[float] = None,
        json_schema: Optional[dict[str, Any]] = None,
    ) -> dict:
        messages = []
        if system_prompt:
            messages.append({"role": "system", "content": system_prompt})
        messages.append({"role": "user", "content": prompt})

        create_kwargs: dict[str, Any] = {"model": model, "messages": messages}
        if temperature is not None:
            create_kwargs["temperature"] = temperature

        # Si se provee un JSON schema, exigimos salida estructurada (JSON estricto).
        # Algunos modelos/SDKs pueden no soportarlo; en ese caso degradamos a salida libre.
        if json_schema is not None:
            with contextlib.suppress(Exception):
                create_kwargs["response_format"] = {
                    "type": "json_schema",
                    "json_schema": json_schema,
                }

        try:
            response = await self._client.chat.completions.create(**create_kwargs)
        except AuthenticationError as exc:
            logger.error("OpenAI rechazó la API key configurada: %s", exc)
            raise AIAuthenticationError() from exc
        except Exception as exc:
            # Fallback: si el modelo o la cuenta no soportan json_schema, reintenta sin response_format.
            if json_schema is not None and "response_format" in create_kwargs:
                logger.warning(
                    "OpenAI rechazó response_format json_schema; reintentando sin schema: %s", exc
                )
                create_kwargs.pop("response_format", None)
                messages.insert(
                    0,
                    {
                        "role": "system",
                        "content": (
                            "IMPORTANTE: aunque no haya schema técnico disponible, responde solo JSON válido "
                            'con la forma {"entries": [...]} y mínimo dos líneas contables.'
                        ),
                    },
                )
                response = await self._client.chat.completions.create(**create_kwargs)
            else:
                raise
        usage = response.usage
        return {
            "content": response.choices[0].message.content or "",
            "usage": {
                "prompt_tokens": usage.prompt_tokens,
                "completion_tokens": usage.completion_tokens,
                "total_tokens": usage.total_tokens,
            },
        }
