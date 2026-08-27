import gzip
import hashlib
import json
from pathlib import Path
from urllib.parse import unquote

import httpx
import pytest

from app.catalog_workflow import (
    CatalogAnalysisError,
    CatalogItem,
    CatalogPublicationError,
    SchematicMetadata,
)
from app.viewer_catalog import ViewerCatalogHttpAdapter

RAW_NBT = b"\x0a\x00\x00\x00"


def _item(filename: str, content: bytes = RAW_NBT) -> CatalogItem:
    return CatalogItem(
        submission_id="submission-42",
        sha256=hashlib.sha256(content).hexdigest(),
        content=content,
        metadata=SchematicMetadata(
            filename=filename,
            title="Brass & Andesite Factory",
            description="A compact starter build",
            tags=("Create", "Starter"),
            license_id="CC0-1.0",
            rights_attested=True,
        ),
    )


def test_contains_sha256_requires_an_exact_catalog_tag() -> None:
    digest = "a" * 64
    requested_queries: list[str] = []

    def upstream(request: httpx.Request) -> httpx.Response:
        requested_queries.append(str(request.url.query))
        return httpx.Response(
            200,
            json={
                "items": [
                    {"title": digest, "tags": ["unrelated"]},
                    {"title": "substring", "tags": [f"prefix-{digest}"]},
                    {"id": "catalog-123", "title": "different case", "tags": [digest.upper()]},
                ],
                "total": 3,
            },
        )

    with ViewerCatalogHttpAdapter(
        "http://schematic-viewer:4173", transport=httpx.MockTransport(upstream)
    ) as catalog:
        assert catalog.contains_sha256(digest) is True
        assert catalog.find_sha256(digest).catalog_id == "catalog-123"

    assert "query=" + digest in requested_queries[0]
    assert "includeTrashed=true" in requested_queries[0]


def test_contains_sha256_rejects_substrings_without_false_positive() -> None:
    digest = "b" * 64

    def upstream(_: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={"items": [{"tags": [f"{digest}0", f"prefix-{digest}"]}]},
        )

    with ViewerCatalogHttpAdapter(
        "http://schematic-viewer:4173", transport=httpx.MockTransport(upstream)
    ) as catalog:
        assert catalog.contains_sha256(digest) is False


def test_count_uses_authoritative_visible_total() -> None:
    def upstream(request: httpx.Request) -> httpx.Response:
        assert "includeTrashed=false" in str(request.url.query)
        return httpx.Response(200, json={"items": [], "total": 7})

    with ViewerCatalogHttpAdapter(
        "http://schematic-viewer:4173", transport=httpx.MockTransport(upstream)
    ) as catalog:
        assert catalog.count() == 7


@pytest.mark.parametrize("filename", ["factory.nbt", "legacy.schematic", "parts.zip"])
def test_analyze_validates_direct_viewer_formats(filename: str) -> None:
    requests: list[str] = []

    def upstream(request: httpx.Request) -> httpx.Response:
        requests.append(request.url.path)
        assert request.content == RAW_NBT
        return httpx.Response(200, json={"size": {"x": 1, "y": 1, "z": 1}})

    with ViewerCatalogHttpAdapter(
        "http://schematic-viewer:4173", transport=httpx.MockTransport(upstream)
    ) as catalog:
        result = catalog.analyze(_item(filename))

    assert result.valid is True
    assert result.failure_code is None
    assert requests == ["/api/schematic"]


def test_analyze_converts_litematic_in_single_mode_then_validates() -> None:
    requests: list[str] = []
    converted = gzip.compress(RAW_NBT, mtime=0)

    def upstream(request: httpx.Request) -> httpx.Response:
        requests.append(request.url.path)
        if request.url.path == "/api/convert/litematic":
            assert request.headers["x-file-name"] == "factory.litematic"
            assert request.headers["x-split-mode"] == "single"
            assert request.headers["x-split-max-kb"] == "0"
            return httpx.Response(200, content=converted, headers={"x-converter-output": "nbt"})
        assert request.url.path == "/api/schematic"
        assert request.content == converted
        return httpx.Response(200, json={"totalBlocks": 1})

    with ViewerCatalogHttpAdapter(
        "http://schematic-viewer:4173", transport=httpx.MockTransport(upstream)
    ) as catalog:
        result = catalog.analyze(_item("factory.litematic", b"litematic source"))

    assert result.valid is True
    assert requests == ["/api/convert/litematic", "/api/schematic"]


def test_analyze_distinguishes_invalid_upload_from_unavailable_viewer() -> None:
    def invalid(_: httpx.Request) -> httpx.Response:
        return httpx.Response(422, json={"error": "secret parser detail"})

    with ViewerCatalogHttpAdapter(
        "http://schematic-viewer:4173", transport=httpx.MockTransport(invalid)
    ) as catalog:
        result = catalog.analyze(_item("invalid.nbt"))
    assert result.valid is False
    assert result.failure_code == "invalid_schematic"

    def unavailable(_: httpx.Request) -> httpx.Response:
        return httpx.Response(503, text="secret internal stack")

    with (
        ViewerCatalogHttpAdapter(
            "http://schematic-viewer:4173", transport=httpx.MockTransport(unavailable)
        ) as catalog,
        pytest.raises(CatalogAnalysisError) as raised,
    ):
        catalog.analyze(_item("factory.nbt"))
    assert str(raised.value) == "schematic analyzer is unavailable"
    assert "secret" not in str(raised.value)


def test_publish_uses_file_credential_and_encoded_safe_provenance(tmp_path: Path) -> None:
    token = "private-viewer-token-that-must-never-be-exposed"
    token_file = tmp_path / "viewer-token"
    token_file.write_text(token + "\n", encoding="utf-8")
    item = _item("folder/unsafe brass § factory.nbt")
    published_requests: list[httpx.Request] = []

    def upstream(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/schematic":
            assert gzip.decompress(request.content) == RAW_NBT
            return httpx.Response(200, json={"totalBlocks": 1})
        published_requests.append(request)
        return httpx.Response(201, json={"id": "catalog-entry-7", "version": 1})

    with ViewerCatalogHttpAdapter(
        "http://schematic-viewer:4173",
        admin_token_file=token_file,
        transport=httpx.MockTransport(upstream),
    ) as catalog:
        entry = catalog.publish(item)

    assert entry.catalog_id == "catalog-entry-7"
    assert entry.sha256 == item.sha256
    request = published_requests[0]
    assert request.url.path == "/api/v1/library/schematics"
    assert request.headers["x-lantern-schematic-admin"] == token
    assert request.headers["x-file-name"] == "unsafe_brass_factory.nbt"
    assert gzip.decompress(request.content) == RAW_NBT
    metadata = json.loads(unquote(request.headers["x-library-metadata"]))
    assert metadata["title"] == "Brass & Andesite Factory"
    assert metadata["license"] == "CC0-1.0"
    assert metadata["sourceFilename"] == "unsafe_brass_factory.nbt"
    assert item.sha256 in metadata["tags"]
    assert token not in request.headers["x-library-metadata"]


def test_publish_converts_before_library_mutation() -> None:
    source = b"litematic input"
    item = _item("factory.schem", source)
    calls: list[str] = []

    def upstream(request: httpx.Request) -> httpx.Response:
        calls.append(request.url.path)
        if request.url.path == "/api/convert/litematic":
            return httpx.Response(
                200,
                content=gzip.compress(RAW_NBT, mtime=0),
                headers={"x-converter-output": "nbt"},
            )
        if request.url.path == "/api/schematic":
            return httpx.Response(200, json={"totalBlocks": 1})
        assert gzip.decompress(request.content) == RAW_NBT
        return httpx.Response(201, json={"id": "converted-entry"})

    with ViewerCatalogHttpAdapter(
        "http://schematic-viewer:4173",
        admin_token="private-token",
        transport=httpx.MockTransport(upstream),
    ) as catalog:
        entry = catalog.publish(item)

    assert entry.catalog_id == "converted-entry"
    assert calls == [
        "/api/convert/litematic",
        "/api/schematic",
        "/api/v1/library/schematics",
    ]


def test_remote_errors_and_oversized_responses_are_sanitized() -> None:
    token = "do-not-leak-this-private-token"

    def oversized(_: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            content=b"{}",
            headers={"content-length": str(3 * 1024 * 1024), "x-secret": token},
        )

    with (
        ViewerCatalogHttpAdapter(
            "http://schematic-viewer:4173",
            admin_token=token,
            transport=httpx.MockTransport(oversized),
        ) as catalog,
        pytest.raises(CatalogAnalysisError) as raised,
    ):
        catalog.contains_sha256("c" * 64)

    assert str(raised.value) == "catalog lookup is unavailable"
    assert token not in str(raised.value)


def test_publish_rejects_content_digest_mismatch_before_network() -> None:
    def should_not_run(_: httpx.Request) -> httpx.Response:
        raise AssertionError("network should not be used")

    item = _item("factory.nbt")
    mismatched = CatalogItem(item.submission_id, "d" * 64, item.content, item.metadata)
    with (
        ViewerCatalogHttpAdapter(
            "http://schematic-viewer:4173",
            admin_token="private-token",
            transport=httpx.MockTransport(should_not_run),
        ) as catalog,
        pytest.raises(CatalogPublicationError, match="does not match"),
    ):
        catalog.publish(mismatched)
