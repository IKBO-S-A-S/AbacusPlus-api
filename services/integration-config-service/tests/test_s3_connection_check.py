from unittest.mock import MagicMock, patch

from botocore.exceptions import ClientError

from app.application.dto.storage import S3StorageUpsertRequest
from app.application.use_cases.manage_storage_settings import ManageStorageSettingsUseCase
from app.infrastructure.clients.s3_connection_checker import (
    S3ConnectionChecker,
    S3ConnectionResult,
)

_KWARGS = dict(
    bucket_name="b", region="us-east-1", access_key_id="AK", secret_access_key="sk",
    endpoint_url=None, key_prefix="dian",
)


def _client_error(code):
    return ClientError({"Error": {"Code": code, "Message": "x"}}, "HeadBucket")


class TestS3ConnectionChecker:
    def test_ok_writes_and_deletes_probe(self):
        client = MagicMock()
        with patch("app.infrastructure.clients.s3_connection_checker.boto3.client", return_value=client):
            result = S3ConnectionChecker().check(**_KWARGS)
        assert result.ok
        key = client.put_object.call_args.kwargs["Key"]
        assert key.startswith("dian/.abacus-connection-test-")
        client.delete_object.assert_called_once_with(Bucket="b", Key=key)

    def test_bad_secret_reports_message(self):
        client = MagicMock()
        client.head_bucket.side_effect = _client_error("SignatureDoesNotMatch")
        with patch("app.infrastructure.clients.s3_connection_checker.boto3.client", return_value=client):
            result = S3ConnectionChecker().check(**_KWARGS)
        assert not result.ok
        assert "Secret Access Key" in result.message

    def test_no_write_permission(self):
        client = MagicMock()
        client.put_object.side_effect = _client_error("AccessDenied")
        with patch("app.infrastructure.clients.s3_connection_checker.boto3.client", return_value=client):
            result = S3ConnectionChecker().check(**_KWARGS)
        assert not result.ok


class TestUpsertS3DoesNotBlockOnFailedConnection:
    def test_saves_even_if_check_fails(self):
        s3_repo = MagicMock()
        checker = MagicMock()
        checker.check.return_value = S3ConnectionResult(ok=False, message="fallo")
        use_case = ManageStorageSettingsUseCase(s3_repo, MagicMock(), MagicMock(), checker)
        config, connection = use_case.upsert_s3(S3StorageUpsertRequest(
            bucket_name=" b ", region="us-east-1", access_key_id="AK", secret_access_key="sk",
        ))
        s3_repo.upsert.assert_called_once()
        assert config is s3_repo.upsert.return_value
        assert not connection.ok


def _resp(status, body=None):
    r = MagicMock()
    r.status_code = status
    r.content = b"x"
    r.json.return_value = body or {}
    return r


def _run_sharepoint(post, put=None):
    from app.infrastructure.clients.sharepoint_connection_checker import SharePointConnectionChecker

    client = MagicMock()
    client.__enter__.return_value = client
    client.post.return_value = post
    client.put.return_value = put
    with patch("app.infrastructure.clients.sharepoint_connection_checker.httpx.Client", return_value=client):
        result = SharePointConnectionChecker().check(
            azure_tenant_id="t", client_id="c", client_secret="s", site_id="site", drive_id="d", folder_path="F"
        )
    return result, client


class TestSharePointConnectionChecker:
    def test_ok_uploads_and_deletes(self):
        result, client = _run_sharepoint(_resp(200, {"access_token": "tok"}), _resp(201, {"id": "i1"}))
        assert result.ok
        assert client.delete.call_args.args[0].endswith("/items/i1")

    def test_bad_secret(self):
        result, _ = _run_sharepoint(_resp(401, {"error": "invalid_client"}))
        assert not result.ok and "client secret" in result.message

    def test_no_permission(self):
        result, _ = _run_sharepoint(_resp(200, {"access_token": "tok"}), _resp(403))
        assert not result.ok and "permiso" in result.message

    def test_upsert_saves_even_if_check_fails(self):
        from app.application.dto.storage import SharePointStorageUpsertRequest
        from app.infrastructure.clients.sharepoint_connection_checker import SharePointConnectionResult

        repo, checker = MagicMock(), MagicMock()
        checker.check.return_value = SharePointConnectionResult(ok=False, message="fallo")
        uc = ManageStorageSettingsUseCase(MagicMock(), repo, MagicMock(), None, checker)
        _, connection = uc.upsert_sharepoint(
            SharePointStorageUpsertRequest(
                azure_tenant_id="t", client_id="c", client_secret="s", site_id="site", drive_id="d"
            )
        )
        repo.upsert.assert_called_once()
        assert not connection.ok
