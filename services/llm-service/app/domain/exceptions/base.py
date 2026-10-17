class DomainException(Exception):
    def __init__(self, message: str, code: str = "DOMAIN_ERROR"):
        self.message = message
        self.code = code
        super().__init__(message)


class ValidationException(DomainException):
    def __init__(self, message: str):
        super().__init__(message=message, code="VALIDATION_ERROR")


class AIServiceNotConfiguredError(DomainException):
    """El servicio no tiene una API key de OpenAI utilizable.

    Es un problema de configuración del despliegue, no de la petición ni un fallo del
    proveedor: se detecta antes de llamar a OpenAI para no devolver un 500 genérico.
    """

    MESSAGE = (
        "El servicio de IA no está configurado: falta la variable de entorno OPENAI_API_KEY "
        "en llm-service. Contacte al administrador."
    )

    def __init__(self, message: str = MESSAGE):
        super().__init__(message=message, code="AI_NOT_CONFIGURED")


class AIAuthenticationError(DomainException):
    """OpenAI rechazó la API key configurada (401): inválida, revocada o sin permisos."""

    MESSAGE = (
        "OpenAI rechazó la API key configurada en llm-service. Verifique que OPENAI_API_KEY "
        "sea válida y esté activa."
    )

    def __init__(self, message: str = MESSAGE):
        super().__init__(message=message, code="AI_AUTHENTICATION_FAILED")


class NoChartOfAccountsError(DomainException):
    """RF-08: no hay Plan Único de Cuentas cargado, así que no se sugieren cuentas.

    El alcance lo define como regla de negocio crítica: sin PUC el modelo no debe inventar
    ni sugerir cuentas de un PUC colombiano genérico, sino detener el proceso con este
    mensaje textual.
    """

    MESSAGE = "No tienes un plan único de cuenta"

    def __init__(self, message: str = MESSAGE):
        super().__init__(message=message, code="NO_CHART_OF_ACCOUNTS")


class NoRetentionCatalogError(DomainException):
    """RF-08: no hay catálogo de retenciones, así que no se pueden sugerir retenciones.

    Mismo principio que la regla del plan de cuentas: sin catálogo sincronizado el modelo
    tendría que inventar retenciones y porcentajes, así que el proceso se detiene. La
    tarifa siempre proviene del catálogo, nunca del modelo.

    Se llamó `NoTaxCatalogError` hasta que el split del 2026-08-31 separó impuestos
    (`integration_taxes`) de retenciones (`integration_retentions`): el chequeo siempre
    fue sobre el catálogo de retenciones, pero el nombre y el mensaje seguían hablando de
    impuestos, así que quien lo veía revisaba y sincronizaba la tabla equivocada.
    """

    MESSAGE = "No tienes un catálogo de retenciones sincronizado"

    def __init__(self, message: str = MESSAGE):
        super().__init__(message=message, code="NO_RETENTION_CATALOG")
