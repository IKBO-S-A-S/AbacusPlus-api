import uuid
from dataclasses import dataclass
from typing import Optional

import boto3
from botocore.config import Config
from botocore.exceptions import (
    BotoCoreError,
    ClientError,
    EndpointConnectionError,
    NoCredentialsError,
)

_TIMEOUT_SECONDS = 8

_CLIENT_ERROR_MESSAGES = {
    "InvalidAccessKeyId": "El Access Key ID no existe.",
    "SignatureDoesNotMatch": "El Secret Access Key no corresponde al Access Key ID.",
    "NoSuchBucket": "El bucket no existe.",
    "AccessDenied": "Las credenciales no tienen permiso sobre el bucket (se requiere escritura).",
    "403": "Las credenciales no tienen permiso sobre el bucket.",
    "404": "El bucket no existe.",
    "301": "El bucket esta en otra region.",
    "PermanentRedirect": "El bucket esta en otra region.",
    "AuthorizationHeaderMalformed": "La region no corresponde a la del bucket.",
}


@dataclass(frozen=True)
class S3ConnectionResult:
    ok: bool
    message: str


class S3ConnectionChecker:
    """Comprueba que unas credenciales S3 permiten alcanzar y escribir en el bucket.

    Hace `head_bucket` y luego sube y borra un objeto de prueba: es la misma
    operacion (`put_object`) que usa xml-processor al publicar documentos, asi que
    un resultado exitoso garantiza que la publicacion funcionara. Nunca lanza.
    """

    def check(
        self,
        *,
        bucket_name: str,
        region: str,
        access_key_id: str,
        secret_access_key: str,
        endpoint_url: Optional[str] = None,
        key_prefix: str = "",
    ) -> S3ConnectionResult:
        try:
            client = boto3.client(
                "s3",
                region_name=region,
                aws_access_key_id=access_key_id,
                aws_secret_access_key=secret_access_key,
                endpoint_url=endpoint_url or None,
                config=Config(
                    connect_timeout=_TIMEOUT_SECONDS,
                    read_timeout=_TIMEOUT_SECONDS,
                    retries={"max_attempts": 1},
                ),
            )
            client.head_bucket(Bucket=bucket_name)
            prefix = key_prefix.strip("/")
            probe = f".abacus-connection-test-{uuid.uuid4().hex}"
            key = f"{prefix}/{probe}" if prefix else probe
            client.put_object(Bucket=bucket_name, Key=key, Body=b"")
            client.delete_object(Bucket=bucket_name, Key=key)
        except ClientError as exc:
            code = str(exc.response.get("Error", {}).get("Code", ""))
            return S3ConnectionResult(
                ok=False,
                message=_CLIENT_ERROR_MESSAGES.get(code, f"S3 rechazo la conexion ({code or 'error'})."),
            )
        except NoCredentialsError:
            return S3ConnectionResult(ok=False, message="Faltan las credenciales de acceso.")
        except EndpointConnectionError:
            return S3ConnectionResult(
                ok=False, message="No se pudo conectar con el endpoint de S3. Revise endpoint y region."
            )
        except (BotoCoreError, ValueError) as exc:
            return S3ConnectionResult(ok=False, message=f"No se pudo validar la conexion: {exc}")
        return S3ConnectionResult(ok=True, message="Conexion exitosa: el bucket es accesible y escribible.")
