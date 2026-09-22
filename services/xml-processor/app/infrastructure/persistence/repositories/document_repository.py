from collections import Counter
from datetime import date, datetime, timezone
from typing import Optional

from sqlalchemy.orm import Session, defer, selectinload

from app.domain.ports.repositories import DocumentRepositoryPort
from app.domain.value_objects.document_status import DocumentStatus
from app.infrastructure.persistence.models.document import Document, DocumentDetail


class DocumentRepository(DocumentRepositoryPort):
    def __init__(self, db: Session):
        self.db = db

    def get_by_document_number(self, document_number: str) -> Optional[Document]:
        return self.db.query(Document).filter(Document.document_number == document_number).first()

    def get_by_id(self, document_id: int) -> Optional[Document]:
        """Un documento por id, **sin traer el PDF ni el XML en el SELECT**.

        `pdf_data` y `xml_data` guardan el archivo completo de la factura (unos 80 KB por
        fila entre las dos, comprimidos en TOAST). Casi ningún consumidor de `get_by_id` los
        usa: el detalle del documento, la transición de estado y la contabilización leen
        campos escalares, y la respuesta de la API ni siquiera los expone. Traerlos costaba
        una lectura de TOAST y su descompresión en cada llamada — y la causación de un
        documento hace varias.

        `defer` no los hace inaccesibles: si alguien lee `doc.pdf_data` sobre la instancia,
        SQLAlchemy emite en ese momento el SELECT de esa columna. Las descargas individuales
        de PDF y XML siguen funcionando igual, pagando el coste solo ellas, que son las
        únicas que lo necesitan.
        """
        return (
            self.db.query(Document)
            .options(defer(Document.pdf_data), defer(Document.xml_data))
            .filter(Document.id == document_id)
            .first()
        )

    def get_status(self, document_id: int) -> Optional[int]:
        """Solo el estado del documento, en una sola columna.

        Existe para las guardas de transición, que necesitan saber en qué estado está el
        documento y nada más. Cargar la entidad entera para leer un entero es el patrón que
        hacía que causar un documento leyera la misma fila tres veces.

        Devuelve `None` si el documento no existe, que quien llama distingue de un estado
        válido porque ningún estado es `None`.
        """
        row = self.db.query(Document.status).filter(Document.id == document_id).first()
        return row[0] if row else None

    def get_by_date_range(
        self,
        date_start: date,
        date_end: date,
        status: Optional[int] = None,
        limit: Optional[int] = None,
    ) -> list[Document]:
        """Documentos del rango, **sin traer el PDF ni el XML**.

        `pdf_data` y `xml_data` son columnas binarias que viven en la misma tabla y guardan el
        archivo completo de cada factura: cientos de kilobytes por fila. Al construir la
        consulta con `query(Document)`, SQLAlchemy pedía todas las columnas, así que listar un
        mes de facturación transfería esos archivos desde PostgreSQL, los materializaba en
        memoria del proceso y los descartaba acto seguido — el resumen que devuelve la API no
        contiene ninguno de los dos. Con unos cientos de documentos eso son cientos de
        megabytes movidos por cada carga de la pantalla principal.

        `defer` los deja fuera del SELECT. Siguen accesibles si alguien los pide sobre una
        instancia concreta (SQLAlchemy los carga entonces bajo demanda), que es justo lo que
        hacen las descargas individuales de PDF y XML, y esas usan `get_by_id`.

        El orden es explícito porque sin `ORDER BY` PostgreSQL no garantiza ninguno: la lista
        podía reordenarse sola entre dos cargas iguales. Más reciente primero, que es como se
        revisa la facturación.
        """
        q = (
            self.db.query(Document)
            .options(
                defer(Document.pdf_data),
                defer(Document.xml_data),
                # El INC del documento se suma a partir de sus líneas. Sin esta carga
                # anticipada, cada documento del listado dispararía su propia consulta al
                # leerlo: un mes de facturación son cientos de viajes a la base.
                #
                # `selectinload` resuelve todas las líneas de la página en UNA consulta, y
                # `load_only` limita esa consulta a las dos columnas que el cálculo necesita
                # —no al detalle entero—, en la misma línea de lo que hace `defer` arriba.
                selectinload(Document.details).load_only(
                    DocumentDetail.document_id, DocumentDetail.taxes
                ),
            )
            .filter(
                Document.date >= date_start,
                Document.date <= date_end,
            )
        )
        if status is not None:
            q = q.filter(Document.status == status)
        q = q.order_by(Document.date.desc(), Document.id.desc())
        if limit is not None:
            q = q.limit(limit)
        return q.all()

    def create(self, document: Document) -> Document:
        self.db.add(document)
        self.db.commit()
        self.db.refresh(document)
        return document

    def update_status(self, document_id: int, new_status: str) -> Optional[Document]:
        doc = self.get_by_id(document_id)
        if doc is None:
            return None
        doc.status = new_status
        self.db.commit()
        # Sin `refresh`: releer la fila entera tras el commit volvía a traer el PDF y el XML
        # —el `defer` de `get_by_id` no sobrevive a un refresh explícito— para devolver un
        # resumen que no contiene ninguno de los dos. La instancia ya está en el estado
        # correcto: es la que acabamos de escribir.
        return doc

    def bulk_update_status(
        self,
        document_ids: list[int],
        new_status: int,
        expected_statuses: frozenset[int],
    ) -> dict[str, list[int]]:
        """Mueve varios documentos a `new_status` en **una sola sentencia atómica**.

        Es la transición de un lote, y por eso no puede ser un bucle de `update_status`: la
        pantalla de causación mueve decenas de documentos de golpe, y hacerlo de uno en uno
        significaba una ida y vuelta a PostgreSQL con su commit por cada documento.

        La guarda de estado va **dentro del UPDATE** (`status IN expected_statuses`), no en
        una lectura previa. Leer-y-luego-escribir deja una ventana en la que otro usuario
        —u otra pestaña del mismo— puede aprobar el documento entre las dos operaciones, y el
        lote lo degradaría en silencio revirtiendo trabajo ya confirmado. Con la condición en
        el UPDATE, el documento que cambió de estado simplemente no se toca y se reporta como
        no aplicable.

        Devuelve tres listas disjuntas que cubren todos los ids pedidos:
        - `updated`: cambiaron de estado en esta operación.
        - `unchanged`: existen y ya estaban en `new_status` (la operación es idempotente:
          reprocesar una selección donde alguno ya pasó no es un error).
        - `rejected`: existen pero su estado actual no admite la transición.
        - `not_found`: no existen.
        """
        if not document_ids:
            return {"updated": [], "unchanged": [], "rejected": [], "not_found": []}

        # Se leen los estados actuales de una vez —una columna, sin BLOBs— para poder
        # explicar por qué un documento no se movió. Es una lectura de diagnóstico: la
        # garantía de corrección la da la condición del UPDATE, no esta consulta.
        current = dict(
            self.db.query(Document.id, Document.status).filter(Document.id.in_(document_ids)).all()
        )
        not_found = [doc_id for doc_id in document_ids if doc_id not in current]
        unchanged = [doc_id for doc_id, st in current.items() if st == new_status]
        rejected = [
            doc_id
            for doc_id, st in current.items()
            if st != new_status and st not in expected_statuses
        ]
        elegibles = [doc_id for doc_id, st in current.items() if st in expected_statuses]

        updated: list[int] = []
        if elegibles:
            self.db.query(Document).filter(
                Document.id.in_(elegibles),
                Document.status.in_(expected_statuses),
            ).update({Document.status: new_status}, synchronize_session=False)
            self.db.commit()
            # Se confirma contra la base cuáles quedaron efectivamente en el nuevo estado:
            # si otra transacción movió uno entre la lectura y el UPDATE, la condición del
            # UPDATE lo excluyó y no debe reportarse como actualizado.
            confirmados = {
                row[0]
                for row in self.db.query(Document.id)
                .filter(Document.id.in_(elegibles), Document.status == new_status)
                .all()
            }
            updated = [doc_id for doc_id in elegibles if doc_id in confirmados]
            perdidos = [doc_id for doc_id in elegibles if doc_id not in confirmados]
            rejected.extend(perdidos)

        return {
            "updated": updated,
            "unchanged": unchanged,
            "rejected": rejected,
            "not_found": not_found,
        }

    # ── RF-05 / RF-06: contabilización en SIIGO ────────────────────────────────

    @staticmethod
    def _can_be_accounted(doc, force: bool = False) -> bool:
        """True si el documento puede enviarse a contabilizar.

        Dos condiciones independientes, y las dos tienen que cumplirse.

        **El cerrojo.** `accounting_locked` significa «hay un envío vivo, o hubo uno cuyo
        desenlace no se conoce». En ninguno de los dos casos puede salir otra petición hacia
        SIIGO: en el primero habría dos facturas en vuelo para el mismo documento, y en el
        segundo la factura pudo haberse creado ya —y /v1/purchases no admite
        `Idempotency-Key` que impida el duplicado—. Esta es la comprobación que sustituye al
        antiguo estado «Contabilizando», y hace exactamente el mismo trabajo.

        `force` la salta, y por eso su uso está restringido: solo debe llegar en True desde
        la reconciliación, que es el único camino en el que alguien **verificó contra SIIGO**
        que la factura no existe. Nunca desde un lote ni desde un botón de reintento.

        **El estado.** «Aprobado» es el caso normal. «Error» entra porque un documento
        rechazado por SIIGO debe poder reintentarse tras la corrección; sin esto, el ciclo
        «Error → Editar → Reintentar» sería imposible.

        La condición `accounting_error` no nulo es la que hace segura esa apertura: distingue
        el documento que falló contabilizando —y que por tanto ya pasó por la aprobación del
        contador— del que nació en Error porque su XML no se pudo procesar. Ese segundo nunca
        tuvo imputación válida y no debe llegar a SIIGO por esta puerta.
        """
        if getattr(doc, "accounting_locked", False) and not force:
            return False
        if doc.status == DocumentStatus.APROBADO:
            return True
        return doc.status == DocumentStatus.ERROR and bool(doc.accounting_error)

    def claim_for_accounting(self, document_id: int, force: bool = False) -> Optional[Document]:
        """Toma el documento para contabilizar, de forma exclusiva. Devuelve None si no puede.

        Este método es la defensa real contra la doble contabilización, y por eso hace las
        tres cosas en un orden que no se puede alterar:

        1. `SELECT ... FOR UPDATE` bloquea la fila. Un segundo proceso que pida el mismo
           documento se queda esperando aquí, no lee un estado obsoleto. Esto es lo que
           neutraliza el doble clic, las pestañas múltiples y dos usuarios a la vez: el
           segundo entra cuando el primero ya puso el cerrojo, y se va con None.
        2. Verifica la elegibilidad **después** de tener el bloqueo. Comprobarla antes sería
           inútil: entre la lectura y la escritura cabe otra transacción.
        3. Pone `accounting_locked` y **hace commit**, liberando el bloqueo de fila antes de
           que nadie llame a SIIGO. Mantener una transacción abierta durante una llamada de
           red que puede tardar 120 s bloquearía la fila todo ese tiempo y agotaría el pool
           de conexiones en cuanto se procese un lote.

        Que el commit ocurra ANTES de llamar a SIIGO es deliberado: si el proceso muere
        durante la llamada, el documento queda **con el cerrojo puesto** —visible, bloqueado
        y a la espera de reconciliación— en lugar de volver a «Aprobado», donde alguien
        podría reenviarlo y crear una segunda factura de compra real.

        A diferencia de la versión anterior, el estado funcional del documento **no cambia**
        aquí. Un documento aprobado sigue estando aprobado mientras se le envía; lo que
        cambia es que queda bloqueado. Esa separación es lo que permite conservar los cinco
        estados sin perder un ápice de la protección.
        """
        doc = self.db.query(Document).filter(Document.id == document_id).with_for_update().first()
        if doc is None:
            return None
        if not self._can_be_accounted(doc, force=force):
            # No es un error del repositorio: el caso de uso decide qué significa.
            self.db.rollback()
            return None

        doc.accounting_locked = True
        doc.accounting_started_at = datetime.now(timezone.utc)
        doc.accounting_attempts = (doc.accounting_attempts or 0) + 1
        self.db.commit()
        self.db.refresh(doc)
        return doc

    def mark_accounted(
        self,
        document_id: int,
        siigo_id: str,
        siigo_name: Optional[str] = None,
        *,
        siigo_total: Optional[float] = None,
        total_matches_dian: Optional[bool] = None,
    ) -> Optional[Document]:
        """Guarda la prueba de la contabilización y cierra el documento.

        El id de SIIGO se escribe en la MISMA transacción que el estado final: si el índice
        único `uq_documents_siigo_id` rechaza el id porque otro documento ya lo tiene, la
        transacción entera se deshace y el documento no queda marcado como contabilizado con
        una referencia que no le pertenece.

        El cerrojo se libera aquí porque el desenlace ya consta: la factura existe y tenemos
        su identificador. Es uno de los dos únicos sitios donde se abre; el otro es la
        reconciliación.
        """
        doc = self.get_by_id(document_id)
        if doc is None:
            return None
        doc.siigo_id = siigo_id
        doc.siigo_name = siigo_name
        # El total que SIIGO contabilizó, junto al documento y no solo dentro del cuerpo de la
        # respuesta en `accounting_attempts`. Un documento cerrado por reconciliación no
        # registra ningún intento, y por eso 2 de los 9 documentos contabilizados del cliente
        # no mostraban el total en su ficha de confirmación.
        if siigo_total is not None:
            doc.siigo_total = siigo_total
        if total_matches_dian is not None:
            doc.siigo_total_matches_dian = total_matches_dian
        doc.accounted_at = datetime.now(timezone.utc)
        doc.accounting_error = None
        doc.accounting_error_class = None
        doc.accounting_recommended_action = None
        doc.accounting_error_code = None
        doc.accounting_locked = False
        doc.accounting_started_at = None
        doc.status = DocumentStatus.CONTABILIZADA
        self.db.commit()
        self.db.refresh(doc)
        return doc

    def mark_accounting_failed(
        self,
        document_id: int,
        error: str,
        *,
        release: bool,
        error_class: Optional[str] = None,
        recommended_action: Optional[str] = None,
        error_code: Optional[str] = None,
    ) -> Optional[Document]:
        """Guarda el fallo clasificado. El estado funcional es **siempre** ERROR.

        Ésta es la diferencia con la implementación anterior, y es toda la idea de RF-05: el
        documento acaba en ERROR pase lo que pase, y lo que distingue un fallo de otro son
        `error_class` y `recommended_action`, no el estado. Un timeout y una cuenta PUC
        inválida se ven igual en la columna «Estado» y se comportan de forma completamente
        distinta cuando el contador hace clic.

        `release` decide qué pasa con el **cerrojo**, que es una pregunta independiente del
        estado:

        - `release=True` abre el cerrojo. Solo cuando consta que SIIGO **no** creó nada:
          validación previa fallida, 401/403/404, un 400/422 por datos inválidos, un 429 o un
          503. Desde ahí el reenvío es seguro, y la acción recomendada dirá si hace falta
          corregir antes o no.
        - `release=False` lo deja puesto. Es el caso de un timeout, un 5xx o una respuesta
          sin identificador: la factura pudo quedar creada en SIIGO y reenviarla duplicaría
          un asiento real. Ese documento solo se desbloquea reconciliando con verificación
          humana. Se ve como ERROR igual que los demás —eso es lo que pide el modelo de cinco
          estados—, pero con la acción `VERIFICAR_EN_SIIGO`, y el cerrojo es lo que hace que
          esa recomendación no sea meramente informativa.
        """
        doc = self.get_by_id(document_id)
        if doc is None:
            return None
        # El error se trunca para no desbordar la columna ni volcar respuestas enormes.
        doc.accounting_error = (error or "")[:4000]
        if error_class is not None:
            doc.accounting_error_class = error_class
        if recommended_action is not None:
            doc.accounting_recommended_action = recommended_action
        doc.accounting_error_code = error_code
        doc.status = DocumentStatus.ERROR
        if release:
            doc.accounting_locked = False
            doc.accounting_started_at = None
        self.db.commit()
        self.db.refresh(doc)
        return doc

    def release_accounting_lock(
        self,
        document_id: int,
        *,
        reason: str,
        error_class: Optional[str] = None,
        recommended_action: Optional[str] = None,
    ) -> Optional[Document]:
        """Abre el cerrojo tras una reconciliación que confirmó que SIIGO no tiene la factura.

        Existe separado de `mark_accounting_failed` porque el motivo no es un fallo nuevo:
        es la conclusión de una verificación. Mezclarlos haría que el historial no
        distinguiera «SIIGO rechazó esto» de «comprobamos que SIIGO no tenía nada», que es
        justo la distinción que un auditor necesita para entender por qué se autorizó un
        reenvío.
        """
        doc = self.get_by_id(document_id)
        if doc is None:
            return None
        doc.accounting_error = (reason or "")[:4000]
        doc.accounting_error_class = error_class
        doc.accounting_recommended_action = recommended_action
        doc.accounting_locked = False
        doc.accounting_started_at = None
        doc.status = DocumentStatus.ERROR
        self.db.commit()
        self.db.refresh(doc)
        return doc

    def get_locked_for_accounting(self) -> list:
        """Documentos con el cerrojo puesto. Son los que esperan reconciliación.

        Sustituye a la consulta por estado 350 que se hacía antes. Es la lista que permite
        vigilar que ningún documento se quede bloqueado indefinidamente sin que nadie lo vea.
        """
        return (
            self.db.query(Document)
            .filter(Document.accounting_locked.is_(True))
            .order_by(Document.accounting_started_at)
            .all()
        )

    def update_detail_codes(
        self, assignments: list[dict], code_source: Optional[str] = None
    ) -> int:
        """Actualiza campos de document_details. Solo modifica los campos presentes.

        Campos soportados: code, type, cost_center_id, tax_id.

        La presencia se mide por la clave, no por el valor: `{"cost_center_id": None}` limpia
        el centro de costo, mientras que omitir la clave lo deja intacto. Distinguir ambos
        casos es lo que permite al contador quitar una asignación; con la comprobación
        anterior (`is not None`) el borrado se ignoraba en silencio y el dato reaparecía.
        Por eso quienes llaman deben serializar con `model_dump(exclude_unset=True)`.

        RF-04: `code_source` ("llm" | "manual") registra quién asignó la cuenta. Solo se
        escribe cuando la asignación trae `code`, para que cambiar únicamente el centro de
        costo no altere el origen ya registrado.

        Cuando la asignación viene del modelo, además se guarda la propuesta en
        `code_suggested`. Una edición manual no la toca: así queda constancia de qué había
        sugerido el LLM antes de que el contador la cambiara.
        """
        updated = 0
        for item in assignments:
            row = (
                self.db.query(DocumentDetail).filter(DocumentDetail.id == item["detail_id"]).first()
            )
            if row is None:
                continue
            if "code" in item:
                row.code = item["code"]
                if code_source is not None:
                    row.code_source = code_source
                # El LLM nunca propone una cuenta vacía, así que solo se guarda la sugerencia
                # cuando trae valor: si no, un borrado manual borraría también la constancia.
                if code_source == "llm" and item["code"] is not None:
                    row.code_suggested = item["code"]
            if "type" in item:
                row.type = item["type"]
            if "cost_center_id" in item:
                row.cost_center_id = item["cost_center_id"]
            if "tax_id" in item:
                row.tax_id = item["tax_id"]
            updated += 1
        self.db.commit()
        return updated

    def update_payment_type(self, document_id: int, payment_type_id: int) -> Optional[Document]:
        row = self.db.query(Document).filter(Document.id == document_id).first()
        if row is None:
            return None
        row.payment_type_id = payment_type_id
        self.db.commit()
        self.db.refresh(row)
        return row

    def update_cost_center(
        self, document_id: int, cost_center_id: Optional[int]
    ) -> Optional[Document]:
        """RF-07: fija (o limpia, con None) el centro de costo del documento.

        Es el que se envía a SIIGO al contabilizar, porque la factura de compra solo admite
        un centro de costo general. Aceptar None permite al contador quitarlo.
        """
        row = self.db.query(Document).filter(Document.id == document_id).first()
        if row is None:
            return None
        row.cost_center_id = cost_center_id
        self.db.commit()
        self.db.refresh(row)
        return row

    def update_storage_locations(
        self, document_id: int, pdf_locations: dict, xml_locations: dict
    ) -> Optional[Document]:
        row = self.db.query(Document).filter(Document.id == document_id).first()
        if row is None:
            return None
        row.pdf_storage_locations = pdf_locations
        row.xml_storage_locations = xml_locations
        self.db.commit()
        self.db.refresh(row)
        return row

    def update_file_urls(
        self, document_id: int, pdf_url: Optional[str] = None, xml_url: Optional[str] = None
    ) -> Optional[Document]:
        """RF-03: guarda los enlaces de S3 que devolvió la API de subida.

        Solo se escriben los enlaces recibidos: publicar únicamente el PDF no debe borrar el
        enlace del XML guardado en una ejecución anterior.
        """
        row = self.db.query(Document).filter(Document.id == document_id).first()
        if row is None:
            return None
        if pdf_url is not None:
            row.pdf_url = pdf_url
        if xml_url is not None:
            row.xml_url = xml_url
        self.db.commit()
        self.db.refresh(row)
        return row

    def has_confirmed_accounting_history(self) -> bool:
        """True si el contador ya confirmó al menos una cuenta en este tenant.

        Es la condición que habilita la asignación automática al procesar un documento. En
        la primera descarga desde la DIAN no hay nada que el usuario haya validado, así que
        el modelo no tiene precedente sobre el que apoyarse: asignar entonces produce
        cuentas sin criterio que el contador debe revisar una por una, y peor, las presenta
        como si fueran una sugerencia fundamentada.

        Se cuenta solo `code_source = 'manual'`: es lo único que representa una decisión
        confirmada por una persona. Las cuentas de origen `llm` son propuestas, no
        historial, y tomarlas como tal haría que el sistema se validara a sí mismo.
        """
        return (
            self.db.query(DocumentDetail.id)
            .filter(DocumentDetail.code_source == "manual", DocumentDetail.code.isnot(None))
            .first()
            is not None
        )

    def find_most_frequent_cost_center(
        self, issuer_nit: str, description: str
    ) -> Optional[int]:
        """Busca el cost_center_id más usado históricamente para descripciones
        similares del mismo emisor. Retorna None si no hay historial."""
        words = [w for w in description.strip().split() if len(w) > 3]
        if not words:
            return None
        pattern = f"%{words[0]}%"
        rows = (
            self.db.query(DocumentDetail.cost_center_id)
            .join(Document, DocumentDetail.document_id == Document.id)
            .filter(
                Document.issuer_nit == issuer_nit,
                DocumentDetail.cost_center_id.isnot(None),
                DocumentDetail.description.ilike(pattern),
            )
            .all()
        )
        if not rows:
            return None
        counts = Counter(row.cost_center_id for row in rows)
        return counts.most_common(1)[0][0]
