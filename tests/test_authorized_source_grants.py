from __future__ import annotations

import json
from datetime import date

import pytest

from src.job_collection.authorization import (
    AuthorizationBlocked,
    load_authorized_source_grants,
)


def _write_grants(tmp_path, source: dict[str, object]):
    path = tmp_path / "authorized_job_sources.local.json"
    path.write_text(
        json.dumps({"sources": {"boss_zhipin_authorized": source}}, ensure_ascii=False),
        encoding="utf-8",
    )
    return path


def _valid_grant(**overrides: object) -> dict[str, object]:
    grant: dict[str, object] = {
        "authorization_reference": "AUTH-BOSS-2026-001",
        "valid_until": "2026-12-31",
        "access_methods": ["file_export"],
        "scope": "全国公开招聘岗位批量导出，仅限比赛研究。",
        "credential_env_vars": [],
    }
    grant.update(overrides)
    return grant


def test_grant_requires_reference_scope_method_and_future_expiry(tmp_path):
    path = _write_grants(tmp_path, _valid_grant())

    grants = load_authorized_source_grants(path, today=date(2026, 8, 12))
    grant = grants.require("boss_zhipin_authorized", "file_export")

    assert grant.scope == "全国公开招聘岗位批量导出，仅限比赛研究。"
    assert grant.authorization_reference == "AUTH-BOSS-2026-001"


@pytest.mark.parametrize("bad_key", ["token", "password", "cookie", "secret", "api_key"])
def test_grant_manifest_rejects_embedded_credentials(tmp_path, bad_key):
    path = _write_grants(tmp_path, _valid_grant(**{bad_key: "sensitive"}))

    with pytest.raises(AuthorizationBlocked, match="credential|field|secret"):
        load_authorized_source_grants(path, today=date(2026, 8, 12))


def test_expired_or_missing_source_is_blocked(tmp_path):
    path = _write_grants(tmp_path, _valid_grant(valid_until="2025-12-31"))
    grants = load_authorized_source_grants(path, today=date(2026, 8, 12))

    with pytest.raises(AuthorizationBlocked, match="expired"):
        grants.require("boss_zhipin_authorized", "file_export")
    with pytest.raises(AuthorizationBlocked, match="not granted"):
        grants.require("job51_authorized", "file_export")


def test_grant_rejects_invalid_credential_environment_variable(tmp_path):
    path = _write_grants(
        tmp_path,
        _valid_grant(
            access_methods=["api"],
            credential_env_vars=["boss-token"],
        ),
    )

    with pytest.raises(AuthorizationBlocked, match="environment variable"):
        load_authorized_source_grants(path, today=date(2026, 8, 12))


def test_grant_manifest_rejects_symbolic_links(tmp_path):
    target = _write_grants(tmp_path, _valid_grant())
    link = tmp_path / "linked-grants.json"
    try:
        link.symlink_to(target)
    except OSError:
        pytest.skip("symbolic links are unavailable in this environment")

    with pytest.raises(AuthorizationBlocked, match="symbolic link"):
        load_authorized_source_grants(link, today=date(2026, 8, 12))
