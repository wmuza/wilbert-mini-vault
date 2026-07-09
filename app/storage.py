"""MinIO layer.

MinIO is S3-compatible object storage that you can self-host. The API talks
to it with the same concepts you would use against AWS S3: buckets and
objects. Everything here would work unchanged against real S3 by swapping
the endpoint and credentials, which is exactly why teams prototype on MinIO.
"""

import io

from minio import Minio

from . import config

client = Minio(
    config.MINIO_ENDPOINT,
    access_key=config.MINIO_ACCESS_KEY,
    secret_key=config.MINIO_SECRET_KEY,
    secure=config.MINIO_SECURE,
)


def ensure_bucket() -> None:
    if not client.bucket_exists(config.MINIO_BUCKET):
        client.make_bucket(config.MINIO_BUCKET)


def put(object_key: str, data: bytes, content_type: str) -> None:
    client.put_object(
        config.MINIO_BUCKET,
        object_key,
        io.BytesIO(data),
        length=len(data),
        content_type=content_type,
    )


def get_stream(object_key: str):
    """Return a raw HTTP response; the caller streams and then closes it."""
    return client.get_object(config.MINIO_BUCKET, object_key)


def remove(object_key: str) -> None:
    client.remove_object(config.MINIO_BUCKET, object_key)


def list_keys() -> list[str]:
    return [
        obj.object_name
        for obj in client.list_objects(config.MINIO_BUCKET, recursive=True)
    ]


def bucket_stats() -> dict:
    """What the object store itself thinks it is holding.

    Counted from MinIO rather than from the Postgres rows on purpose: if the
    two ever disagree, the dashboard shows it instead of hiding it.
    """
    objects = 0
    total_bytes = 0
    for obj in client.list_objects(config.MINIO_BUCKET, recursive=True):
        objects += 1
        total_bytes += obj.size or 0
    return {
        "bucket": config.MINIO_BUCKET,
        "objects": objects,
        "bytes": total_bytes,
        "endpoint": config.MINIO_ENDPOINT,
    }


def ping() -> bool:
    client.bucket_exists(config.MINIO_BUCKET)
    return True
