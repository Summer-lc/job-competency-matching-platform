from __future__ import annotations

import json
import re
from collections import Counter
from pathlib import Path
from urllib.parse import urlparse

from schemes.job_competency import EvidenceInput


APPROVED_JOB_FAMILIES = {
    "BIG_DATA_DEVELOPER",
    "CYBERSECURITY_ENGINEER",
    "DATA_ENGINEER",
    "DATA_GOVERNANCE_ENGINEER",
    "DIGITAL_TWIN_ENGINEER",
    "EDGE_COMPUTING_ENGINEER",
    "IOT_ENGINEER",
    "ROBOTICS_ENGINEER",
}

SUPPLEMENTAL_JOB_FAMILIES = {
    "AI_AGENT_ENGINEER",
    "AI_SOLUTION_ENGINEER",
    "CLOUD_NATIVE_ENGINEER",
    "DEVOPS_ENGINEER",
    "FRONTEND_DEVELOPER",
    "GO_DEVELOPER",
    "JAVA_DEVELOPER",
    "LLM_APPLICATION_ENGINEER",
    "MLOPS_ENGINEER",
    "MULTIMODAL_ENGINEER",
    "PROMPT_ENGINEER",
    "PYTHON_BACKEND",
    "RAG_ENGINEER",
    "SRE_ENGINEER",
}

SUPPORTED_JOB_FAMILIES = APPROVED_JOB_FAMILIES | SUPPLEMENTAL_JOB_FAMILIES

APPROVED_EVIDENCE_TYPES = {
    "occupation_standard",
    "technical_standard",
    "policy_document",
    "official_document",
}

APPROVED_DOMAIN_SUFFIXES = (
    "gov.cn",
    "mohrss.gov.cn",
    "miit.gov.cn",
    "openstd.samr.gov.cn",
    "samr.gov.cn",
    "iso.org",
    "iec.ch",
    "nist.gov",
    "etsi.org",
    "apache.org",
    "kubernetes.io",
    "docs.ros.org",
    "ros.org",
    "docs.spring.io",
    "oracle.com",
    "python.org",
    "djangoproject.com",
    "fastapi.tiangolo.com",
    "go.dev",
    "grpc.io",
    "protobuf.dev",
    "w3.org",
    "tc39.es",
    "typescriptlang.org",
    "jenkins.io",
    "gitlab.com",
    "hashicorp.com",
    "sre.google",
    "opentelemetry.io",
    "prometheus.io",
    "istio.io",
    "helm.sh",
    "modelcontextprotocol.io",
    "openai.com",
    "anthropic.com",
    "claude.com",
    "google.dev",
    "llamaindex.ai",
    "langchain.com",
    "mlflow.org",
    "kubeflow.org",
)

STANDARD_EVIDENCE_TYPES = {"occupation_standard", "technical_standard"}
KNOWN_INVALID_SOURCE_URLS = {
    "https://www.iso.org/standard/78843.html",
}


def load_jsonl(path: str | Path) -> list[dict]:
    dataset_path = Path(path)
    records: list[dict] = []
    for line_number, line in enumerate(
        dataset_path.read_text(encoding="utf-8-sig").splitlines(), start=1
    ):
        if not line.strip():
            continue
        try:
            value = json.loads(line)
        except json.JSONDecodeError as exc:
            raise ValueError(f"JSONL 第 {line_number} 行格式错误: {exc.msg}") from exc
        if not isinstance(value, dict):
            raise ValueError(f"JSONL 第 {line_number} 行必须是 JSON 对象")
        records.append(value)
    return records


def _is_approved_domain(hostname: str) -> bool:
    host = hostname.lower().strip(".")
    return any(host == suffix or host.endswith(f".{suffix}") for suffix in APPROVED_DOMAIN_SUFFIXES)


def _require_unique(records: list[dict], field_name: str) -> None:
    values = [str(record.get(field_name) or "").strip() for record in records]
    duplicates = sorted(value for value, count in Counter(values).items() if value and count > 1)
    if duplicates:
        raise ValueError(f"{field_name} 重复: {', '.join(duplicates)}")


def validate_official_evidence_records(
    records: list[dict],
    *,
    expected_total: int | None = 24,
    enforce_family_mix: bool = True,
    required_families: set[str] | frozenset[str] | None = None,
) -> dict:
    mix_families = (
        APPROVED_JOB_FAMILIES
        if required_families is None
        else set(required_families)
    )
    unsupported_required = mix_families - SUPPORTED_JOB_FAMILIES
    if unsupported_required:
        raise ValueError(
            "required_families contains unsupported values: "
            + ", ".join(sorted(unsupported_required))
        )
    validated: list[dict] = []
    for index, record in enumerate(records, start=1):
        try:
            item = EvidenceInput.model_validate(record)
        except Exception as exc:
            raise ValueError(f"第 {index} 条证据字段校验失败: {exc}") from exc
        values = item.model_dump(mode="json", exclude_none=True)
        if values["job_family_id"] not in SUPPORTED_JOB_FAMILIES:
            raise ValueError(f"第 {index} 条证据使用未批准岗位族: {values['job_family_id']}")
        if values["evidence_type"] not in APPROVED_EVIDENCE_TYPES:
            raise ValueError(f"第 {index} 条证据类型不受支持: {values['evidence_type']}")

        parsed_url = urlparse(values["source_url"])
        if parsed_url.scheme.lower() != "https":
            raise ValueError(f"第 {index} 条证据源链接必须使用 HTTPS")
        normalized_url = values["source_url"].lower().rstrip("/")
        if normalized_url in KNOWN_INVALID_SOURCE_URLS:
            raise ValueError(
                f"第 {index} 条证据命中了 known incorrect source mapping: "
                f"{values['source_url']}"
            )
        hostname = parsed_url.hostname or ""
        if not _is_approved_domain(hostname):
            raise ValueError(f"第 {index} 条证据使用非准入官方域名: {hostname or '空域名'}")
        if "example.com" in values["source_url"].lower():
            raise ValueError(f"第 {index} 条证据使用示例链接")

        chinese_count = len(re.findall(r"[\u4e00-\u9fff]", values["evidence_summary"]))
        if not 60 <= chinese_count <= 200:
            raise ValueError(
                f"第 {index} 条证据摘要需包含 60-200 个汉字，当前为 {chinese_count}"
            )
        validated.append(values)

    for field_name in ("evidence_id", "title", "source_url"):
        _require_unique(validated, field_name)

    if expected_total is not None and len(validated) != expected_total:
        raise ValueError(f"证据总数必须为 {expected_total}，当前为 {len(validated)}")

    family_counts = Counter(item["job_family_id"] for item in validated)
    type_counts = Counter(item["evidence_type"] for item in validated)
    if enforce_family_mix:
        expected_counts = {family: 3 for family in mix_families}
        if dict(family_counts) != expected_counts:
            raise ValueError(f"每个岗位族必须恰好 3 条证据，当前为 {dict(family_counts)}")
        for family in mix_families:
            family_types = {
                item["evidence_type"]
                for item in validated
                if item["job_family_id"] == family
            }
            if len(family_types) < 2:
                raise ValueError(f"岗位族 {family} 至少需要两种证据类型")
            if not family_types.intersection(STANDARD_EVIDENCE_TYPES):
                raise ValueError(f"岗位族 {family} 至少需要一条标准类证据")

    return {
        "total": len(validated),
        "family_counts": dict(sorted(family_counts.items())),
        "type_counts": dict(sorted(type_counts.items())),
    }
