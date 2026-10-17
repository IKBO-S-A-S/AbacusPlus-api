from abc import ABC, abstractmethod
from datetime import date
from typing import Optional


class DocumentRepositoryPort(ABC):
    @abstractmethod
    def get_by_document_number(self, document_number: str) -> Optional[object]: ...

    @abstractmethod
    def get_by_id(self, document_id: int) -> Optional[object]: ...

    @abstractmethod
    def get_by_date_range(
        self, date_start: date, date_end: date, status: Optional[int] = None
    ) -> list[object]: ...

    @abstractmethod
    def create(self, document: object) -> object: ...

    @abstractmethod
    def get_status(self, document_id: int) -> Optional[int]: ...

    @abstractmethod
    def update_status(self, document_id: int, new_status: str) -> object: ...

    @abstractmethod
    def bulk_update_status(
        self,
        document_ids: list[int],
        new_status: int,
        expected_statuses: frozenset,
    ) -> dict: ...


class ReceiverRepositoryPort(ABC):
    @abstractmethod
    def get_by_nit(self, nit: str) -> Optional[object]: ...

    @abstractmethod
    def get_all(self) -> list[object]: ...

    @abstractmethod
    def create(self, receiver: object) -> object: ...


class IssuerRepositoryPort(ABC):
    @abstractmethod
    def get_by_nit(self, nit: str) -> Optional[object]: ...

    @abstractmethod
    def create(self, issuer: object) -> object: ...

    @abstractmethod
    def search(self, q: Optional[str], limit: int, offset: int) -> tuple[list[object], int]: ...

    @abstractmethod
    def find_conflict(
        self, issuer_id: int, name: Optional[str], email: Optional[str]
    ) -> Optional[str]: ...

    @abstractmethod
    def payment_type_state(self, payment_id: int) -> Optional[bool]: ...

    @abstractmethod
    def update(self, issuer: object, fields: dict) -> object: ...


class TaxRepositoryPort(ABC):
    @abstractmethod
    def get_by_receiver_and_name(self, receiver_nit: str, tax_name: str) -> Optional[object]: ...

    @abstractmethod
    def create(self, tax: object) -> object: ...


class ConceptRepositoryPort(ABC):
    @abstractmethod
    def get_descriptions_by_receiver(self, receiver_nit: str) -> list[object]: ...

    @abstractmethod
    def find_matching_description(
        self, receiver_nit: str, description: str
    ) -> Optional[object]: ...

    @abstractmethod
    def create_description(self, concept_desc: object) -> object: ...
