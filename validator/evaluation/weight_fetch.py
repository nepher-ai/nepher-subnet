"""Download sealed Hugging Face weights on the validator host.

The files come from short-lived signed URLs issued by the Nepher backend, so no Hugging
Face credential reaches the validator or the sandbox. Every byte is hashed while it is
written and compared with the sealed manifest.
"""

from __future__ import annotations

import hashlib
import shutil
from collections.abc import Awaitable, Callable
from pathlib import Path
from urllib.parse import urlsplit

import httpx

from nepher_core.api.models import WeightLease, WeightLeaseFile
from nepher_core.custody import CustodyError, safe_relative_path
from nepher_core.utils.logging import get_logger

logger = get_logger(__name__)

_CHUNK = 1 << 20
_EXPIRED_STATUSES = frozenset({401, 403, 410})
_DISK_MARGIN_BYTES = 1 << 30


class WeightFetchError(Exception):
    """The weights could not be fetched. The agent stays pending and is retried later."""


class WeightMismatchError(WeightFetchError):
    """Downloaded bytes differ from the sealed manifest."""


class _UrlExpiredError(Exception):
    pass


class _RetryableError(Exception):
    pass


def manifest_of(lease: WeightLease) -> dict[str, str]:
    """Return ``{path: sha256}`` for a lease."""
    return {item.path: item.sha256.lower() for item in lease.files}


def total_size(lease: WeightLease) -> int:
    """Return the sum of the sealed file sizes in bytes."""
    return sum(item.size for item in lease.files)


def check_lease_shape(lease: WeightLease) -> None:
    """Reject a lease with unsafe paths, duplicate paths, or non-HTTPS URLs."""
    seen: set[str] = set()
    for item in lease.files:
        try:
            safe_relative_path(item.path)
        except CustodyError as exc:
            raise WeightFetchError(str(exc)) from exc
        if item.path in seen:
            raise WeightFetchError(f"lease lists {item.path} twice")
        seen.add(item.path)
        if item.size < 0:
            raise WeightFetchError(f"lease gives a negative size for {item.path}")
        if urlsplit(item.url).scheme != "https":
            raise WeightFetchError(f"lease URL for {item.path} is not HTTPS")


def _signature(lease: WeightLease) -> list[tuple[str, str, int]]:
    return sorted((item.path, item.sha256.lower(), item.size) for item in lease.files)


async def download_weights(
    lease: WeightLease,
    destination: Path,
    *,
    refresh_lease: Callable[[], Awaitable[WeightLease]] | None = None,
    client: httpx.AsyncClient | None = None,
    max_refreshes: int = 3,
    max_attempts: int = 3,
) -> None:
    """Download every file of *lease* into *destination*.

    Files are written to ``<name>.part`` first and renamed only after the size and SHA-256
    match. A partial file resumes with a Range request. When a URL has expired, the lease
    is refreshed through *refresh_lease* and the new manifest must equal the old one.

    Raises:
        WeightMismatchError: A file has the wrong size or hash. The file is deleted.
        WeightFetchError: Any other failure, including exhausted retries.
    """
    check_lease_shape(lease)
    destination.mkdir(parents=True, exist_ok=True)
    needed = total_size(lease) + _DISK_MARGIN_BYTES
    free = shutil.disk_usage(destination).free
    if free < needed:
        raise WeightFetchError(f"not enough disk space for weights: need {needed}, have {free}")

    owns_client = client is None
    # Redirects are refused: the signed URL is already the final location.
    http = client or httpx.AsyncClient(
        timeout=httpx.Timeout(60.0, read=300.0), follow_redirects=False
    )
    refreshes = 0
    try:
        current = {item.path: item for item in lease.files}
        for path in list(current):
            attempts = 0
            while True:
                try:
                    await _download_one(http, current[path], destination)
                    break
                except _UrlExpiredError:
                    if refresh_lease is None or refreshes >= max_refreshes:
                        raise WeightFetchError(f"download URL for {path} expired") from None
                    refreshes += 1
                    fresh = await refresh_lease()
                    check_lease_shape(fresh)
                    if _signature(fresh) != _signature(lease):
                        raise WeightMismatchError(
                            "the sealed manifest changed during download"
                        ) from None
                    current = {item.path: item for item in fresh.files}
                except (httpx.TransportError, _RetryableError) as exc:
                    attempts += 1
                    if attempts >= max_attempts:
                        raise WeightFetchError(f"download of {path} failed: {exc}") from exc
                    logger.warning(f"Retrying {path} after: {exc}")
    finally:
        if owns_client:
            await http.aclose()


async def _download_one(http: httpx.AsyncClient, item: WeightLeaseFile, destination: Path) -> None:
    target = destination / safe_relative_path(item.path)
    if destination.resolve() not in target.resolve().parents:
        raise WeightFetchError(f"weights path escapes the destination: {item.path}")
    target.parent.mkdir(parents=True, exist_ok=True)
    part = target.with_name(target.name + ".part")

    digest = hashlib.sha256()
    offset = 0
    if part.is_file():
        offset = part.stat().st_size
        if offset > item.size:
            part.unlink()
            offset = 0
        else:
            with open(part, "rb") as handle:
                for chunk in iter(lambda: handle.read(_CHUNK), b""):
                    digest.update(chunk)

    if offset < item.size or item.size == 0:
        headers = {"Range": f"bytes={offset}-"} if offset else {}
        async with http.stream("GET", item.url, headers=headers) as response:
            if response.status_code in _EXPIRED_STATUSES:
                raise _UrlExpiredError()
            if response.status_code >= 500 or response.status_code == 429:
                raise _RetryableError(f"HTTP {response.status_code}")
            if response.status_code == 200 and offset:
                # The server ignored Range; start over.
                digest = hashlib.sha256()
                offset = 0
                part.unlink(missing_ok=True)
            elif response.status_code not in (200, 206):
                raise WeightFetchError(f"HTTP {response.status_code} while fetching {item.path}")
            written = offset
            with open(part, "ab" if offset else "wb") as handle:
                async for chunk in response.aiter_bytes(_CHUNK):
                    written += len(chunk)
                    if written > item.size:
                        handle.close()
                        part.unlink(missing_ok=True)
                        raise WeightMismatchError(f"{item.path} is larger than the sealed size")
                    digest.update(chunk)
                    handle.write(chunk)

    size = part.stat().st_size if part.exists() else 0
    if size < item.size:
        # Connection ended early; keep the partial file and retry with Range.
        raise _RetryableError(f"{item.path} ended at {size} of {item.size} bytes")
    if size != item.size or digest.hexdigest() != item.sha256.lower():
        part.unlink(missing_ok=True)
        raise WeightMismatchError(f"{item.path} does not match the sealed sha256")
    part.replace(target)
