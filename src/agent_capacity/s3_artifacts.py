"""S3-compatible artifact transport used by the Fly-native Tigris path."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import boto3
from botocore.client import Config
from botocore.exceptions import BotoCoreError, ClientError

DEFAULT_TIGRIS_ENDPOINT = "https://t3.storage.dev"
MAX_PRESIGN_SECONDS = 604800


def validated_endpoint(values: dict[str, Any]) -> str:
    endpoint = str(values.get("endpoint", DEFAULT_TIGRIS_ENDPOINT)).rstrip("/")
    parsed = urlparse(endpoint)
    if (
        parsed.scheme != "https"
        or parsed.hostname != "t3.storage.dev"
        or parsed.port not in {None, 443}
        or parsed.username is not None
        or parsed.password is not None
        or parsed.path not in {"", "/"}
        or parsed.params
        or parsed.query
        or parsed.fragment
    ):
        raise ValueError("Tigris endpoint must be https://t3.storage.dev")
    return DEFAULT_TIGRIS_ENDPOINT


def store_identity(values: dict[str, Any]) -> dict[str, str]:
    """Return the non-secret destination identity bound into a trust receipt."""
    return {
        "provider": "tigris",
        "bucket": str(values.get("bucket", "")),
        "endpoint": validated_endpoint(values),
        "region": str(values.get("region", "auto")),
    }


def store_ready(values: dict[str, Any]) -> tuple[bool, str]:
    if values.get("provider") != "tigris":
        return False, "artifact_store.provider must be tigris"
    if not values.get("bucket"):
        return False, "artifact_store.bucket is required; create one with `fly storage create`"
    try:
        validated_endpoint(values)
    except ValueError as error:
        return False, str(error)
    try:
        credentials = _session(values).get_credentials()
    except (BotoCoreError, RuntimeError) as error:
        return False, f"S3 credentials are unavailable: {type(error).__name__}"
    if credentials is None:
        return False, "S3 credentials are unavailable; set the credentials printed by `fly storage create`"
    try:
        _client(values).head_bucket(Bucket=str(values["bucket"]))
    except (BotoCoreError, ClientError, OSError) as error:
        return False, f"Tigris bucket is not reachable: {type(error).__name__}"
    return True, "ready"


def _session(values: dict[str, Any]) -> boto3.session.Session:
    profile = values.get("profile")
    return boto3.session.Session(profile_name=profile) if profile else boto3.session.Session()


def _client(values: dict[str, Any]):
    endpoint = validated_endpoint(values)
    region = str(values.get("region", "auto"))
    addressing_style = str(values.get("addressing_style", "virtual"))
    return _session(values).client(
        "s3",
        endpoint_url=endpoint,
        region_name=region,
        config=Config(signature_version="s3v4", s3={"addressing_style": addressing_style}),
    )


def _ttl_seconds(values: dict[str, Any], minimum_minutes: int = 0) -> int:
    minutes = max(int(values.get("presign_ttl_minutes", 60)), minimum_minutes)
    seconds = max(60, minutes * 60)
    if seconds > MAX_PRESIGN_SECONDS:
        raise ValueError("artifact URL lifetime cannot exceed seven days")
    return seconds


def prepare_source(
    bundle: Path,
    manifest: dict[str, Any],
    job: dict[str, Any],
    values: dict[str, Any],
) -> dict[str, Any]:
    client = _client(values)
    bucket = str(values["bucket"])
    key = f"sources/{job['id']}.tar.gz"
    try:
        client.upload_file(str(bundle), bucket, key)
        url = client.generate_presigned_url(
            "get_object",
            Params={"Bucket": bucket, "Key": key},
            ExpiresIn=_ttl_seconds(values),
            HttpMethod="GET",
        )
    except Exception as upload_error:
        try:
            client.delete_object(Bucket=bucket, Key=key)
            client.head_object(Bucket=bucket, Key=key)
        except ClientError as cleanup_error:
            status = int(cleanup_error.response.get("ResponseMetadata", {}).get("HTTPStatusCode", 0))
            code = str(cleanup_error.response.get("Error", {}).get("Code", ""))
            if status == 404 or code in {"404", "NoSuchKey", "NotFound"}:
                raise upload_error
            raise RuntimeError(
                f"source preparation failed and cleanup could not be verified for {key}"
            ) from upload_error
        except (BotoCoreError, OSError) as cleanup_error:
            raise RuntimeError(
                f"source preparation failed and cleanup could not be verified for {key}"
            ) from cleanup_error
        raise RuntimeError(
            f"source preparation failed and uploaded object still exists at {key}"
        ) from upload_error
    identity = store_identity(values)
    expires_in = _ttl_seconds(values)
    return {
        "provider": "tigris",
        "bucket": bucket,
        "endpoint": identity["endpoint"],
        "region": identity["region"],
        "key": key,
        "url": url,
        "expires_at": (datetime.now(UTC) + timedelta(seconds=expires_in)).isoformat(),
        "manifest": {
            "file_count": len(manifest["files"]),
            "skipped_count": len(manifest["skipped"]),
            "total_bytes": manifest["total_bytes"],
            "skipped": manifest["skipped"],
        },
    }


def prepare_result(
    job: dict[str, Any],
    values: dict[str, Any],
    result_paths: list[str],
) -> dict[str, Any]:
    client = _client(values)
    bucket = str(values["bucket"])
    key = f"results/{job['id']}.tar.gz"
    minimum_minutes = (int(job["max_runtime_seconds"]) + 59) // 60 + 30
    expires_in = _ttl_seconds(values, minimum_minutes)
    client.head_bucket(Bucket=bucket)
    url = client.generate_presigned_url(
        "put_object",
        Params={"Bucket": bucket, "Key": key},
        ExpiresIn=expires_in,
        HttpMethod="PUT",
    )
    identity = store_identity(values)
    return {
        "provider": "tigris",
        "bucket": bucket,
        "endpoint": identity["endpoint"],
        "region": identity["region"],
        "key": key,
        "url": url,
        "expires_in_seconds": expires_in,
        "expires_at": (datetime.now(UTC) + timedelta(seconds=expires_in)).isoformat(),
        "requested_paths": result_paths,
        "state": "awaiting_upload",
    }


def download(artifact: dict[str, Any], destination: Path, values: dict[str, Any]) -> tuple[bool, str]:
    try:
        _client(values).download_file(artifact["bucket"], artifact["key"], str(destination))
    except (BotoCoreError, ClientError, OSError) as error:
        return False, f"{type(error).__name__}: result download failed"
    return True, ""


def cleanup(artifact: dict[str, Any], values: dict[str, Any]) -> dict[str, Any]:
    client = _client(values)
    bucket = artifact["bucket"]
    key = artifact["key"]
    try:
        client.delete_object(Bucket=bucket, Key=key)
    except (BotoCoreError, ClientError) as error:
        return {
            "deleted": False,
            "verified_absent": False,
            "error": f"{type(error).__name__}: artifact deletion failed",
        }
    try:
        client.head_object(Bucket=bucket, Key=key)
    except ClientError as error:
        status = int(error.response.get("ResponseMetadata", {}).get("HTTPStatusCode", 0))
        code = str(error.response.get("Error", {}).get("Code", ""))
        absent = status == 404 or code in {"404", "NoSuchKey", "NotFound"}
        return {
            "deleted": absent,
            "already_absent": False,
            "verified_absent": absent,
            "error": "" if absent else "artifact absence check failed",
        }
    except BotoCoreError:
        return {"deleted": False, "verified_absent": False, "error": "artifact absence check failed"}
    return {"deleted": False, "verified_absent": False, "error": "artifact still exists after deletion"}
