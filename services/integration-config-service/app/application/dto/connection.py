from pydantic import BaseModel, Field


class ConnectionCheckResponse(BaseModel):
    ok: bool = Field(..., description="True si la conexion con el destino fue exitosa.")
    message: str = Field(
        ...,
        description="Resultado legible para el usuario.",
        examples=["Conexion exitosa: el bucket es accesible y escribible."],
    )
