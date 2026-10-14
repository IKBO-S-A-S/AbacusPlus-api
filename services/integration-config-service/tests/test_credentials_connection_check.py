from unittest.mock import MagicMock, patch

from app.application.dto.integration import CredentialUpsertRequest
from app.application.use_cases.manage_credentials import ManageCredentialsUseCase
from app.domain.exceptions.base import ExternalAuthException, ExternalServiceException
from app.infrastructure.clients.siigo_connection_checker import (
    SiigoConnectionChecker,
    SiigoConnectionResult,
)


def _request(provider="siigo"):
    return CredentialUpsertRequest(
        provider=provider, username="u@x.co", access_key="k", base_url="https://api.siigo.com"
    )


class TestSiigoConnectionChecker:
    def _check(self, **kw):
        with patch(
            "app.infrastructure.clients.siigo_connection_checker.SiigoApiClient"
        ) as client_cls:
            client_cls.return_value.authenticate.side_effect = kw.get("error")
            if "result" in kw:
                client_cls.return_value.authenticate.side_effect = None
                client_cls.return_value.authenticate.return_value = kw["result"]
            return SiigoConnectionChecker().check(MagicMock())

    def test_ok_returns_auth_response(self):
        result = self._check(result={"access_token": "t"})
        assert result.ok and result.auth_response == {"access_token": "t"}

    def test_rejected_credentials(self):
        result = self._check(error=ExternalAuthException("401"))
        assert not result.ok and "rechazo" in result.message

    def test_unreachable(self):
        result = self._check(error=ExternalServiceException("down"))
        assert not result.ok and "conectar" in result.message


class TestUpsertCredentials:
    def test_siigo_ok_saves_token(self):
        repo, checker = MagicMock(), MagicMock()
        checker.check.return_value = SiigoConnectionResult(
            ok=True, message="ok", auth_response={"access_token": "tok", "expires_in": 100}
        )
        _, connection = ManageCredentialsUseCase(repo, checker).upsert(_request())
        assert connection.ok
        assert repo.save_token.call_args.kwargs["access_token"] == "tok"

    def test_siigo_failure_still_saves(self):
        repo, checker = MagicMock(), MagicMock()
        checker.check.return_value = SiigoConnectionResult(ok=False, message="mal")
        credential, connection = ManageCredentialsUseCase(repo, checker).upsert(_request())
        repo.upsert.assert_called_once()
        repo.save_token.assert_not_called()
        assert credential is repo.upsert.return_value and not connection.ok

    def test_other_provider_not_validated(self):
        repo, checker = MagicMock(), MagicMock()
        _, connection = ManageCredentialsUseCase(repo, checker).upsert(_request("odoo"))
        checker.check.assert_not_called()
        assert connection is None
