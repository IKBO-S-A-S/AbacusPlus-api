from app.application.dto.issuer import IssuerListResponse
from app.application.use_cases.query_issuers import ListIssuersUseCase
from app.infrastructure.persistence.models.issuer import Issuer
from app.infrastructure.persistence.repositories.issuer_repository import IssuerRepository


def _seed(db):
    for i, (name, nit) in enumerate(
        [("Zeta SAS", "900000003"), ("Alfa SAS", "900000001"), ("Beta Ltda", "800000002")]
    ):
        db.add(Issuer(name=name, nit=nit, dv=i, email=f"{nit}@x.co", account_number="220505"))
    db.commit()


def test_lista_ordenada_por_nombre_con_total(db_session):
    _seed(db_session)
    uc = ListIssuersUseCase(IssuerRepository(db_session))

    out = IssuerListResponse.model_validate(uc.execute(None, 2, 0))

    assert [i.name for i in out.items] == ["Alfa SAS", "Beta Ltda"]
    assert out.total == 3


def test_paginacion_por_offset(db_session):
    _seed(db_session)
    uc = ListIssuersUseCase(IssuerRepository(db_session))

    out = IssuerListResponse.model_validate(uc.execute(None, 2, 2))

    assert [i.name for i in out.items] == ["Zeta SAS"]


def test_busqueda_por_nombre_nit_o_correo(db_session):
    _seed(db_session)
    uc = ListIssuersUseCase(IssuerRepository(db_session))

    assert uc.execute("alfa", 50, 0)["total"] == 1
    assert uc.execute("8000000", 50, 0)["total"] == 1
    assert uc.execute("@x.co", 50, 0)["total"] == 3


def test_devuelve_todas_las_columnas(db_session):
    _seed(db_session)
    uc = ListIssuersUseCase(IssuerRepository(db_session))

    item = IssuerListResponse.model_validate(uc.execute(None, 1, 0)).items[0].model_dump()

    assert set(item) == {c.name for c in Issuer.__table__.columns}
