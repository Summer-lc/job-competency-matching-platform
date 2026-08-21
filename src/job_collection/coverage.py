from __future__ import annotations

import json
from collections import Counter
from datetime import date, datetime
from pathlib import Path
from typing import Mapping, Sequence

from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

from src.job_data_service import JOB_FAMILY_NAMES


DEFAULT_TARGETS_PATH = (
    Path(__file__).resolve().parents[2] / "config" / "job_collection_targets.json"
)


class CollectionTargets(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    minimum_usable_unique: int = Field(gt=0, strict=True)
    maximum_usable_unique: int = Field(gt=0, strict=True)
    minimum_source_domains: int = Field(gt=0, strict=True)
    minimum_source_types: int = Field(gt=0, strict=True)
    maximum_single_domain_share: float = Field(gt=0.0, le=1.0, strict=True)
    minimum_usable_per_family: int = Field(gt=0, strict=True)
    minimum_published_year: int = Field(ge=2000, le=2100, strict=True)
    maximum_published_year: int = Field(ge=2000, le=2100, strict=True)
    require_trusted_published_at: bool = Field(strict=True)
    family_minimum_overrides: dict[str, int]
    source_targets: dict[str, int] = Field(min_length=1)
    city_tier_minimum_shares: dict[str, float]
    required_job_families: tuple[str, ...] = Field(min_length=1)

    @model_validator(mode="after")
    def validate_contract(self) -> "CollectionTargets":
        if self.minimum_usable_unique > self.maximum_usable_unique:
            raise ValueError("minimum usable target cannot exceed maximum")
        if self.minimum_published_year > self.maximum_published_year:
            raise ValueError("minimum published year cannot exceed maximum published year")
        if any(
            family not in self.required_job_families or value <= 0
            for family, value in self.family_minimum_overrides.items()
        ):
            raise ValueError(
                "family minimum override must name a required family and be positive"
            )
        if any(value <= 0 for value in self.source_targets.values()):
            raise ValueError("source targets must be positive")
        expected_tiers = {"tier_1", "new_tier_1", "tier_2", "other"}
        if set(self.city_tier_minimum_shares) != expected_tiers:
            raise ValueError("city tier targets must define the four reviewed tiers")
        if any(
            value < 0.0 or value > 1.0
            for value in self.city_tier_minimum_shares.values()
        ):
            raise ValueError("city tier shares must be between zero and one")
        if sum(self.city_tier_minimum_shares.values()) > 1.0:
            raise ValueError("city tier minimum shares cannot exceed one")
        if set(self.required_job_families) != set(JOB_FAMILY_NAMES):
            raise ValueError("required job families must match JOB_FAMILY_NAMES")
        if len(self.required_job_families) != len(set(self.required_job_families)):
            raise ValueError("required job families must be unique")
        return self


def load_collection_targets(
    path: str | Path = DEFAULT_TARGETS_PATH,
) -> CollectionTargets:
    target_path = Path(path)
    try:
        return CollectionTargets.model_validate_json(
            target_path.read_text(encoding="utf-8")
        )
    except (OSError, UnicodeError, json.JSONDecodeError, ValidationError) as exc:
        raise ValueError(f"invalid collection targets {target_path}: {exc}") from exc


_CITY_TIERS = {
    "tier_1": {"beijing", "shanghai", "guangzhou", "shenzhen", "北京", "上海", "广州", "深圳"},
    "new_tier_1": {
        "chengdu", "hangzhou", "chongqing", "wuhan", "suzhou", "xi'an", "xian",
        "nanjing", "tianjin", "zhengzhou", "changsha", "dongguan", "ningbo",
        "foshan", "hefei", "qingdao", "成都", "杭州", "重庆", "武汉", "苏州",
        "西安", "南京", "天津", "郑州", "长沙", "东莞", "宁波", "佛山", "合肥", "青岛",
    },
    "tier_2": {
        "kunming", "shenyang", "jinan", "wuxi", "xiamen", "fuzhou", "wenzhou",
        "jinhua", "harbin", "dalian", "guiyang", "nanning", "quanzhou", "shijiazhuang",
        "changchun", "nanchang", "huizhou", "changzhou", "jiaxing", "xuzhou", "nantong",
        "taiyuan", "baoding", "zhuhai", "zhongshan", "lanzhou", "linyi", "weifang",
        "yantai", "shaoxing", "昆明", "沈阳", "济南", "无锡", "厦门", "福州", "温州",
        "金华", "哈尔滨", "大连", "贵阳", "南宁", "泉州", "石家庄", "长春", "南昌",
        "惠州", "常州", "嘉兴", "徐州", "南通", "太原", "保定", "珠海", "中山",
        "兰州", "临沂", "潍坊", "烟台", "绍兴",
    },
}


def _city_tier(region: object) -> str:
    value = str(region or "").strip().casefold()
    for tier, names in _CITY_TIERS.items():
        if any(name in value for name in names):
            return tier
    return "other"


def _published_year(value: object) -> int | None:
    if isinstance(value, datetime):
        return value.year
    if isinstance(value, date):
        return value.year
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        return datetime.fromisoformat(value.strip().replace("Z", "+00:00")).year
    except ValueError:
        return None


def _family_target(targets: CollectionTargets, family: str) -> int:
    return targets.family_minimum_overrides.get(
        family, targets.minimum_usable_per_family
    )


def _is_usable(row: Mapping[str, object], targets: CollectionTargets) -> bool:
    year = _published_year(row.get("published_at"))
    trusted = row.get("published_at_trusted")
    return bool(
        row.get("gate_status") == "valid"
        and row.get("provenance_status") == "approved"
        and row.get("duplicate_of_id") is None
        and row.get("job_family_id") in targets.required_job_families
        and year is not None
        and targets.minimum_published_year <= year <= targets.maximum_published_year
        and (
            not targets.require_trusted_published_at
            or trusted is True
            or (isinstance(trusted, int) and trusted == 1)
        )
    )


def build_coverage_report(
    *,
    targets: CollectionTargets,
    usable_rows: Sequence[Mapping[str, object]],
) -> dict[str, object]:
    canonical: list[Mapping[str, object]] = []
    seen: set[str] = set()
    excluded = 0
    for index, row in enumerate(usable_rows):
        if not _is_usable(row, targets):
            excluded += 1
            continue
        identity = str(
            row.get("record_id")
            or row.get("content_hash")
            or row.get("id")
            or f"row-{index}"
        )
        if identity in seen:
            excluded += 1
            continue
        seen.add(identity)
        canonical.append(row)

    total = len(canonical)
    family_counts = Counter(str(row.get("job_family_id")) for row in canonical)
    source_counts = Counter(str(row.get("source_id") or "unknown") for row in canonical)
    domain_counts = Counter(
        str(row.get("source_domain")).strip().casefold()
        for row in canonical
        if str(row.get("source_domain") or "").strip()
    )
    source_types = {
        str(row.get("source_type")).strip()
        for row in canonical
        if str(row.get("source_type") or "").strip()
    }
    city_counts = Counter(_city_tier(row.get("region")) for row in canonical)
    year_counts = Counter(_published_year(row.get("published_at")) for row in canonical)
    maximum_share = max(domain_counts.values(), default=0) / total if total else 0.0
    ordered_family_counts = {
        family: family_counts.get(family, 0)
        for family in targets.required_job_families
    }
    missing = [family for family, count in ordered_family_counts.items() if count == 0]
    family_targets = {
        family: _family_target(targets, family)
        for family in targets.required_job_families
    }
    below_minimum = [
        {
            "job_family_id": family,
            "current": count,
            "target": family_targets[family],
            "gap": max(0, family_targets[family] - count),
        }
        for family, count in ordered_family_counts.items()
        if count < family_targets[family]
    ]
    core_family_windows = {
        family: {
            "2022-2023": sum(
                1
                for row in canonical
                if row.get("job_family_id") == family
                and _published_year(row.get("published_at")) in {2022, 2023}
            ),
            "2024-2025": sum(
                1
                for row in canonical
                if row.get("job_family_id") == family
                and _published_year(row.get("published_at")) in {2024, 2025}
            ),
            "2026": sum(
                1
                for row in canonical
                if row.get("job_family_id") == family
                and _published_year(row.get("published_at")) == 2026
            ),
        }
        for family in targets.family_minimum_overrides
    }
    return {
        "usable_unique": {
            "current": total,
            "target": [targets.minimum_usable_unique, targets.maximum_usable_unique],
            "gap_to_minimum": max(0, targets.minimum_usable_unique - total),
        },
        "family_counts": ordered_family_counts,
        "family_targets": family_targets,
        "core_family_windows": core_family_windows,
        "missing_families": missing,
        "families_below_minimum": below_minimum,
        "minimum_usable_per_family": {
            "current": min(ordered_family_counts.values(), default=0),
            "target": targets.minimum_usable_per_family,
        },
        "date_window": {
            "minimum_year": targets.minimum_published_year,
            "maximum_year": targets.maximum_published_year,
            "trusted_required": targets.require_trusted_published_at,
            "year_counts": {
                str(year): year_counts.get(year, 0)
                for year in range(
                    targets.minimum_published_year,
                    targets.maximum_published_year + 1,
                )
            },
        },
        "source_counts": dict(sorted(source_counts.items())),
        "source_domains": {
            "current": len(domain_counts),
            "target": targets.minimum_source_domains,
            "counts": dict(sorted(domain_counts.items())),
        },
        "source_types": {
            "current": len(source_types),
            "target": targets.minimum_source_types,
            "values": sorted(source_types),
        },
        "maximum_single_domain_share": {
            "current": round(maximum_share, 6),
            "target": targets.maximum_single_domain_share,
        },
        "city_tiers": {
            tier: {
                "current": city_counts.get(tier, 0),
                "share": round(city_counts.get(tier, 0) / total, 6) if total else 0.0,
                "minimum_share": targets.city_tier_minimum_shares[tier],
            }
            for tier in targets.city_tier_minimum_shares
        },
        "excluded_rows": excluded,
    }


def recommend_batches(
    *,
    targets: CollectionTargets,
    coverage: Mapping[str, object],
    batch_size: int = 100,
) -> tuple[dict[str, object], ...]:
    if isinstance(batch_size, bool) or not isinstance(batch_size, int) or batch_size < 1:
        raise ValueError("batch_size must be a positive integer")
    usable = dict(coverage.get("usable_unique") or {})
    current_total = int(usable.get("current") or 0)
    total_gap = max(0, targets.minimum_usable_unique - current_total)
    if total_gap == 0:
        return ()
    family_counts = {
        family: int(dict(coverage.get("family_counts") or {}).get(family) or 0)
        for family in targets.required_job_families
    }
    source_counts = {
        source_id: int(dict(coverage.get("source_counts") or {}).get(source_id) or 0)
        for source_id in targets.source_targets
    }
    city_tiers = dict(coverage.get("city_tiers") or {})
    tier_order = sorted(
        targets.city_tier_minimum_shares,
        key=lambda tier: (
            -(
                targets.city_tier_minimum_shares[tier]
                - float(dict(city_tiers.get(tier) or {}).get("share") or 0.0)
            ),
            tier,
        ),
    )
    family_order = sorted(
        targets.required_job_families,
        key=lambda family: (family_counts[family], family),
    )
    planned_total = 0
    recommendations: list[dict[str, object]] = []
    for index, family in enumerate(family_order):
        if planned_total >= total_gap:
            break
        family_gap = max(0, _family_target(targets, family) - family_counts[family])
        amount_limit = min(batch_size, total_gap - planned_total)
        if family_gap:
            amount_limit = min(amount_limit, family_gap)
        candidates = sorted(
            targets.source_targets,
            key=lambda source_id: (
                source_counts[source_id] / max(1, current_total + planned_total),
                -(targets.source_targets[source_id] - source_counts[source_id]),
                source_id,
            ),
        )
        chosen = None
        chosen_amount = 0
        chosen_share = 0.0
        for source_id in candidates:
            source_room = targets.source_targets[source_id] - source_counts[source_id]
            amount = min(amount_limit, source_room)
            if amount <= 0:
                continue
            denominator = current_total + planned_total + amount
            projected_share = (source_counts[source_id] + amount) / max(1, denominator)
            if current_total == 0:
                projected_share = (source_counts[source_id] + amount) / targets.minimum_usable_unique
            if projected_share <= targets.maximum_single_domain_share:
                chosen = source_id
                chosen_amount = amount
                chosen_share = projected_share
                break
        if chosen is None:
            continue
        source_counts[chosen] += chosen_amount
        family_counts[family] += chosen_amount
        planned_total += chosen_amount
        recommendations.append(
            {
                "source_id": chosen,
                "job_family_id": family,
                "city_tier": tier_order[index % len(tier_order)],
                "requested_records": chosen_amount,
                "projected_source_count": source_counts[chosen],
                "projected_source_share": round(chosen_share, 6),
            }
        )
    return tuple(recommendations)


__all__ = [
    "CollectionTargets",
    "build_coverage_report",
    "load_collection_targets",
    "recommend_batches",
]
