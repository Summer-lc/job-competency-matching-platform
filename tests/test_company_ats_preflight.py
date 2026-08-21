from __future__ import annotations

import hashlib
import json

import httpx
import pytest

from src.preflight_company_ats import load_candidates, preflight_candidates


def candidate(**overrides):
    value = {
        "source_id": "company_feishu_example",
        "organization_name": "示例科技",
        "ats_type": "feishu",
        "portal_host": "example.jobs.feishu.cn",
        "reviewed_homepage_url": "https://example.jobs.feishu.cn/",
        "status": "candidate",
    }
    value.update(overrides)
    return value


def feishu_response(records=None):
    if records is None:
        records = [
            {
                "id": "job_1",
                "title": "Python开发工程师",
                "description": "负责服务端系统设计、开发、测试与稳定性建设。",
                "requirement": "熟悉Python、数据库、容器和自动化测试。",
                "city_list": [{"name": "北京"}],
                "publish_time": 1775212138373,
            }
        ]
    return {"code": 0, "data": {"count": len(records), "job_post_list": records}}


def test_load_candidates_rejects_unreviewed_host(tmp_path):
    path = tmp_path / "candidates.json"
    path.write_text(
        json.dumps({"schema_version": 1, "candidates": [candidate(portal_host="evil.test")]}),
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="approved ATS domain"):
        load_candidates(path)


@pytest.mark.asyncio
async def test_feishu_preflight_discovers_path_and_never_replays_cookie():
    requests = []
    api_body = json.dumps(feishu_response(), ensure_ascii=False).encode("utf-8")

    def handler(request: httpx.Request):
        requests.append(request)
        assert request.headers.get("cookie") is None
        if request.method == "GET":
            html = (
                '<script id="js-websiteInfo" type="application/json">'
                '{"website_info":{"path":"campusrecruitment"}}</script>'
            )
            return httpx.Response(
                200,
                text=html,
                headers={"content-type": "text/html", "Set-Cookie": "session=secret"},
            )
        return httpx.Response(
            200,
            content=api_body,
            headers={"content-type": "application/json"},
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        report = await preflight_candidates(
            [candidate()], client=client, checked_at="2026-08-14T08:00:00+08:00"
        )

    result = report["results"][0]
    assert result["accepted"] is True
    assert result["portal_path"] == "campusrecruitment"
    assert result["sample_count"] == 1
    assert result["field_presence"] == {
        "title": True,
        "record_id": True,
        "description": True,
        "company": True,
        "region_field": True,
        "traceable_url": True,
    }
    assert result["response_sha256"] == hashlib.sha256(api_body).hexdigest()
    assert len(requests) == 2
    assert requests[1].method == "POST"
    assert json.loads(requests[1].content)["limit"] == 1


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("status_code", "body", "reason"),
    [
        (401, b"unauthorized", "HTTP 401"),
        (403, b"captcha", "HTTP 403"),
        (429, b"rate limited", "HTTP 429"),
        (200, b'{"code":0,"data":{"count":0,"job_post_list":[]}}', "empty"),
        (200, b'{"encrypted":true,"data":"ciphertext"}', "malformed"),
    ],
)
async def test_beisen_or_feishu_bad_api_responses_are_rejected(
    status_code, body, reason
):
    def handler(request: httpx.Request):
        if request.method == "GET":
            return httpx.Response(
                200,
                text='<script id="js-websiteInfo">{"path":"index"}</script>',
            )
        return httpx.Response(
            status_code,
            content=body,
            headers={"content-type": "application/json"},
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        report = await preflight_candidates([candidate()], client=client)

    result = report["results"][0]
    assert result["accepted"] is False
    assert reason.casefold() in result["stop_reason"].casefold()


@pytest.mark.asyncio
async def test_preflight_does_not_write_registry_or_database(tmp_path):
    registry = tmp_path / "job_sources.json"
    database = tmp_path / "job_competency.db"
    registry.write_bytes(b"registry-sentinel")
    database.write_bytes(b"database-sentinel")

    def handler(request: httpx.Request):
        if request.method == "GET":
            return httpx.Response(
                200,
                text='<script id="js-websiteInfo">{"path":"index"}</script>',
            )
        return httpx.Response(200, json=feishu_response())

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        await preflight_candidates([candidate()], client=client)

    assert registry.read_bytes() == b"registry-sentinel"
    assert database.read_bytes() == b"database-sentinel"
