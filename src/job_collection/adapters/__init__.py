from src.job_collection.adapters.base import (
    AdapterRecordError,
    AdapterStructureError,
    ListPage,
    RequestSpec,
    SourceAdapter,
    SourceJobRecord,
)
from src.job_collection.adapters.ncss import NCSSAdapter
from src.job_collection.adapters.mohrss import MOHRSSAdapter
from src.job_collection.adapters.feishu_ats import FeishuATSAdapter
from src.job_collection.adapters.beisen_ats import BeisenATSAdapter
from src.job_collection.adapters.legacy_file import LegacyFileAdapter, LegacyFileAdapterError
from src.job_collection.adapters.authorized_export import (
    AuthorizedExportAdapter,
    AuthorizedExportAdapterError,
)

__all__ = [
    "AdapterRecordError",
    "AdapterStructureError",
    "AuthorizedExportAdapter",
    "AuthorizedExportAdapterError",
    "BeisenATSAdapter",
    "FeishuATSAdapter",
    "ListPage",
    "LegacyFileAdapter",
    "LegacyFileAdapterError",
    "MOHRSSAdapter",
    "NCSSAdapter",
    "RequestSpec",
    "SourceAdapter",
    "SourceJobRecord",
]
