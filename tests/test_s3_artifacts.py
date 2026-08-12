import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from botocore.exceptions import ClientError

from agent_capacity.s3_artifacts import (
    cleanup,
    download,
    prepare_result,
    prepare_source,
    store_identity,
    store_ready,
    validated_endpoint,
)


def values(**overrides):
    return {
        "provider": "tigris",
        "bucket": "example-bucket",
        "endpoint": "https://t3.storage.dev",
        "region": "auto",
        "addressing_style": "virtual",
        "presign_ttl_minutes": 60,
        **overrides,
    }


class TigrisArtifactTests(unittest.TestCase):
    def test_identity_contains_destination_but_no_credentials(self):
        identity = store_identity(values(secret_access_key="must-not-appear"))
        self.assertEqual(identity["provider"], "tigris")
        self.assertEqual(identity["bucket"], "example-bucket")
        self.assertNotIn("secret_access_key", identity)
        self.assertNotIn("must-not-appear", str(identity))

    def test_tigris_adapter_rejects_credential_exfiltration_endpoints(self):
        for endpoint in (
            "http://t3.storage.dev",
            "https://evil.example",
            "https://t3.storage.dev.evil.example",
            "https://t3.storage.dev/path",
            "https://user@t3.storage.dev",
        ):
            with self.subTest(endpoint=endpoint), self.assertRaises(ValueError):
                validated_endpoint(values(endpoint=endpoint))
        self.assertEqual(
            validated_endpoint(values(endpoint="https://t3.storage.dev/")),
            "https://t3.storage.dev",
        )

    def test_readiness_requires_bucket_and_credentials(self):
        ready, reason = store_ready(values(bucket=""))
        self.assertFalse(ready)
        self.assertIn("fly storage create", reason)
        session = MagicMock()
        session.get_credentials.return_value = None
        with patch("agent_capacity.s3_artifacts._session", return_value=session):
            ready, reason = store_ready(values())
        self.assertFalse(ready)
        self.assertIn("credentials", reason)

    def test_readiness_proves_the_bucket_is_reachable(self):
        session = MagicMock()
        session.get_credentials.return_value = MagicMock()
        client = MagicMock()
        with (
            patch("agent_capacity.s3_artifacts._session", return_value=session),
            patch("agent_capacity.s3_artifacts._client", return_value=client),
        ):
            ready, reason = store_ready(values())
        self.assertTrue(ready, reason)
        client.head_bucket.assert_called_once_with(Bucket="example-bucket")

        denied = ClientError(
            {"Error": {"Code": "AccessDenied"}, "ResponseMetadata": {"HTTPStatusCode": 403}},
            "HeadBucket",
        )
        client.head_bucket.side_effect = denied
        with (
            patch("agent_capacity.s3_artifacts._session", return_value=session),
            patch("agent_capacity.s3_artifacts._client", return_value=client),
        ):
            ready, reason = store_ready(values())
        self.assertFalse(ready)
        self.assertIn("not reachable", reason)

    def test_source_upload_returns_scoped_presigned_read_url(self):
        client = MagicMock()
        client.generate_presigned_url.return_value = "https://signed.example/source?signature=redacted"
        manifest = {"files": [{"path": "README.md"}], "skipped": [], "total_bytes": 10}
        job = {"id": "job-1"}
        with tempfile.TemporaryDirectory() as directory:
            bundle = Path(directory) / "source.tar.gz"
            bundle.write_bytes(b"archive")
            with patch("agent_capacity.s3_artifacts._client", return_value=client):
                artifact = prepare_source(bundle, manifest, job, values())
        client.upload_file.assert_called_once_with(str(bundle), "example-bucket", "sources/job-1.tar.gz")
        client.generate_presigned_url.assert_called_once()
        self.assertEqual(artifact["provider"], "tigris")
        self.assertEqual(artifact["key"], "sources/job-1.tar.gz")
        self.assertEqual(artifact["manifest"]["file_count"], 1)
        self.assertIn("signature=redacted", artifact["url"])

    def test_source_presign_failure_requires_verified_rollback(self):
        client = MagicMock()
        client.generate_presigned_url.side_effect = ClientError(
            {"Error": {"Code": "AccessDenied"}, "ResponseMetadata": {"HTTPStatusCode": 403}},
            "GetObject",
        )
        not_found = ClientError(
            {"Error": {"Code": "NoSuchKey"}, "ResponseMetadata": {"HTTPStatusCode": 404}},
            "HeadObject",
        )
        client.head_object.side_effect = not_found
        with tempfile.TemporaryDirectory() as directory:
            bundle = Path(directory) / "source.tar.gz"
            bundle.write_bytes(b"archive")
            with (
                patch("agent_capacity.s3_artifacts._client", return_value=client),
                self.assertRaises(ClientError),
            ):
                prepare_source(bundle, {"files": [], "skipped": [], "total_bytes": 0}, {"id": "job-3"}, values())
        client.delete_object.assert_called_once_with(
            Bucket="example-bucket", Key="sources/job-3.tar.gz"
        )

    def test_source_presign_failure_reports_unverified_rollback(self):
        client = MagicMock()
        client.generate_presigned_url.side_effect = RuntimeError("presign failed")
        client.head_object.return_value = {"ContentLength": 7}
        with tempfile.TemporaryDirectory() as directory:
            bundle = Path(directory) / "source.tar.gz"
            bundle.write_bytes(b"archive")
            with (
                patch("agent_capacity.s3_artifacts._client", return_value=client),
                self.assertRaisesRegex(RuntimeError, "uploaded object still exists"),
            ):
                prepare_source(bundle, {"files": [], "skipped": [], "total_bytes": 0}, {"id": "job-4"}, values())

    def test_ambiguous_source_upload_failure_still_requires_verified_rollback(self):
        client = MagicMock()
        client.upload_file.side_effect = RuntimeError("connection dropped after upload")
        not_found = ClientError(
            {"Error": {"Code": "NoSuchKey"}, "ResponseMetadata": {"HTTPStatusCode": 404}},
            "HeadObject",
        )
        client.head_object.side_effect = not_found
        with tempfile.TemporaryDirectory() as directory:
            bundle = Path(directory) / "source.tar.gz"
            bundle.write_bytes(b"archive")
            with (
                patch("agent_capacity.s3_artifacts._client", return_value=client),
                self.assertRaisesRegex(RuntimeError, "connection dropped after upload"),
            ):
                prepare_source(
                    bundle,
                    {"files": [], "skipped": [], "total_bytes": 0},
                    {"id": "job-ambiguous"},
                    values(),
                )
        client.delete_object.assert_called_once_with(
            Bucket="example-bucket", Key="sources/job-ambiguous.tar.gz"
        )

    def test_result_upload_url_outlives_the_approved_runtime(self):
        client = MagicMock()
        client.generate_presigned_url.return_value = "https://signed.example/result"
        job = {"id": "job-2", "max_runtime_seconds": 7200}
        with patch("agent_capacity.s3_artifacts._client", return_value=client):
            artifact = prepare_result(job, values(presign_ttl_minutes=5), ["dist"])
        call = client.generate_presigned_url.call_args
        client.head_bucket.assert_called_once_with(Bucket="example-bucket")
        self.assertGreaterEqual(call.kwargs["ExpiresIn"], 9000)
        self.assertEqual(call.kwargs["HttpMethod"], "PUT")
        self.assertEqual(artifact["requested_paths"], ["dist"])

    def test_result_setup_proves_bucket_access_before_signing(self):
        client = MagicMock()
        client.head_bucket.side_effect = ClientError(
            {"Error": {"Code": "NoSuchBucket"}, "ResponseMetadata": {"HTTPStatusCode": 404}},
            "HeadBucket",
        )
        with (
            patch("agent_capacity.s3_artifacts._client", return_value=client),
            self.assertRaises(ClientError),
        ):
            prepare_result({"id": "job-5", "max_runtime_seconds": 600}, values(), [])
        client.generate_presigned_url.assert_not_called()

    def test_result_setup_rejects_runtime_beyond_signing_limit(self):
        client = MagicMock()
        with (
            patch("agent_capacity.s3_artifacts._client", return_value=client),
            self.assertRaisesRegex(ValueError, "seven days"),
        ):
            prepare_result(
                {"id": "job-6", "max_runtime_seconds": 7 * 24 * 60 * 60}, values(), []
            )
        client.head_bucket.assert_not_called()

    def test_cleanup_requires_verified_absence(self):
        client = MagicMock()
        not_found = ClientError(
            {"Error": {"Code": "NoSuchKey"}, "ResponseMetadata": {"HTTPStatusCode": 404}},
            "HeadObject",
        )
        client.head_object.side_effect = not_found
        artifact = {"bucket": "example-bucket", "key": "results/job-2.tar.gz"}
        with patch("agent_capacity.s3_artifacts._client", return_value=client):
            result = cleanup(artifact, values())
        self.assertTrue(result["deleted"])
        self.assertTrue(result["verified_absent"])
        client.delete_object.assert_called_once_with(Bucket="example-bucket", Key="results/job-2.tar.gz")

    def test_download_uses_authenticated_client_without_exposing_a_url(self):
        client = MagicMock()
        artifact = {"bucket": "example-bucket", "key": "results/job-2.tar.gz"}
        with tempfile.TemporaryDirectory() as directory:
            destination = Path(directory) / "result.tar.gz"
            with patch("agent_capacity.s3_artifacts._client", return_value=client):
                ok, error = download(artifact, destination, values())
        self.assertTrue(ok)
        self.assertEqual(error, "")
        client.download_file.assert_called_once_with(
            "example-bucket", "results/job-2.tar.gz", str(destination)
        )


if __name__ == "__main__":
    unittest.main()
