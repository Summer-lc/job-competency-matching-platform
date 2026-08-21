from __future__ import annotations

import asyncio
import hashlib
import json
import re
import time
from dataclasses import dataclass
from html.parser import HTMLParser
from typing import Awaitable, Callable, NoReturn
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

import httpx

from src.job_collection.models import SourceDefinition
from src.job_collection.source_registry import CollectionBlocked, SourceRegistry
from src.job_collection.storage import (
    RunStorage,
    SnapshotCorrupt,
    StorageError,
    StoredSnapshot,
)


RETRYABLE_STATUSES = frozenset({500, 502, 503, 504})
STOP_STATUSES = frozenset({401, 403, 429})
REDIRECT_STATUSES = frozenset({301, 302, 303, 307, 308})
DEFAULT_USER_AGENT = (
    "JobResearch/1.0 (public job research; project=job-competency; "
    "contact=project-maintainer)"
)
_MAX_SAMPLED_BODY_CHARS = 12_000
DEFAULT_MAX_RESPONSE_BYTES = 5 * 1024 * 1024


class SourceStopped(RuntimeError):
    """Further automatic requests to this source must stop immediately."""


class RequestBudget:
    """One run-wide counter consumed by every physical HTTP attempt."""

    def __init__(self, max_requests: int, *, used: int = 0) -> None:
        if isinstance(max_requests, bool) or max_requests < 1:
            raise ValueError("max_requests must be a positive integer")
        if isinstance(used, bool) or not isinstance(used, int) or not 0 <= used <= max_requests:
            raise ValueError("used requests must be between zero and max_requests")
        self.max_requests = max_requests
        self.used = used
        self._lock = asyncio.Lock()

    async def consume(self) -> None:
        async with self._lock:
            if self.used >= self.max_requests:
                raise SourceStopped(
                    f"run-wide request budget exhausted at {self.max_requests} requests"
                )
            self.used += 1


@dataclass(frozen=True)
class FetchResult:
    source_id: str
    run_id: str
    url: str
    final_url: str
    status_code: int
    content_type: str
    content: bytes
    content_hash: str
    parser_version: str
    from_cache: bool

    @property
    def ok(self) -> bool:
        return 200 <= self.status_code < 300


class _HtmlAccessDetector(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self._in_title = False
        self._in_heading = False
        self.title_parts: list[str] = []
        self.heading_parts: list[str] = []
        self.body_parts: list[str] = []
        self.body_char_count = 0
        self.password_input = False
        self.login_form = False
        self.captcha_structure = False
        self.captcha_input = False

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        lowered = tag.lower()
        attributes = {name.lower(): (value or "").lower() for name, value in attrs}
        if lowered == "title":
            self._in_title = True
        if lowered in {"h1", "h2", "h3"}:
            self._in_heading = True
        if lowered == "input" and attributes.get("type") == "password":
            self.password_input = True
        if lowered == "form" and any(
            marker in attributes.get("action", "")
            for marker in ("login", "signin", "sign-in")
        ):
            self.login_form = True
        structural_text = " ".join(
            attributes.get(name, "") for name in ("id", "class", "name", "src")
        )
        if any(marker in structural_text for marker in ("captcha", "verify-code", "verification-code")):
            self.captcha_structure = True
            if lowered == "input":
                self.captcha_input = True

    def handle_endtag(self, tag: str) -> None:
        if tag.lower() == "title":
            self._in_title = False
        if tag.lower() in {"h1", "h2", "h3"}:
            self._in_heading = False

    def handle_data(self, data: str) -> None:
        if self._in_title:
            self.title_parts.append(data)
        if self._in_heading:
            self.heading_parts.append(data)
        remaining = _MAX_SAMPLED_BODY_CHARS - self.body_char_count
        if remaining > 0:
            sampled = data[:remaining]
            self.body_parts.append(sampled)
            self.body_char_count += len(sampled)


def _looks_like_access_gate(response: httpx.Response) -> bool:
    content_type = response.headers.get("content-type", "").lower()
    prefix = response.content[:256].lstrip().lower()
    if "html" not in content_type and not prefix.startswith((b"<!doctype html", b"<html")):
        return False
    detector = _HtmlAccessDetector()
    try:
        detector.feed(response.text[:100_000])
    except (UnicodeError, ValueError):
        return False

    title = " ".join("".join(detector.title_parts).split()).lower()
    headings = " ".join("".join(detector.heading_parts).split()).lower()
    heading_text = f"{title} {headings}".strip()
    explicit_verification_markers = (
        "访问验证",
        "安全验证",
        "access verification",
        "verify you are human",
    )
    if any(marker in heading_text for marker in explicit_verification_markers):
        return True

    login_heading_markers = (
        "用户登录",
        "请登录",
        "登录验证",
        "sign in",
        "log in",
    )
    visible_text = " ".join("".join(detector.body_parts).split()).lower()
    short_page = len(visible_text) <= 8_000
    if short_page and any(marker in heading_text for marker in login_heading_markers):
        return True

    short_page_markers = (
        "请输入验证码",
        "请完成验证",
        "访问验证",
        "安全验证",
        "请先登录",
        "登录后访问",
        "登录后查看",
        "需要登录",
        "verify you are human",
        "sign in to continue",
        "log in to continue",
    )
    supporting_structure = (
        detector.password_input or detector.login_form or detector.captcha_structure
    )
    return short_page and supporting_structure and any(
        marker in visible_text for marker in short_page_markers
    )


class BoundedHttpClient:
    def __init__(
        self,
        *,
        source: SourceDefinition,
        registry: SourceRegistry,
        storage: RunStorage,
        client: httpx.AsyncClient,
        sleep: Callable[[float], Awaitable[None]] | None = None,
        clock: Callable[[], float] | None = None,
        timeout_seconds: float = 10.0,
        max_attempts: int = 3,
        backoff_seconds: float = 1.0,
        max_redirects: int = 5,
        max_response_bytes: int = DEFAULT_MAX_RESPONSE_BYTES,
        request_budget: RequestBudget | None = None,
        user_agent: str = DEFAULT_USER_AGENT,
    ) -> None:
        if timeout_seconds <= 0:
            raise ValueError("timeout_seconds must be positive")
        if max_attempts < 1:
            raise ValueError("max_attempts must be at least 1")
        if backoff_seconds < 0:
            raise ValueError("backoff_seconds cannot be negative")
        if max_redirects < 0:
            raise ValueError("max_redirects cannot be negative")
        if (
            isinstance(max_response_bytes, bool)
            or max_response_bytes < 1
            or max_response_bytes > 100 * 1024 * 1024
        ):
            raise ValueError("max_response_bytes must be between 1 and 104857600")
        self._validate_user_agent(user_agent)

        self.source = source
        self.registry = registry
        self.storage = storage
        self.client = client
        self.sleep = sleep or asyncio.sleep
        self.clock = clock or time.monotonic
        self.timeout_seconds = timeout_seconds
        self.max_attempts = max_attempts
        self.backoff_seconds = backoff_seconds
        self.max_redirects = max_redirects
        self.max_response_bytes = max_response_bytes
        self.request_budget = request_budget
        self.user_agent = user_agent
        self._last_request_started: float | None = None
        self._rate_lock = asyncio.Lock()
        self._stop_reason: str | None = None

    @property
    def stopped(self) -> bool:
        return self._stop_reason is not None

    @property
    def stop_reason(self) -> str | None:
        return self._stop_reason

    async def fetch(
        self,
        url: str,
        *,
        method: str = "GET",
        headers: dict[str, str] | None = None,
        json_body: dict[str, object] | None = None,
        resume: bool = True,
        redirect_validator: Callable[[str, str], object] | None = None,
    ) -> FetchResult:
        if self._stop_reason is not None:
            raise SourceStopped(self._stop_reason)
        requested_url = self._validate_request_boundary(url)
        normalized_method = method.upper()
        if normalized_method not in {"GET", "POST"}:
            raise ValueError("method must be GET or POST")
        if normalized_method == "GET" and json_body is not None:
            raise ValueError("GET requests cannot include json_body")
        if normalized_method == "POST" and json_body is None:
            raise ValueError("POST requests require json_body")
        request_headers = self._validated_adapter_headers(headers or {})
        json_payload = self._json_payload(json_body) if json_body is not None else None
        snapshot_url = self._snapshot_identity_url(
            requested_url, normalized_method, json_payload
        )

        if resume:
            try:
                cached = self.storage.load_snapshot(self.source.source_id, snapshot_url)
            except SnapshotCorrupt:
                cached = None
            if cached is not None:
                return self._cached_result(snapshot_url, cached)

        current_url = requested_url
        redirects_followed = 0
        while True:
            response = await self._request_with_retries(
                snapshot_url,
                current_url,
                method=normalized_method,
                headers=request_headers,
                json_payload=json_payload,
            )
            if response.status_code in REDIRECT_STATUSES and "location" in response.headers:
                if normalized_method == "POST":
                    self._stop_source(
                        f"POST redirect refused for source {self.source.source_id}",
                        requested_url=snapshot_url,
                        final_url=current_url,
                        response=response,
                    )
                if redirects_followed >= self.max_redirects:
                    self._stop_source(
                        f"redirect limit exceeded for source {self.source.source_id}",
                        requested_url=requested_url,
                        final_url=current_url,
                        response=response,
                    )
                location = response.headers["location"]
                canonical_target = self.registry.validate_redirect(
                    self.source.source_id,
                    current_url,
                    location,
                )
                if redirect_validator is not None:
                    redirect_validator(current_url, canonical_target)
                current_url = canonical_target
                redirects_followed += 1
                continue
            return self._finalize_response(snapshot_url, current_url, response)

    async def _request_with_retries(
        self,
        requested_url: str,
        current_url: str,
        *,
        method: str,
        headers: dict[str, str],
        json_payload: bytes | None,
    ) -> httpx.Response:
        for attempt in range(1, self.max_attempts + 1):
            try:
                response = await self._send_once(
                    requested_url,
                    current_url,
                    method=method,
                    headers=headers,
                    json_payload=json_payload,
                )
            except httpx.TransportError:
                if attempt == self.max_attempts:
                    raise
                await self._backoff(attempt)
                continue

            if response.status_code not in RETRYABLE_STATUSES or attempt == self.max_attempts:
                return response
            await self._backoff(attempt)
        raise RuntimeError("unreachable retry state")

    async def _send_once(
        self,
        requested_url: str,
        url: str,
        *,
        method: str,
        headers: dict[str, str],
        json_payload: bytes | None,
    ) -> httpx.Response:
        url = self._validate_request_boundary(url)
        async with self._rate_lock:
            if self._stop_reason is not None:
                raise SourceStopped(self._stop_reason)
            now = self.clock()
            if self._last_request_started is not None:
                remaining = self.source.rate_limit_seconds - (
                    now - self._last_request_started
                )
                if remaining > 0:
                    await self.sleep(remaining)
                    now = self.clock()
            if self._stop_reason is not None:
                raise SourceStopped(self._stop_reason)
            self._last_request_started = now
            if self.request_budget is not None:
                await self.request_budget.consume()
            request_headers = {"User-Agent": self.user_agent, **headers}
            if json_payload is not None:
                request_headers.setdefault("Content-Type", "application/json")
            self.client.cookies.clear()
            async with self.client.stream(
                method,
                url,
                headers=request_headers,
                content=json_payload,
                timeout=self.timeout_seconds,
                follow_redirects=False,
            ) as streamed:
                declared = streamed.headers.get("content-length")
                if declared is not None:
                    try:
                        declared_size = int(declared)
                    except ValueError:
                        declared_size = -1
                    if declared_size < 0:
                        self._stop_oversized_body(
                            "response body has an invalid Content-Length",
                            requested_url=requested_url,
                            final_url=url,
                            status=streamed.status_code,
                            content_type=streamed.headers.get("content-type", ""),
                        )
                    if declared_size > self.max_response_bytes:
                        self._stop_oversized_body(
                            f"response body exceeds {self.max_response_bytes} bytes",
                            requested_url=requested_url,
                            final_url=url,
                            status=streamed.status_code,
                            content_type=streamed.headers.get("content-type", ""),
                        )
                chunks: list[bytes] = []
                received = 0
                async for chunk in streamed.aiter_bytes():
                    received += len(chunk)
                    if received > self.max_response_bytes:
                        self._stop_oversized_body(
                            f"response body exceeds {self.max_response_bytes} bytes",
                            requested_url=requested_url,
                            final_url=url,
                            status=streamed.status_code,
                            content_type=streamed.headers.get("content-type", ""),
                        )
                    chunks.append(chunk)
                decoded_headers = [
                    (name, value)
                    for name, value in streamed.headers.multi_items()
                    if name.lower()
                    not in {"content-encoding", "content-length", "transfer-encoding"}
                ]
                response = httpx.Response(
                    streamed.status_code,
                    headers=decoded_headers,
                    content=b"".join(chunks),
                    request=streamed.request,
                )
            self.client.cookies.clear()
            if response.status_code in STOP_STATUSES:
                self._stop_source(
                    f"source {self.source.source_id} returned stop status "
                    f"{response.status_code}",
                    requested_url=requested_url,
                    final_url=url,
                    response=response,
                )
            if _looks_like_access_gate(response):
                self._stop_source(
                    f"login or verification page detected for source {self.source.source_id}",
                    requested_url=requested_url,
                    final_url=url,
                    response=response,
                )
            return response

    @staticmethod
    def _json_payload(value: dict[str, object]) -> bytes:
        try:
            return json.dumps(
                value,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
                allow_nan=False,
            ).encode("utf-8")
        except (TypeError, ValueError) as exc:
            raise ValueError("json_body must contain finite JSON values") from exc

    @staticmethod
    def _snapshot_identity_url(url: str, method: str, payload: bytes | None) -> str:
        if method == "GET":
            return url
        digest = hashlib.sha256(payload or b"").hexdigest()
        parsed = urlsplit(url)
        pairs = [
            (name, value)
            for name, value in parse_qsl(parsed.query, keep_blank_values=True)
            if name != "__job_research_body_sha256"
        ]
        pairs.append(("__job_research_body_sha256", digest))
        return urlunsplit(
            (parsed.scheme, parsed.netloc, parsed.path, urlencode(pairs), "")
        )

    @staticmethod
    def _validated_adapter_headers(headers: dict[str, str]) -> dict[str, str]:
        blocked = {
            "authorization",
            "proxy-authorization",
            "cookie",
            "set-cookie",
            "x-api-key",
            "x-auth-token",
            "host",
        }
        token = re.compile(r"^[!#$%&'*+.^_`|~0-9A-Za-z-]+$")
        validated: dict[str, str] = {}
        for name, value in headers.items():
            if not isinstance(name, str) or not isinstance(value, str):
                raise ValueError("request headers must contain strings")
            if not token.fullmatch(name) or name.casefold() in blocked:
                raise ValueError(f"request header is not allowed: {name}")
            if any(ord(character) < 32 or ord(character) == 127 for character in value):
                raise ValueError(f"request header contains control characters: {name}")
            validated[name] = value
        return validated

    def _stop_oversized_body(
        self,
        reason: str,
        *,
        requested_url: str,
        final_url: str,
        status: int,
        content_type: str,
    ) -> NoReturn:
        if self._stop_reason is None:
            self._stop_reason = reason
        try:
            self.storage.write_error_metadata(
                source=self.source,
                url=requested_url,
                final_url=final_url,
                status=status,
                content_type=content_type,
                content=b"",
            )
        except StorageError:
            pass
        raise SourceStopped(self._stop_reason)

    def _validate_request_boundary(self, url: str) -> str:
        reviewed_source = self.registry.require_automatic(self.source.source_id)
        if reviewed_source != self.source:
            raise CollectionBlocked(
                f"injected source definition differs from reviewed registry: {self.source.source_id}"
            )
        return self.registry.validate_url(self.source.source_id, url)

    @staticmethod
    def _validate_user_agent(user_agent: str) -> None:
        normalized = user_agent.strip().lower()
        has_research_purpose = "jobresearch" in normalized or "research" in normalized
        has_identity = re.search(
            r"(?:contact|project)\s*[:=]\s*[^\s;,)]+", normalized
        ) is not None
        if (
            not normalized
            or "competition" in normalized
            or "比赛" in user_agent
            or not has_research_purpose
            or not has_identity
        ):
            raise ValueError(
                "user_agent must identify JobResearch/Research purpose and a "
                "contact or project, without competition wording"
            )

    def _stop_source(
        self,
        reason: str,
        *,
        requested_url: str,
        final_url: str,
        response: httpx.Response,
    ) -> NoReturn:
        if self._stop_reason is None:
            self._stop_reason = reason
        try:
            self._store_error(requested_url, final_url, response)
        except StorageError:
            pass
        raise SourceStopped(self._stop_reason)

    async def _backoff(self, failed_attempt: int) -> None:
        delay = self.backoff_seconds * (2 ** (failed_attempt - 1))
        if delay > 0:
            await self.sleep(delay)

    def _finalize_response(
        self, requested_url: str, final_url: str, response: httpx.Response
    ) -> FetchResult:
        content_type = response.headers.get("content-type", "")
        content = response.content
        content_hash = hashlib.sha256(content).hexdigest()
        if 200 <= response.status_code < 300:
            snapshot = self.storage.write_success(
                source=self.source,
                url=requested_url,
                final_url=final_url,
                status=response.status_code,
                content_type=content_type,
                content=content,
            )
            content_hash = snapshot.metadata["content_hash"]
        else:
            self._store_error(requested_url, final_url, response)
        return FetchResult(
            source_id=self.source.source_id,
            run_id=self.storage.run_id,
            url=requested_url,
            final_url=final_url,
            status_code=response.status_code,
            content_type=content_type,
            content=content,
            content_hash=content_hash,
            parser_version=self.source.parser_version,
            from_cache=False,
        )

    def _store_error(
        self, requested_url: str, final_url: str, response: httpx.Response
    ) -> None:
        self.storage.write_error_metadata(
            source=self.source,
            url=requested_url,
            final_url=final_url,
            status=response.status_code,
            content_type=response.headers.get("content-type", ""),
            content=response.content,
        )

    def _cached_result(self, requested_url: str, snapshot: StoredSnapshot) -> FetchResult:
        metadata = snapshot.metadata
        return FetchResult(
            source_id=self.source.source_id,
            run_id=self.storage.run_id,
            url=requested_url,
            final_url=metadata["final_url"],
            status_code=metadata["status"],
            content_type=metadata["content_type"],
            content=snapshot.content,
            content_hash=metadata["content_hash"],
            parser_version=metadata["parser_version"],
            from_cache=True,
        )
