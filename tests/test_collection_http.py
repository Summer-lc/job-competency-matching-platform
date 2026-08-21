from __future__ import annotations

import asyncio
import hashlib
import json
from datetime import datetime, timedelta, timezone

import httpx
import pytest
from pydantic import ValidationError

from src.job_collection.adapters.base import RequestSpec
from src.job_collection.http_client import (
    BoundedHttpClient,
    SourceStopped,
    _HtmlAccessDetector,
)
from src.job_collection.models import SourceDefinition
from src.job_collection.source_registry import CollectionBlocked, SourceRegistry, URLScopeError
from src.job_collection.storage import RunStorage, SnapshotCorrupt, StorageError


NOW = datetime(2026, 8, 5, 12, 0, tzinfo=timezone.utc)


async def no_sleep(_seconds: float) -> None:
    return None


class VirtualClock:
    def __init__(self) -> None:
        self.now = 0.0
        self.sleep_events: list[tuple[float, float]] = []

    def __call__(self) -> float:
        return self.now

    async def sleep(self, seconds: float) -> None:
        self.sleep_events.append((self.now, seconds))
        self.now += seconds
        await asyncio.sleep(0)


def make_source(**overrides) -> SourceDefinition:
    payload = {
        "source_id": "example_jobs",
        "source_name": "Example Jobs",
        "source_type": "public_service",
        "market_scope": "china",
        "base_url": "https://example.com",
        "allowed_paths": ["/jobs/"],
        "collection_mode": "public_html",
        "compliance_status": "approved",
        "compliance_note": "Reviewed public job pages without login.",
        "rate_limit_seconds": 3.0,
        "max_pages": 5,
        "max_records": 100,
        "parser_name": "example",
        "parser_version": "v1",
        "enabled": True,
    }
    payload.update(overrides)
    return SourceDefinition.model_validate(payload)


def make_storage(tmp_path, run_id: str = "run-1") -> RunStorage:
    return RunStorage(tmp_path / "data" / "collections", run_id, clock=lambda: NOW)


def test_request_spec_accepts_public_json_post_request():
    request = RequestSpec(
        method="POST",
        url="https://company.jobs.feishu.cn/api/v1/search/job/posts",
        headers={"Portal-Channel": "office", "website-path": "index"},
        json_body={"keyword": "Python", "limit": 20, "offset": 0},
    )

    assert request.method == "POST"
    assert request.headers == {
        "Portal-Channel": "office",
        "website-path": "index",
    }
    assert request.json_body == {"keyword": "Python", "limit": 20, "offset": 0}


@pytest.mark.parametrize(
    "header",
    [
        "Authorization",
        "Proxy-Authorization",
        "Cookie",
        "Set-Cookie",
        "X-Api-Key",
        "X-Auth-Token",
        "Host",
    ],
)
def test_request_spec_rejects_sensitive_request_headers(header):
    with pytest.raises(ValidationError, match="header"):
        RequestSpec(
            method="POST",
            url="https://company.jobs.feishu.cn/api/v1/search/job/posts",
            headers={header: "secret"},
            json_body={},
        )


def test_request_spec_rejects_json_body_for_get_request():
    with pytest.raises(ValidationError, match="GET"):
        RequestSpec(
            method="GET",
            url="https://example.com/jobs/",
            json_body={"query": "Python"},
        )


def make_client(
    source: SourceDefinition,
    storage: RunStorage,
    handler,
    *,
    sleep=None,
    clock=None,
    max_attempts: int = 3,
    backoff_seconds: float = 1.0,
    max_redirects: int = 3,
    max_response_bytes: int = 5 * 1024 * 1024,
    request_budget=None,
):
    transport = httpx.MockTransport(handler)
    http_client = httpx.AsyncClient(transport=transport)
    registry = SourceRegistry([source])
    bounded = BoundedHttpClient(
        source=source,
        registry=registry,
        storage=storage,
        client=http_client,
        sleep=sleep or no_sleep,
        clock=clock,
        max_attempts=max_attempts,
        backoff_seconds=backoff_seconds,
        max_redirects=max_redirects,
        timeout_seconds=7.5,
        max_response_bytes=max_response_bytes,
        request_budget=request_budget,
    )
    return bounded, http_client


@pytest.mark.asyncio
async def test_post_json_body_is_sent_and_part_of_cache_identity(tmp_path):
    source = make_source(
        base_url="https://company.jobs.feishu.cn",
        allowed_paths=["/api/v1/search/job/posts"],
        collection_mode="public_json",
    )
    storage = make_storage(tmp_path)
    requests = []

    def handler(request: httpx.Request):
        requests.append(request)
        return httpx.Response(
            200,
            json={"code": 0, "data": {"job_post_list": []}},
            headers={"content-type": "application/json"},
        )

    bounded, http_client = make_client(source, storage, handler)
    url = "https://company.jobs.feishu.cn/api/v1/search/job/posts"
    try:
        first = await bounded.fetch(
            url,
            method="POST",
            headers={"Portal-Channel": "office", "website-path": "index"},
            json_body={"keyword": "Python", "limit": 20, "offset": 0},
        )
        second = await bounded.fetch(
            url,
            method="POST",
            headers={"Portal-Channel": "office", "website-path": "index"},
            json_body={"keyword": "Java", "limit": 20, "offset": 0},
        )
        cached = await bounded.fetch(
            url,
            method="POST",
            headers={"Portal-Channel": "office", "website-path": "index"},
            json_body={"offset": 0, "limit": 20, "keyword": "Python"},
        )
    finally:
        await http_client.aclose()

    assert len(requests) == 2
    assert requests[0].method == "POST"
    assert json.loads(requests[0].content) == {
        "keyword": "Python",
        "limit": 20,
        "offset": 0,
    }
    assert requests[0].headers["portal-channel"] == "office"
    assert first.url != second.url
    assert cached.url == first.url
    assert cached.from_cache is True


@pytest.mark.asyncio
async def test_response_cookies_are_never_replayed(tmp_path):
    source = make_source(rate_limit_seconds=0.01)
    seen_cookie_headers: list[str | None] = []

    def handler(request: httpx.Request):
        seen_cookie_headers.append(request.headers.get("cookie"))
        if len(seen_cookie_headers) == 1:
            return httpx.Response(200, headers={"Set-Cookie": "session=secret"})
        return httpx.Response(200)

    bounded, http_client = make_client(source, make_storage(tmp_path), handler)
    try:
        await bounded.fetch("/jobs/1", resume=False)
        await bounded.fetch("/jobs/2", resume=False)
    finally:
        await http_client.aclose()

    assert seen_cookie_headers == [None, None]


def test_html_detector_tracks_fragmented_body_sample_in_linear_counter():
    detector = _HtmlAccessDetector()

    assert hasattr(detector, "body_char_count")
    detector.feed("<body>" + "".join("<span>x</span>" for _ in range(20_000)) + "</body>")

    assert detector.body_char_count == 12_000
    assert len("".join(detector.body_parts)) == 12_000


@pytest.mark.asyncio
async def test_success_writes_snapshot_metadata_and_sha256(tmp_path):
    source = make_source()
    storage = make_storage(tmp_path)
    body = "<html><title>Python Engineer</title><body>Build services.</body></html>".encode()
    requests = []

    def handler(request: httpx.Request):
        requests.append(request)
        return httpx.Response(200, headers={"content-type": "text/html; charset=utf-8"}, content=body)

    bounded, http_client = make_client(source, storage, handler)
    try:
        result = await bounded.fetch("https://example.com/jobs/42")
    finally:
        await http_client.aclose()

    expected_hash = hashlib.sha256(body).hexdigest()
    assert result.ok is True
    assert result.content == body
    assert result.content_hash == expected_hash
    assert result.from_cache is False
    assert len(requests) == 1
    assert "job research" in requests[0].headers["user-agent"].lower()
    assert "competition" not in requests[0].headers["user-agent"].lower()

    raw_path, metadata_path = storage.snapshot_paths(source.source_id, result.url)
    assert raw_path.read_bytes() == body
    metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
    assert metadata == {
        "source_id": source.source_id,
        "run_id": "run-1",
        "url": "https://example.com/jobs/42",
        "final_url": "https://example.com/jobs/42",
        "status": 200,
        "content_type": "text/html; charset=utf-8",
        "fetched_at": "2026-08-05T12:00:00+00:00",
        "content_hash": expected_hash,
        "parser_version": "v1",
        "from_cache": False,
    }


@pytest.mark.asyncio
async def test_resume_uses_valid_cache_without_network(tmp_path):
    source = make_source()
    storage = make_storage(tmp_path)
    calls = 0

    def handler(request: httpx.Request):
        nonlocal calls
        calls += 1
        return httpx.Response(200, content=b"cached-body", headers={"content-type": "text/html"})

    first, http_client = make_client(source, storage, handler)
    try:
        await first.fetch("/jobs/1")
        second = await first.fetch("/jobs/1", resume=True)
    finally:
        await http_client.aclose()

    assert calls == 1
    assert second.from_cache is True
    assert second.content == b"cached-body"


@pytest.mark.asyncio
async def test_corrupt_cache_hash_is_not_trusted_and_is_refetched(tmp_path):
    source = make_source()
    storage = make_storage(tmp_path)
    url = "https://example.com/jobs/1"
    storage.write_success(
        source=source,
        url=url,
        final_url=url,
        status=200,
        content_type="text/html",
        content=b"original",
    )
    raw_path, _ = storage.snapshot_paths(source.source_id, url)
    raw_path.write_bytes(b"tampered")
    calls = 0

    def handler(request: httpx.Request):
        nonlocal calls
        calls += 1
        return httpx.Response(200, content=b"fresh", headers={"content-type": "text/html"})

    bounded, http_client = make_client(source, storage, handler)
    try:
        result = await bounded.fetch(url, resume=True)
    finally:
        await http_client.aclose()

    assert calls == 1
    assert result.content == b"fresh"
    assert result.from_cache is False
    assert raw_path.read_bytes() == b"fresh"


@pytest.mark.asyncio
@pytest.mark.parametrize("failure_kind", [500, 502, 503, 504, "transport"])
async def test_retryable_failures_stop_at_configured_attempts(tmp_path, failure_kind):
    source = make_source(rate_limit_seconds=0.01)
    calls = 0

    def handler(request: httpx.Request):
        nonlocal calls
        calls += 1
        if failure_kind == "transport":
            raise httpx.ConnectError("offline", request=request)
        return httpx.Response(failure_kind, content=b"temporarily unavailable")

    sleeps = []

    async def fake_sleep(seconds):
        sleeps.append(seconds)

    bounded, http_client = make_client(
        source,
        make_storage(tmp_path),
        handler,
        sleep=fake_sleep,
        clock=lambda: 10.0,
        max_attempts=3,
    )
    try:
        if failure_kind == "transport":
            with pytest.raises(httpx.ConnectError):
                await bounded.fetch("/jobs/1")
        else:
            result = await bounded.fetch("/jobs/1")
            assert result.status_code == failure_kind
            assert result.ok is False
    finally:
        await http_client.aclose()

    assert calls == 3
    assert sleeps.count(1.0) == 1
    assert sleeps.count(2.0) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("status", [401, 403, 429])
async def test_auth_and_throttle_statuses_stop_source_without_retry(
    tmp_path, monkeypatch, status
):
    source = make_source()
    calls = 0

    def handler(request: httpx.Request):
        nonlocal calls
        calls += 1
        return httpx.Response(status, content=b"blocked")

    bounded, http_client = make_client(source, make_storage(tmp_path), handler)
    try:
        with pytest.raises(SourceStopped, match=str(status)) as first_stop:
            await bounded.fetch("/jobs/1")
        assert bounded.stopped is True
        assert bounded.stop_reason == str(first_stop.value)

        def cache_must_not_be_read(*args, **kwargs):
            raise AssertionError("stopped source must fail before cache access")

        monkeypatch.setattr(bounded.storage, "load_snapshot", cache_must_not_be_read)
        with pytest.raises(SourceStopped) as repeated_stop:
            await bounded.fetch("/jobs/1")
        assert str(repeated_stop.value) == bounded.stop_reason
    finally:
        await http_client.aclose()
    assert calls == 1


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "html",
    [
        "<html><title>用户登录</title><body>账号</body></html>",
        "<html><title>访问验证</title><body>请输入验证码</body></html>",
        '<html><body><h1>用户登录</h1><form action="/login"><input type="password"></form></body></html>',
        '<html><body><h1>访问验证</h1><label>请输入验证码</label><input name="captcha"></body></html>',
    ],
)
async def test_login_and_captcha_pages_stop_source(tmp_path, html):
    source = make_source()
    calls = 0

    def handler(request: httpx.Request):
        nonlocal calls
        calls += 1
        return httpx.Response(200, content=html.encode("utf-8"), headers={"content-type": "text/html; charset=utf-8"})

    storage = make_storage(tmp_path)
    bounded, http_client = make_client(source, storage, handler)
    try:
        with pytest.raises(SourceStopped, match="login or verification"):
            await bounded.fetch("/jobs/1")
        assert bounded.stopped is True
        with pytest.raises(SourceStopped):
            await bounded.fetch("/jobs/2")
    finally:
        await http_client.aclose()

    assert calls == 1
    assert storage.load_snapshot(source.source_id, "https://example.com/jobs/1") is None


@pytest.mark.asyncio
async def test_normal_job_description_with_login_word_is_not_blocked(tmp_path):
    source = make_source()
    html = "<html><title>平台工程师</title><body>要求具备登录系统经验，负责认证服务。</body></html>"

    def handler(request: httpx.Request):
        return httpx.Response(200, content=html.encode("utf-8"), headers={"content-type": "text/html"})

    bounded, http_client = make_client(source, make_storage(tmp_path), handler)
    try:
        result = await bounded.fetch("/jobs/1")
    finally:
        await http_client.aclose()
    assert result.ok is True


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "html",
    [
        "<html><title>验证码算法工程师</title><body>负责验证码识别算法研发与模型优化。</body></html>",
        '<html><title>前端工程师</title><body>负责业务 captcha 验证码组件开发。<input name="captcha"></body></html>',
    ],
)
async def test_captcha_word_or_input_without_access_gate_does_not_stop_source(
    tmp_path, html
):
    source = make_source()

    def handler(request: httpx.Request):
        return httpx.Response(
            200,
            content=html.encode("utf-8"),
            headers={"content-type": "text/html; charset=utf-8"},
        )

    bounded, http_client = make_client(source, make_storage(tmp_path), handler)
    try:
        result = await bounded.fetch("/jobs/captcha-related")
    finally:
        await http_client.aclose()

    assert result.ok is True
    assert bounded.stopped is False


@pytest.mark.asyncio
async def test_long_job_description_with_global_login_components_is_not_blocked(tmp_path):
    source = make_source()
    job_text = "负责平台研发、性能优化和服务治理。" * 600
    html = (
        "<html><title>验证码算法工程师</title><body>"
        f"<article>{job_text}</article>"
        '<footer><form action="/login"><input type="password">'
        '<div class="captcha-widget">验证码</div></form></footer>'
        "</body></html>"
    )

    def handler(request: httpx.Request):
        return httpx.Response(
            200,
            content=html.encode("utf-8"),
            headers={"content-type": "text/html; charset=utf-8"},
        )

    bounded, http_client = make_client(source, make_storage(tmp_path), handler)
    try:
        result = await bounded.fetch("/jobs/long")
    finally:
        await http_client.aclose()

    assert result.ok is True
    assert bounded.stopped is False
    assert bounded.stop_reason is None


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "user_agent",
    [
        "",
        "Mozilla/5.0",
        "JobResearch/1.0",
        "ResearchBot (contact=)",
        "ResearchBot (contact=team@example.org; competition crawler)",
    ],
)
async def test_custom_user_agent_requires_research_identity_and_rejects_generic_or_competition(
    tmp_path, user_agent
):
    source = make_source()
    http_client = httpx.AsyncClient(transport=httpx.MockTransport(lambda request: httpx.Response(200)))
    try:
        with pytest.raises(ValueError, match="user_agent"):
            BoundedHttpClient(
                source=source,
                registry=SourceRegistry([source]),
                storage=make_storage(tmp_path),
                client=http_client,
                user_agent=user_agent,
            )
    finally:
        await http_client.aclose()


@pytest.mark.asyncio
async def test_custom_user_agent_accepts_research_purpose_with_project_or_contact(tmp_path):
    source = make_source()
    seen_user_agents = []

    def handler(request: httpx.Request):
        seen_user_agents.append(request.headers["user-agent"])
        return httpx.Response(200, content=b"ok")

    custom_user_agent = (
        "JobResearch/2.0 (project=job-competency; contact=team@example.org)"
    )
    bounded, http_client = make_client(source, make_storage(tmp_path), handler)
    bounded = BoundedHttpClient(
        source=source,
        registry=SourceRegistry([source]),
        storage=make_storage(tmp_path),
        client=http_client,
        sleep=no_sleep,
        user_agent=custom_user_agent,
    )
    try:
        await bounded.fetch("/jobs/ua")
    finally:
        await http_client.aclose()

    assert seen_user_agents == [custom_user_agent]


@pytest.mark.asyncio
async def test_content_length_over_limit_stops_before_snapshot_write(tmp_path):
    source = make_source()
    storage = make_storage(tmp_path)

    def handler(request: httpx.Request):
        return httpx.Response(
            200,
            headers={"content-length": "1000", "content-type": "application/json"},
            content=b"{}",
        )

    bounded, http_client = make_client(
        source, storage, handler, max_response_bytes=100
    )
    try:
        with pytest.raises(SourceStopped, match="response body"):
            await bounded.fetch("/jobs/oversized")
    finally:
        await http_client.aclose()

    assert storage.load_snapshot(
        source.source_id, "https://example.com/jobs/oversized"
    ) is None


@pytest.mark.asyncio
async def test_decompressed_stream_bytes_are_bounded(tmp_path):
    import gzip

    source = make_source()
    compressed = gzip.compress(b"x" * 5000)

    def handler(request: httpx.Request):
        return httpx.Response(
            200,
            headers={
                "content-encoding": "gzip",
                "content-length": str(len(compressed)),
                "content-type": "application/octet-stream",
            },
            content=compressed,
        )

    bounded, http_client = make_client(
        source, make_storage(tmp_path), handler, max_response_bytes=1000
    )
    try:
        with pytest.raises(SourceStopped, match="response body"):
            await bounded.fetch("/jobs/compressed")
    finally:
        await http_client.aclose()


@pytest.mark.asyncio
async def test_successful_compressed_response_is_not_decoded_twice(tmp_path):
    import gzip

    source = make_source()
    body = b'{"data":{"list":[]}}'
    compressed = gzip.compress(body)

    def handler(request: httpx.Request):
        return httpx.Response(
            200,
            headers={
                "content-encoding": "gzip",
                "content-length": str(len(compressed)),
                "content-type": "application/json",
            },
            content=compressed,
        )

    bounded, http_client = make_client(source, make_storage(tmp_path), handler)
    try:
        result = await bounded.fetch("/jobs/compressed")
    finally:
        await http_client.aclose()

    assert result.content == body


@pytest.mark.asyncio
async def test_shared_request_budget_counts_retries_and_redirects(tmp_path):
    from src.job_collection.http_client import RequestBudget

    source = make_source(rate_limit_seconds=0.01)
    calls = 0

    def handler(request: httpx.Request):
        nonlocal calls
        calls += 1
        if calls == 1:
            return httpx.Response(500, content=b"retry")
        return httpx.Response(302, headers={"location": "/jobs/final"})

    bounded, http_client = make_client(
        source,
        make_storage(tmp_path),
        handler,
        request_budget=RequestBudget(2),
        backoff_seconds=0,
    )
    try:
        with pytest.raises(SourceStopped, match="request budget"):
            await bounded.fetch("/jobs/start")
    finally:
        await http_client.aclose()

    assert calls == 2


@pytest.mark.asyncio
async def test_request_budget_is_shared_across_clients(tmp_path):
    from src.job_collection.http_client import RequestBudget

    source = make_source(rate_limit_seconds=0.01)
    calls = 0

    def handler(request: httpx.Request):
        nonlocal calls
        calls += 1
        return httpx.Response(200, content=b"ok")

    budget = RequestBudget(1)
    first, first_http = make_client(
        source, make_storage(tmp_path, "budget-one"), handler, request_budget=budget
    )
    second, second_http = make_client(
        source, make_storage(tmp_path, "budget-two"), handler, request_budget=budget
    )
    try:
        await first.fetch("/jobs/one")
        with pytest.raises(SourceStopped, match="request budget"):
            await second.fetch("/jobs/two")
    finally:
        await first_http.aclose()
        await second_http.aclose()

    assert calls == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("block_kind", ["status", "login"])
async def test_queued_concurrent_fetch_is_not_sent_after_source_stops(
    tmp_path, block_kind
):
    source = make_source(rate_limit_seconds=0.01)
    first_request_started = asyncio.Event()
    release_first_response = asyncio.Event()
    calls = 0

    async def handler(request: httpx.Request):
        nonlocal calls
        calls += 1
        if calls > 1:
            return httpx.Response(200, content=b"must not be sent")
        first_request_started.set()
        await release_first_response.wait()
        if block_kind == "status":
            return httpx.Response(403, content=b"forbidden")
        return httpx.Response(
            200,
            content="<html><title>用户登录</title><body>请登录</body></html>".encode(
                "utf-8"
            ),
            headers={"content-type": "text/html; charset=utf-8"},
        )

    http_client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    bounded = BoundedHttpClient(
        source=source,
        registry=SourceRegistry([source]),
        storage=make_storage(tmp_path),
        client=http_client,
        sleep=no_sleep,
        clock=lambda: 1.0,
    )
    first = asyncio.create_task(bounded.fetch("/jobs/1", resume=False))
    await first_request_started.wait()
    second = asyncio.create_task(bounded.fetch("/jobs/2", resume=False))
    await asyncio.sleep(0)
    release_first_response.set()
    try:
        results = await asyncio.gather(first, second, return_exceptions=True)
    finally:
        await http_client.aclose()

    assert all(isinstance(result, SourceStopped) for result in results)
    assert calls == 1
    assert bounded.stopped is True


@pytest.mark.asyncio
async def test_redirects_are_followed_one_hop_at_a_time_with_scope_validation(tmp_path):
    source = make_source()
    seen = []

    def handler(request: httpx.Request):
        seen.append(str(request.url))
        if request.url.path == "/jobs/start":
            return httpx.Response(302, headers={"location": "next"})
        return httpx.Response(200, content=b"done", headers={"content-type": "text/plain"})

    bounded, http_client = make_client(source, make_storage(tmp_path), handler)
    try:
        result = await bounded.fetch("/jobs/start")
    finally:
        await http_client.aclose()
    assert seen == ["https://example.com/jobs/start", "https://example.com/jobs/next"]
    assert result.final_url == "https://example.com/jobs/next"


@pytest.mark.asyncio
async def test_redirect_callback_observes_canonical_targets_cannot_replace_them_and_cache_keeps_final_url(
    tmp_path,
):
    source = make_source()
    storage = make_storage(tmp_path)
    seen_requests = []
    validated_hops = []

    def handler(request: httpx.Request):
        seen_requests.append(str(request.url))
        if request.url.path == "/jobs/start":
            return httpx.Response(302, headers={"location": "middle"})
        if request.url.path == "/jobs/middle":
            return httpx.Response(302, headers={"location": "final"})
        return httpx.Response(
            200,
            content=b"done",
            headers={"content-type": "text/plain"},
        )

    def redirect_validator(current_url: str, canonical_target: str):
        validated_hops.append((current_url, canonical_target))
        return "https://evil.example/jobs/replaced"

    bounded, http_client = make_client(source, storage, handler)
    try:
        result = await bounded.fetch(
            "/jobs/start",
            resume=False,
            redirect_validator=redirect_validator,
        )
        cached = await bounded.fetch("/jobs/start", resume=True)
    finally:
        await http_client.aclose()

    assert seen_requests == [
        "https://example.com/jobs/start",
        "https://example.com/jobs/middle",
        "https://example.com/jobs/final",
    ]
    assert validated_hops == [
        (
            "https://example.com/jobs/start",
            "https://example.com/jobs/middle",
        ),
        (
            "https://example.com/jobs/middle",
            "https://example.com/jobs/final",
        ),
    ]
    assert result.final_url == "https://example.com/jobs/final"
    assert cached.from_cache is True
    assert cached.final_url == "https://example.com/jobs/final"
    snapshot = storage.load_snapshot(
        source.source_id, "https://example.com/jobs/start"
    )
    assert snapshot is not None
    assert snapshot.metadata["final_url"] == "https://example.com/jobs/final"


@pytest.mark.asyncio
async def test_redirect_outside_scope_is_rejected_before_request(tmp_path):
    source = make_source()
    calls = 0

    def handler(request: httpx.Request):
        nonlocal calls
        calls += 1
        return httpx.Response(302, headers={"location": "https://evil.example/jobs/1"})

    bounded, http_client = make_client(source, make_storage(tmp_path), handler)
    try:
        with pytest.raises(URLScopeError):
            await bounded.fetch("/jobs/start")
    finally:
        await http_client.aclose()
    assert calls == 1


@pytest.mark.asyncio
async def test_redirect_limit_stops_before_following_excess_hop(tmp_path):
    source = make_source()
    calls = 0

    def handler(request: httpx.Request):
        nonlocal calls
        calls += 1
        return httpx.Response(302, headers={"location": f"/jobs/{calls}"})

    bounded, http_client = make_client(
        source, make_storage(tmp_path), handler, max_redirects=2
    )
    try:
        with pytest.raises(SourceStopped, match="redirect limit"):
            await bounded.fetch("/jobs/start")
    finally:
        await http_client.aclose()
    assert calls == 3


@pytest.mark.asyncio
@pytest.mark.parametrize("concurrent", [False, True])
async def test_virtual_clock_enforces_source_rate_limit_for_sequential_and_concurrent_fetches(
    tmp_path, concurrent
):
    source = make_source(rate_limit_seconds=2.5)
    clock = VirtualClock()
    request_starts = []

    def handler(request: httpx.Request):
        request_starts.append(clock())
        return httpx.Response(200, content=b"ok", headers={"content-type": "text/plain"})

    bounded, http_client = make_client(
        source,
        make_storage(tmp_path),
        handler,
        sleep=clock.sleep,
        clock=clock,
    )
    try:
        urls = [f"/jobs/{index}" for index in range(3)]
        if concurrent:
            await asyncio.gather(
                *(bounded.fetch(url, resume=False) for url in urls)
            )
        else:
            for url in urls:
                await bounded.fetch(url, resume=False)
    finally:
        await http_client.aclose()

    assert request_starts == [0.0, 2.5, 5.0]
    assert all(
        later - earlier >= source.rate_limit_seconds
        for earlier, later in zip(request_starts, request_starts[1:])
    )
    assert [seconds for _, seconds in clock.sleep_events] == [2.5, 2.5]


@pytest.mark.asyncio
async def test_virtual_clock_distinguishes_retry_backoff_from_rate_limit(tmp_path):
    source = make_source(rate_limit_seconds=5.0)
    clock = VirtualClock()
    calls = 0
    request_starts = []

    def handler(request: httpx.Request):
        nonlocal calls
        calls += 1
        request_starts.append(clock())
        if calls < 3:
            return httpx.Response(500)
        return httpx.Response(200, content=b"ok", headers={"content-type": "text/plain"})

    bounded, http_client = make_client(
        source,
        make_storage(tmp_path),
        handler,
        sleep=clock.sleep,
        clock=clock,
        max_attempts=3,
        backoff_seconds=0.25,
    )
    try:
        result = await bounded.fetch("/jobs/1")
    finally:
        await http_client.aclose()

    assert result.ok is True
    assert request_starts == [0.0, 5.0, 10.0]
    assert [seconds for _, seconds in clock.sleep_events] == [0.25, 4.75, 0.5, 4.5]


@pytest.mark.asyncio
async def test_approval_and_scope_are_revalidated_before_every_network_attempt(tmp_path):
    source = make_source(rate_limit_seconds=0.01)

    class CountingRegistry(SourceRegistry):
        def __init__(self):
            super().__init__([source])
            self.automatic_checks = 0
            self.url_checks = 0

        def require_automatic(self, source_id):
            self.automatic_checks += 1
            return super().require_automatic(source_id)

        def validate_url(self, source_id, url):
            self.url_checks += 1
            return super().validate_url(source_id, url)

    calls = 0

    def handler(request: httpx.Request):
        nonlocal calls
        calls += 1
        if calls == 1:
            return httpx.Response(500)
        return httpx.Response(200, content=b"ok")

    registry = CountingRegistry()
    http_client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    bounded = BoundedHttpClient(
        source=source,
        registry=registry,
        storage=make_storage(tmp_path),
        client=http_client,
        sleep=no_sleep,
        clock=lambda: 1.0,
        max_attempts=2,
    )
    try:
        await bounded.fetch("/jobs/1")
    finally:
        await http_client.aclose()

    assert calls == 2
    assert registry.automatic_checks == 3
    assert registry.url_checks == 3


def test_checkpoint_resume_validates_types_and_preserves_url_order(tmp_path):
    storage = make_storage(tmp_path)
    assert storage.load_checkpoint().last_completed_page is None
    assert not storage.checkpoint_path.exists()

    storage.mark_detail_completed("https://example.com/jobs/2")
    storage.mark_detail_completed("https://example.com/jobs/1")
    storage.mark_detail_completed("https://example.com/jobs/2")
    storage.mark_page_completed(4)

    resumed = make_storage(tmp_path).load_checkpoint()
    assert resumed.last_completed_page == 4
    assert resumed.completed_detail_urls == (
        "https://example.com/jobs/2",
        "https://example.com/jobs/1",
    )


def test_checkpoint_persists_monotonic_list_cursor_for_resume(tmp_path):
    storage = make_storage(tmp_path)
    storage.initialize_checkpoint(
        {
            "run_id": storage.run_id,
            "source_ids": ["ncss_public_jobs"],
            "max_records": 10,
            "max_pages": 5,
            "max_requests": 20,
            "manifest_path": None,
        }
    )

    storage.mark_list_cursor("ncss_public_jobs|PYTHON_BACKEND|python", 3)
    resumed = make_storage(tmp_path).load_checkpoint()

    assert resumed.list_cursors == {
        "ncss_public_jobs|PYTHON_BACKEND|python": 3
    }
    with pytest.raises(StorageError, match="cannot decrease"):
        storage.mark_list_cursor("ncss_public_jobs|PYTHON_BACKEND|python", 2)
    assert resumed.updated_at == "2026-08-05T12:00:00+00:00"

    storage.checkpoint_path.write_text(
        json.dumps(
            {
                "last_completed_page": "4",
                "completed_detail_urls": [],
                "updated_at": "2026-08-05T12:00:00+00:00",
            }
        ),
        encoding="utf-8",
    )
    with pytest.raises(StorageError, match="invalid checkpoint"):
        storage.load_checkpoint()


def test_checkpoint_replace_failure_preserves_previous_file_and_cleans_temp(
    tmp_path, monkeypatch
):
    storage = make_storage(tmp_path)
    storage.mark_page_completed(2)
    previous_bytes = storage.checkpoint_path.read_bytes()

    def fail_replace(source, destination):
        raise OSError("simulated replace failure")

    monkeypatch.setattr("src.job_collection.storage.os.replace", fail_replace)
    with pytest.raises(StorageError, match="atomically write"):
        storage.mark_detail_completed("https://example.com/jobs/1")

    assert storage.checkpoint_path.read_bytes() == previous_bytes
    checkpoint = storage.load_checkpoint()
    assert checkpoint.last_completed_page == 2
    assert checkpoint.completed_detail_urls == ()
    assert list(storage.checkpoint_path.parent.glob(".checkpoint.json.*")) == []


def test_storage_rejects_naive_clock_datetime(tmp_path):
    storage = RunStorage(
        tmp_path / "collections",
        "run-naive",
        clock=lambda: datetime(2026, 8, 5, 12, 0),
    )

    with pytest.raises(StorageError, match="timezone-aware"):
        storage.mark_page_completed(1)
    assert not storage.checkpoint_path.exists()


def test_storage_normalizes_aware_clock_to_utc(tmp_path):
    china_time = datetime(
        2026, 8, 5, 20, 0, tzinfo=timezone(timedelta(hours=8))
    )
    storage = RunStorage(
        tmp_path / "collections", "run-offset", clock=lambda: china_time
    )

    checkpoint = storage.mark_page_completed(1)

    assert checkpoint.updated_at == "2026-08-05T12:00:00+00:00"


def test_default_storage_clock_writes_timezone_aware_utc(tmp_path):
    storage = RunStorage(tmp_path / "collections", "run-default-clock")

    checkpoint = storage.mark_page_completed(1)
    parsed = datetime.fromisoformat(checkpoint.updated_at)

    assert parsed.tzinfo is not None
    assert parsed.utcoffset() == timedelta(0)


def test_snapshot_corruption_is_explicit_at_storage_boundary(tmp_path):
    source = make_source()
    storage = make_storage(tmp_path)
    url = "https://example.com/jobs/1"
    storage.write_success(
        source=source,
        url=url,
        final_url=url,
        status=200,
        content_type="text/html",
        content=b"original",
    )
    raw_path, _ = storage.snapshot_paths(source.source_id, url)
    raw_path.write_bytes(b"bad")

    with pytest.raises(SnapshotCorrupt, match="hash mismatch"):
        storage.load_snapshot(source.source_id, url)


@pytest.mark.parametrize("run_id", ["../escape", "..\\escape", "/absolute"])
def test_run_storage_rejects_path_escape(tmp_path, run_id):
    with pytest.raises(StorageError, match="outside run root|invalid run_id"):
        RunStorage(tmp_path / "collections", run_id)


@pytest.mark.parametrize(
    "run_id",
    [
        "CON",
        "con.txt",
        "PRN.json",
        "AUX",
        "NUL.log",
        "COM1",
        "com9.txt",
        "LPT1",
        "lpt9.csv",
        "valid-run.",
        "valid-run ",
    ],
)
def test_run_storage_rejects_windows_unsafe_run_id_segments(tmp_path, run_id):
    with pytest.raises(StorageError, match="reserved|trailing|invalid run_id"):
        RunStorage(tmp_path / "collections", run_id)


@pytest.mark.parametrize(
    "source_id", ["con", "prn", "aux", "nul", "com1", "com9", "lpt1", "lpt9"]
)
def test_snapshot_paths_reject_windows_reserved_source_ids(tmp_path, source_id):
    storage = make_storage(tmp_path)

    with pytest.raises(StorageError, match="reserved"):
        storage.snapshot_paths(source_id, "https://example.com/jobs/1")


@pytest.mark.parametrize("segment", ["CON.txt", "AUX.json", "artifact.", "artifact "])
def test_resolve_path_rejects_windows_unsafe_nested_segments(tmp_path, segment):
    storage = make_storage(tmp_path)

    with pytest.raises(StorageError, match="reserved|trailing"):
        storage.resolve_path("raw", segment)


def test_resolved_target_must_remain_inside_run_root(tmp_path):
    storage = make_storage(tmp_path)
    with pytest.raises(StorageError, match="outside run root"):
        storage.resolve_path("..", "escape.txt")


@pytest.mark.asyncio
async def test_unapproved_source_is_blocked_before_cache_or_network(tmp_path):
    source = make_source(compliance_status="pending_review")
    calls = 0

    def handler(request: httpx.Request):
        nonlocal calls
        calls += 1
        return httpx.Response(200, content=b"must not happen")

    bounded, http_client = make_client(source, make_storage(tmp_path), handler)
    try:
        with pytest.raises(CollectionBlocked):
            await bounded.fetch("/jobs/1")
    finally:
        await http_client.aclose()
    assert calls == 0


@pytest.mark.asyncio
async def test_other_client_error_is_not_retried_and_not_cached(tmp_path):
    source = make_source()
    calls = 0
    storage = make_storage(tmp_path)

    def handler(request: httpx.Request):
        nonlocal calls
        calls += 1
        return httpx.Response(404, content=b"not found", headers={"content-type": "text/plain"})

    bounded, http_client = make_client(source, storage, handler)
    try:
        result = await bounded.fetch("/jobs/missing")
    finally:
        await http_client.aclose()

    assert calls == 1
    assert result.status_code == 404
    assert result.ok is False
    assert storage.load_snapshot(source.source_id, result.url) is None
