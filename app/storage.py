"""Object storage, through the S3 API only, so Cloudflare R2, AWS S3 and Supabase Storage are
interchangeable. Without S3 settings, files go to a local folder (development)."""

from functools import cache
from pathlib import Path

from . import config


class LocalStore:
    def __init__(self, root: Path):
        self.root = root

    def put(self, key: str, data: bytes, content_type: str) -> None:
        path = self.root / key
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)

    def get(self, key: str) -> bytes:
        try:
            return (self.root / key).read_bytes()
        except FileNotFoundError:
            raise KeyError(key) from None


class S3Store:
    def __init__(self):
        import boto3
        from botocore.config import Config

        self.bucket = config.S3_BUCKET
        self.client = boto3.client(
            "s3", endpoint_url=config.S3_ENDPOINT, region_name=config.S3_REGION,
            aws_access_key_id=config.S3_ACCESS_KEY_ID, aws_secret_access_key=config.S3_SECRET_ACCESS_KEY,
            config=Config(s3={"addressing_style": "path"}, retries={"max_attempts": 5, "mode": "standard"}))

    def put(self, key: str, data: bytes, content_type: str) -> None:
        self.client.put_object(Bucket=self.bucket, Key=key, Body=data, ContentType=content_type)

    def get(self, key: str) -> bytes:
        try:
            return self.client.get_object(Bucket=self.bucket, Key=key)["Body"].read()
        except self.client.exceptions.NoSuchKey:
            raise KeyError(key) from None


class CachedStore:
    """Read-through disk cache in front of S3 for small, immutable files (thumbnails), so a page of
    photos doesn't make one request to the bucket per image. Disposable: the bucket is the source."""

    def __init__(self, inner, root: Path):
        self.inner, self.root = inner, root

    def put(self, key: str, data: bytes, content_type: str) -> None:
        self.inner.put(key, data, content_type)

    def get(self, key: str) -> bytes:
        if key.startswith("orig/"):  # originals are big and rarely opened
            return self.inner.get(key)
        path = self.root / key
        try:
            return path.read_bytes()
        except FileNotFoundError:
            data = self.inner.get(key)
            path.parent.mkdir(parents=True, exist_ok=True)
            tmp = path.with_suffix(".part")
            tmp.write_bytes(data)
            tmp.replace(path)
            return data


@cache
def store():
    if config.S3_BUCKET:
        return CachedStore(S3Store(), config.MEDIA_CACHE_DIR)
    return LocalStore(config.MEDIA_DIR)
