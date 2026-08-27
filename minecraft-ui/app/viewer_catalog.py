"""Synchronous production adapter from the catalog workflow to the viewer API."""

from __future__ import annotations

import gzip
import hashlib
import io
import json
import re
import zipfile
import zlib
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Self
from urllib.parse import quote

import httpx

from .catalog_workflow import (
    CatalogAnalysis,
    CatalogAnalysisError,
    CatalogEntry,
    CatalogItem,
    CatalogPublicationError,
)

_DIGEST = re.compile(r"^[a-f0-9]{64}$")
_CONVERTED_EXTENSIONS = frozenset({".litematic", ".schem"})
_DIRECT_EXTENSIONS = frozenset({".nbt", ".schematic", ".zip"})
_MAX_JSON_BYTES = 2 * 1024 * 1024
_MAX_CANONICAL_BYTES = 250 * 1024 * 1024
_MAX_METADATA_HEADER_BYTES = 12_000


@dataclass(frozen=True, slots=True)
class _ResponseBody:
    status_code: int
    headers: Mapping[str, str]
    content: bytes


class ViewerCatalogHttpAdapter:
    """Publish approved submissions through the private viewer HTTP contract.

    The workflow is synchronous and calls this adapter from a worker thread.  The
    viewer credential is therefore kept server-side and is only attached to the
    single library mutation request.
    """

    def __init__(
        self,
        base_url: str,
        *,
        admin_token: str | None = None,
        admin_token_file: str | Path | None = None,
        transport: httpx.BaseTransport | None = None,
        timeout_seconds: float = 300.0,
    ) -> None:
        if admin_token is not None and admin_token_file is not None:
            raise ValueError("provide the viewer admin token by value or file, not both")
        if not 0 < timeout_seconds <= 600:
            raise ValueError("timeout_seconds must be between 0 and 600")
        self._admin_token = admin_token.strip() if admin_token is not None else None
        self._admin_token_file = Path(admin_token_file) if admin_token_file else None
        short_timeout = min(timeout_seconds, 5.0)
        self._client = httpx.Client(
            base_url=base_url.rstrip("/"),
            transport=transport,
            follow_redirects=False,
            timeout=httpx.Timeout(
                connect=short_timeout,
                read=timeout_seconds,
                write=timeout_seconds,
                pool=short_timeout,
            ),
        )

    def close(self) -> None:
        self._client.close()

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *_: object) -> None:
        self.close()

    def contains_sha256(self, sha256: str) -> bool:
        """Return true only for an exact source digest marker in the catalog."""
        return self.find_sha256(sha256) is not None

    def find_sha256(self, sha256: str) -> CatalogEntry | None:
        """Return the exact published item, enabling post-timeout reconciliation."""
        digest = _normalized_digest(sha256)
        try:
            response = self._request(
                "GET",
                "/api/v1/library/schematics",
                limit=_MAX_JSON_BYTES,
                params={"query": digest, "includeTrashed": "true"},
            )
            if response.status_code != 200:
                raise CatalogAnalysisError("catalog lookup is unavailable")
            payload = _json_object(response.content)
            items = payload.get("items")
            if not isinstance(items, list):
                raise TypeError("items is not a list")
            for item in items:
                if not _item_has_exact_digest(item, digest):
                    continue
                catalog_id = item.get("id") if isinstance(item, dict) else None
                if isinstance(catalog_id, str) and catalog_id.strip():
                    return CatalogEntry(catalog_id=catalog_id, sha256=digest)
            return None
        except CatalogAnalysisError:
            raise
        except (httpx.HTTPError, OSError, UnicodeError, ValueError, TypeError):
            raise CatalogAnalysisError("catalog lookup is unavailable") from None

    def count(self) -> int:
        """Return the viewer's authoritative visible catalog count."""
        try:
            response = self._request(
                "GET",
                "/api/v1/library/schematics",
                limit=_MAX_JSON_BYTES,
                params={"includeTrashed": "false"},
            )
            if response.status_code != 200:
                raise CatalogAnalysisError("catalog count is unavailable")
            payload = _json_object(response.content)
            total = payload.get("total")
            if type(total) is not int or total < 0:
                items = payload.get("items")
                if not isinstance(items, list):
                    raise TypeError("catalog count is invalid")
                total = len(items)
            return total
        except CatalogAnalysisError:
            raise
        except (httpx.HTTPError, OSError, UnicodeError, ValueError, TypeError):
            raise CatalogAnalysisError("catalog count is unavailable") from None

    def analyze(self, item: CatalogItem) -> CatalogAnalysis:
        """Ask the viewer to convert, when necessary, and validate the upload."""
        extension = _extension(item.metadata.filename)
        if extension not in _CONVERTED_EXTENSIONS | _DIRECT_EXTENSIONS:
            return CatalogAnalysis(valid=False, failure_code="unsupported_format")
        try:
            content = item.content
            if extension in _CONVERTED_EXTENSIONS:
                converted = self._convert(item)
                if converted is None:
                    return CatalogAnalysis(valid=False, failure_code="conversion_failed")
                content = converted
            response = self._request(
                "POST",
                "/api/schematic",
                limit=_MAX_JSON_BYTES,
                headers={"content-type": "application/octet-stream"},
                content=content,
            )
            if 400 <= response.status_code < 500:
                return CatalogAnalysis(valid=False, failure_code="invalid_schematic")
            if response.status_code != 200:
                raise CatalogAnalysisError("schematic analyzer is unavailable")
            _json_object(response.content)
            return CatalogAnalysis(valid=True)
        except CatalogAnalysisError:
            raise
        except (httpx.HTTPError, OSError, UnicodeError, ValueError, TypeError):
            raise CatalogAnalysisError("schematic analyzer is unavailable") from None

    def publish(self, item: CatalogItem) -> CatalogEntry:
        """Convert, validate, and add one immutable version to the viewer library."""
        if hashlib.sha256(item.content).hexdigest() != _normalized_digest(item.sha256):
            raise CatalogPublicationError("catalog item content does not match its digest")
        try:
            canonical = self._publishable_nbt(item)
            validation = self._request(
                "POST",
                "/api/schematic",
                limit=_MAX_JSON_BYTES,
                headers={"content-type": "application/octet-stream"},
                content=canonical,
            )
            if validation.status_code != 200:
                raise CatalogPublicationError("catalog rejected the schematic")
            _json_object(validation.content)

            metadata = _encoded_metadata(item)
            response = self._request(
                "POST",
                "/api/v1/library/schematics",
                limit=_MAX_JSON_BYTES,
                headers={
                    "content-type": "application/octet-stream",
                    "x-file-name": _canonical_filename(item.metadata.filename),
                    "x-library-metadata": metadata,
                    "x-lantern-schematic-admin": self._credential(),
                },
                content=canonical,
            )
            if response.status_code != 201:
                raise CatalogPublicationError("catalog publication was refused")
            payload = _json_object(response.content)
            catalog_id = payload.get("id")
            if not isinstance(catalog_id, str) or not catalog_id.strip():
                raise ValueError("missing catalog id")
            return CatalogEntry(catalog_id=catalog_id, sha256=item.sha256)
        except CatalogPublicationError:
            raise
        except (
            CatalogAnalysisError,
            httpx.HTTPError,
            OSError,
            UnicodeError,
            ValueError,
            TypeError,
            zipfile.BadZipFile,
        ):
            raise CatalogPublicationError("catalog publication failed") from None

    def _convert(self, item: CatalogItem) -> bytes | None:
        response = self._request(
            "POST",
            "/api/convert/litematic",
            limit=_MAX_CANONICAL_BYTES,
            headers={
                "content-type": "application/octet-stream",
                "x-file-name": _safe_source_filename(item.metadata.filename),
                "x-split-mode": "single",
                "x-split-max-kb": "0",
            },
            content=item.content,
        )
        if 400 <= response.status_code < 500:
            return None
        if response.status_code != 200:
            raise CatalogAnalysisError("schematic converter is unavailable")
        if response.headers.get("x-converter-output", "nbt").lower() != "nbt":
            return None
        return response.content

    def _publishable_nbt(self, item: CatalogItem) -> bytes:
        extension = _extension(item.metadata.filename)
        if extension in _CONVERTED_EXTENSIONS:
            converted = self._convert(item)
            if converted is None:
                raise CatalogPublicationError("catalog could not convert the schematic")
            return _canonical_gzip(converted)
        if extension == ".zip":
            return _canonical_gzip(_single_nbt_from_zip(item.content))
        if extension in _DIRECT_EXTENSIONS:
            return _canonical_gzip(item.content)
        raise CatalogPublicationError("catalog does not support this schematic format")

    def _credential(self) -> str:
        value = self._admin_token
        if self._admin_token_file is not None:
            value = self._admin_token_file.read_text(encoding="utf-8").strip()
        if not value:
            raise CatalogPublicationError("catalog publication credential is unavailable")
        return value

    def _request(self, method: str, path: str, *, limit: int, **kwargs: object) -> _ResponseBody:
        with self._client.stream(method, path, **kwargs) as response:
            declared = response.headers.get("content-length")
            if declared is not None:
                try:
                    if int(declared) > limit:
                        raise ValueError("viewer response exceeded its limit")
                except ValueError as exc:
                    raise ValueError("viewer returned an invalid response length") from exc
            collected = bytearray()
            for chunk in response.iter_bytes():
                collected.extend(chunk)
                if len(collected) > limit:
                    raise ValueError("viewer response exceeded its limit")
            return _ResponseBody(response.status_code, dict(response.headers), bytes(collected))


def _normalized_digest(value: str) -> str:
    digest = value.strip().lower()
    if not _DIGEST.fullmatch(digest):
        raise ValueError("sha256 must be 64 lowercase hexadecimal characters")
    return digest


def _item_has_exact_digest(item: object, digest: str) -> bool:
    if not isinstance(item, dict):
        return False
    tags = item.get("tags")
    return isinstance(tags, list) and any(
        isinstance(tag, str) and tag.strip().lower() == digest for tag in tags
    )


def _json_object(content: bytes) -> dict[str, object]:
    value = json.loads(content)
    if not isinstance(value, dict):
        raise TypeError("viewer response is not an object")
    return value


def _extension(filename: str) -> str:
    return PurePosixPath(filename.replace("\\", "/")).suffix.lower()


def _safe_source_filename(filename: str) -> str:
    source = PurePosixPath(filename.replace("\\", "/")).name
    safe = re.sub(r"[^A-Za-z0-9._-]+", "_", source).strip("_")[:255]
    return safe or "schematic.nbt"


def _canonical_filename(filename: str) -> str:
    source = _safe_source_filename(filename)
    stem = re.sub(r"\.(?:litematic|schem|schematic|nbt|zip)$", "", source, flags=re.IGNORECASE)
    stem = stem.rstrip(".")[:90] or "schematic"
    return f"{stem}.nbt"


def _encoded_metadata(item: CatalogItem) -> str:
    digest = _normalized_digest(item.sha256)
    tags = []
    for tag in item.metadata.tags:
        clean = " ".join(str(tag).strip().lower().split())[:64]
        if clean and clean != digest and clean not in tags:
            tags.append(clean)
    tags = tags[:31]
    tags.append(digest)
    description = " ".join(item.metadata.description.strip().split())[:4000]
    payload = {
        "title": " ".join(item.metadata.title.strip().split())[:160] or "Untitled schematic",
        "description": description,
        "tags": tags,
        "license": " ".join(item.metadata.license_id.strip().split())[:200],
        "sourceFilename": _safe_source_filename(item.metadata.filename),
    }
    encoded = _quote_json(payload)
    while len(encoded.encode("ascii")) > _MAX_METADATA_HEADER_BYTES and payload["description"]:
        payload["description"] = payload["description"][: len(payload["description"]) // 2]
        encoded = _quote_json(payload)
    while len(encoded.encode("ascii")) > _MAX_METADATA_HEADER_BYTES and len(payload["tags"]) > 1:
        payload["tags"].pop(-2)
        encoded = _quote_json(payload)
    if len(encoded.encode("ascii")) > _MAX_METADATA_HEADER_BYTES:
        raise CatalogPublicationError("catalog metadata is too large")
    return encoded


def _quote_json(payload: object) -> str:
    compact = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
    return quote(compact, safe="")


def _single_nbt_from_zip(content: bytes) -> bytes:
    with zipfile.ZipFile(io.BytesIO(content)) as archive:
        entries = [
            entry
            for entry in archive.infolist()
            if not entry.is_dir() and entry.filename.lower().endswith(".nbt")
        ]
        if len(entries) != 1:
            raise CatalogPublicationError("catalog publication requires one NBT file")
        entry = entries[0]
        if entry.flag_bits & 0x1 or entry.file_size > _MAX_CANONICAL_BYTES:
            raise CatalogPublicationError("catalog archive is not publishable")
        with archive.open(entry) as source:
            value = source.read(_MAX_CANONICAL_BYTES + 1)
        if len(value) > _MAX_CANONICAL_BYTES:
            raise CatalogPublicationError("catalog archive is too large")
        return value


def _canonical_gzip(content: bytes) -> bytes:
    if content.startswith(b"\x1f\x8b"):
        raw = _bounded_decompress(content, 16 + zlib.MAX_WBITS)
    elif len(content) >= 2 and content[0] == 0x78:
        try:
            raw = _bounded_decompress(content, zlib.MAX_WBITS)
        except ValueError:
            raw = content
    else:
        raw = content
    if not raw or raw[0] != 0x0A:
        raise CatalogPublicationError("converted schematic is not NBT")
    compressed = gzip.compress(raw, compresslevel=9, mtime=0)
    if len(compressed) > _MAX_CANONICAL_BYTES:
        raise CatalogPublicationError("converted schematic is too large")
    return compressed


def _bounded_decompress(content: bytes, window_bits: int) -> bytes:
    decompressor = zlib.decompressobj(window_bits)
    raw = decompressor.decompress(content, _MAX_CANONICAL_BYTES + 1)
    if len(raw) > _MAX_CANONICAL_BYTES or decompressor.unconsumed_tail:
        raise ValueError("expanded schematic exceeded its limit")
    remaining = _MAX_CANONICAL_BYTES + 1 - len(raw)
    raw += decompressor.flush(remaining)
    if len(raw) > _MAX_CANONICAL_BYTES or not decompressor.eof:
        raise ValueError("expanded schematic is invalid")
    return raw
