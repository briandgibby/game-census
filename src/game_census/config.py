"""The canonical configuration schema and safe first-run generator."""
from __future__ import annotations

import json
import os
from pathlib import Path
import secrets
from typing import Annotated, Literal
from urllib.parse import urlsplit

from pydantic import BaseModel, ConfigDict, Field, PrivateAttr, SecretStr, ValidationError, field_validator


class ConfigurationError(ValueError):
    """Safe actionable error without echoing untrusted values."""


class Model(BaseModel):
    model_config = ConfigDict(extra="forbid", validate_default=True, strict=True)


class Storage(Model):
    database_url: SecretStr
    backup_path: str = "/state/backups"
    partition_months_ahead: int = Field(default=2, ge=1, le=12)
    max_partition_months: int = Field(default=24, ge=1, le=120)
    migration_max_captures: int = Field(default=100000, ge=1, le=1000000)
    backup_max_bytes: int = Field(default=10737418240, ge=1048576, le=1099511627776)
    recovery_timeout_seconds: int = Field(default=600, ge=10, le=86400)

    @field_validator("max_partition_months")
    @classmethod
    def maintenance_covers_lookahead(cls, value: int, info) -> int:
        ahead = info.data.get("partition_months_ahead")
        if ahead is not None and value < ahead + 1:
            raise ValueError("must include the current month plus storage.partition_months_ahead")
        return value

    @field_validator("database_url")
    @classmethod
    def database_scheme(cls, value: SecretStr) -> SecretStr:
        try:
            parsed = urlsplit(value.get_secret_value())
            valid = (parsed.scheme in ("postgresql", "postgres") and parsed.hostname
                     and parsed.username and parsed.password and parsed.path not in ("", "/")
                     and parsed.port in (None, *range(1, 65536)) and not parsed.query and not parsed.fragment)
        except ValueError:
            valid = False
        if not valid:
            raise ValueError("must be a PostgreSQL connection URL with host, database and credentials")
        return value

    @field_validator("backup_path")
    @classmethod
    def safe_backup(cls, value: str) -> str:
        if not value or "\x00" in value or ".." in Path(value).parts:
            raise ValueError("must be a nonempty path without parent traversal")
        return value


class Tracking(Model):
    app_ids: list[Annotated[int, Field(ge=1, le=4294967295)]] = Field(default_factory=lambda: [570], min_length=1, max_length=25)
    interval_seconds: int = Field(default=300, ge=300, le=604800)

    @field_validator("app_ids")
    @classmethod
    def unique(cls, value: list[int]) -> list[int]:
        if len(value) != len(set(value)):
            raise ValueError("must contain unique app IDs")
        return value


class Http(Model):
    timeout_seconds: int = Field(default=15, ge=1, le=60)
    max_attempts: int = Field(default=1, ge=1, le=5)
    min_interval_seconds: int = Field(default=1, ge=1, le=3600)
    max_response_bytes: int = Field(default=2000000, ge=1024, le=10000000)


class Sources(Model):
    store_metadata_enabled: bool = True
    catalog_api_key: SecretStr | None = None


class Quota(Model):
    webapi_rolling_24h: int = Field(default=90000, ge=1, le=100000)
    store_rolling_24h: int = Field(default=5000, ge=1, le=10000)


class EnrichmentPolicy(Model):
    enabled: bool = False
    interval_seconds: int = Field(default=3600, ge=300, le=604800)


class StorePolicy(EnrichmentPolicy):
    country: str = Field(default="us", pattern=r"^[a-z]{2}$")
    language: str = Field(default="english", pattern=r"^[a-z_]{2,30}$")


class ReviewPolicy(EnrichmentPolicy):
    language: str = Field(default="all", pattern=r"^[a-z_]{2,30}$")
    filter: Literal["all", "recent", "updated"] = "all"
    day_range: int = Field(default=365, ge=1, le=365)
    review_type: Literal["all", "positive", "negative"] = "all"
    purchase_type: Literal["all", "steam", "non_steam_purchase"] = "all"
    include_offtopic: bool = False


class NewsPolicy(EnrichmentPolicy):
    count: int = Field(default=5, ge=1, le=20)


class SchemaPolicy(EnrichmentPolicy):
    language: str = Field(default="english", pattern=r"^[a-z_]{2,30}$")


class Enrichment(Model):
    store: StorePolicy = Field(default_factory=StorePolicy)
    reviews: ReviewPolicy = Field(default_factory=ReviewPolicy)
    achievements: EnrichmentPolicy = Field(default_factory=EnrichmentPolicy)
    achievement_schema: SchemaPolicy = Field(default_factory=SchemaPolicy)
    news: NewsPolicy = Field(default_factory=NewsPolicy)
    history_limit: int = Field(default=100, ge=1, le=1000)


class Catalog(Model):
    page_size: int = Field(default=1000, ge=1, le=50000)
    max_pages_per_run: int = Field(default=5, ge=1, le=20)
    sync_interval_seconds: int = Field(default=3600, ge=60, le=604800)
    full_scan_interval_seconds: int = Field(default=604800, ge=3600, le=2592000)
    overlap_seconds: int = Field(default=300, ge=1, le=86400)
    include_games: bool = True
    include_dlc: bool = False
    include_software: bool = False
    include_videos: bool = False
    include_hardware: bool = False


class Cohort(Model):
    enabled: bool = False
    max_apps: int = Field(default=3, ge=2, le=25)
    exploration_slots: int = Field(default=1, ge=1, le=24)
    candidate_limit: int = Field(default=1000, ge=1, le=10000)
    max_evidence_samples: int = Field(default=5000, ge=50, le=250000)
    min_samples: int = Field(default=3, ge=2, le=24)
    evidence_max_age_seconds: int = Field(default=3600, ge=300, le=604800)
    min_residency_seconds: int = Field(default=3600, ge=300, le=604800)
    reconcile_interval_seconds: int = Field(default=3600, ge=300, le=604800)
    promote_above: int = Field(default=1000, ge=1, le=1000000000)
    demote_below: int = Field(default=100, ge=0, le=999999999)
    max_replacements: int = Field(default=1, ge=1, le=25)
    discovery_webapi_reserve: int = Field(default=20, ge=0, le=10000)
    discovery_store_reserve: int = Field(default=2, ge=0, le=10000)

    @field_validator("exploration_slots")
    @classmethod
    def room_for_pinned_apps(cls, value, info):
        if value >= info.data.get("max_apps", 25):
            raise ValueError("must be smaller than cohort.max_apps")
        return value

    @field_validator("demote_below")
    @classmethod
    def distinct_thresholds(cls, value, info):
        if value >= info.data.get("promote_above", 1000000001):
            raise ValueError("must be lower than cohort.promote_above")
        return value


class Metrics(Model):
    gap_cap_multiplier: float = Field(default=2.0, ge=1, le=4)
    freshness_interval_multiplier: float = Field(default=2.0, ge=1, le=4)
    min_coverage_ratio: float = Field(default=0.9, ge=0, le=1)


class Scheduler(Model):
    lease_seconds: int = Field(default=120, ge=5, le=600)
    max_run_seconds: int = Field(default=3600, ge=5, le=86400)
    poll_seconds: int = Field(default=1, ge=1, le=60)
    retry_reserve: int = Field(default=10, ge=0, le=10000)


class Cache(Model):
    bucket_seconds: int = Field(default=3600, ge=300, le=86400)
    rebuild_max_buckets: int = Field(default=8760, ge=1, le=20000)

    @field_validator("bucket_seconds")
    @classmethod
    def utc_day_divisor(cls, value: int) -> int:
        if 86400 % value:
            raise ValueError("must divide 86400 seconds exactly for UTC day alignment")
        return value


class Web(Model):
    bind: Literal["0.0.0.0", "127.0.0.1"] = "0.0.0.0"
    publish_host: Literal["127.0.0.1"] = "127.0.0.1"
    port: int = Field(default=8000, ge=1024, le=65535)
    refresh_seconds: int = Field(default=60, ge=15, le=3600)
    max_points: int = Field(default=2000, ge=10, le=10000)
    max_history_days: int = Field(default=90, ge=1, le=365)
    max_history_samples: int = Field(default=250000, ge=100, le=1000000)
    max_compare_apps: int = Field(default=5, ge=1, le=10)
    max_compare_points: int = Field(default=10000, ge=10, le=100000)
    max_page_size: int = Field(default=100, ge=1, le=500)


class Benchmark(Model):
    app_count: int = Field(default=25, ge=1, le=25)
    history_days: int = Field(default=90, ge=1, le=90)
    requests_per_second: int = Field(default=20, ge=1, le=50)
    duration_seconds: int = Field(default=30, ge=1, le=300)
    workers: int = Field(default=20, ge=1, le=25)


class Settings(Model):
    schema_version: Literal[1] = 1
    storage: Storage
    tracking: Tracking = Field(default_factory=Tracking)
    http: Http = Field(default_factory=Http)
    sources: Sources = Field(default_factory=Sources)
    quota: Quota = Field(default_factory=Quota)
    catalog: Catalog = Field(default_factory=Catalog)
    enrichment: Enrichment = Field(default_factory=Enrichment)
    cohort: Cohort = Field(default_factory=Cohort)
    metrics: Metrics = Field(default_factory=Metrics)
    scheduler: Scheduler = Field(default_factory=Scheduler)
    cache: Cache = Field(default_factory=Cache)
    web: Web = Field(default_factory=Web)
    benchmark: Benchmark = Field(default_factory=Benchmark)
    _cohort_pinned_app_ids: list[int] | None = PrivateAttr(default=None)

    @field_validator("schema_version", mode="before")
    @classmethod
    def strict_schema_version(cls, value):
        if type(value) is not int:
            raise ValueError("must be integer 1")
        return value


def config_path(value: str | Path | None = None) -> Path:
    return Path(value or os.environ.get("GAME_CENSUS_CONFIG", "config/local.json")).resolve()


def _unique_keys(pairs: list[tuple[str, object]]) -> dict:
    result = {}
    for key, value in pairs:
        if key in result:
            safe_key = key if key.isidentifier() and len(key) <= 80 else "<invalid key>"
            raise ConfigurationError(f"Duplicate configuration setting: {safe_key}. Remove the duplicate key and retry.")
        result[key] = value
    return result


def load_settings(path: str | Path | None = None) -> Settings:
    target = config_path(path)
    try:
        if target.stat().st_size > 65536:
            raise ConfigurationError("Configuration exceeds 65536 bytes. Use a smaller configuration file.")
        text = target.read_text(encoding="utf-8")
        data = json.loads(text, object_pairs_hook=_unique_keys)
        return Settings.model_validate(data)
    except FileNotFoundError as exc:
        raise ConfigurationError("Configuration is missing. Run config init or tools/dev.py quickstart --once.") from exc
    except ValidationError as exc:
        # Do not echo Pydantic's input field or original exception: it can contain credentials.
        keys = sorted({".".join(map(str, error["loc"])) or "configuration" for error in exc.errors()})
        raise ConfigurationError("Invalid configuration setting(s): " + ", ".join(keys) + ". Run config describe --schema for supported bounds.") from exc
    except (json.JSONDecodeError, UnicodeError) as exc:
        raise ConfigurationError("Configuration is not valid UTF-8 JSON. Correct its syntax and retry.") from exc
    except OSError as exc:
        raise ConfigurationError("Cannot read configuration. Check the configured path and file permissions.") from exc


def generate_profile(path: str | Path | None = None, *, app_ids: list[int] | None = None, port: int = 8000) -> dict:
    target = config_path(path)
    if target.exists():
        existing = load_settings(target)
        return {"status": "existing", "config_path": str(target), "app_ids": existing.tracking.app_ids}
    password = secrets.token_urlsafe(32)
    settings = Settings(storage=Storage(database_url=SecretStr(f"postgresql://game_census:{password}@db:5432/game_census")),
                        tracking=Tracking(app_ids=[570] if app_ids is None else app_ids), web=Web(port=port))
    data = settings.model_dump(mode="json")
    data["storage"]["database_url"] = settings.storage.database_url.get_secret_value()
    target.parent.mkdir(parents=True, exist_ok=True)
    try:
        with target.open("x", encoding="utf-8") as stream:
            stream.write(json.dumps(data, indent=2) + "\n")
        target.chmod(0o600)
    except FileExistsError:
        return {"status": "existing", "config_path": str(target), "app_ids": load_settings(target).tracking.app_ids}
    return {"status": "created", "config_path": str(target), "app_ids": settings.tracking.app_ids}


def describe_settings(settings: Settings | None = None) -> dict:
    if settings is None:
        return Settings.model_json_schema()
    return settings.model_dump(mode="json")
