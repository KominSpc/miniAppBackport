"""服务端导出的 OpenAPI 必须与 Flutter 仓库的冻结契约兼容。"""

from __future__ import annotations

import pytest

from contract_diff import DEFAULT_CONTRACT, compare, load_json, server_schema


@pytest.mark.skipif(not DEFAULT_CONTRACT.exists(), reason="找不到 Flutter 仓库的契约快照")
def test_server_openapi_matches_frozen_contract() -> None:
    report = compare(load_json(DEFAULT_CONTRACT), server_schema())
    assert report.errors == [], "\n".join(report.errors)


def test_every_reference_resolves() -> None:
    from contract_diff import collect_refs, resolve_pointer

    schema = server_schema()
    unresolved = sorted(ref for ref in collect_refs(schema) if not resolve_pointer(schema, ref))
    assert unresolved == []


def test_error_codes_match_contract_enum() -> None:
    from app.core.errors import ERROR_CODES

    schema = server_schema()
    declared = schema["components"]["schemas"]["ErrorCode"]["enum"]
    assert sorted(declared) == sorted(ERROR_CODES)
