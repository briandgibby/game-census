"""Public read contracts. They deliberately contain no operator configuration."""

from datetime import datetime
from decimal import Decimal
from typing import Literal, Annotated

from pydantic import BaseModel, ConfigDict, Field


class ReadModel(BaseModel):
    model_config = ConfigDict(extra="ignore", populate_by_name=True)


class SourceFailure(ReadModel):
    code: str
    message: str
    next_action: str


class LastAttempt(ReadModel):
    status: str
    at: datetime | None = None
    error: SourceFailure | None = None


class EnrichmentValue(ReadModel):
    app_id: int = Field(ge=1, le=4294967295)
    query: dict
    query_hash: str


class PriceValue(EnrichmentValue):
    kind: Literal['store']
    state: Literal['priced','free','unknown','unavailable']
    country: str
    currency: str | None
    product: str
    price: dict | None
    metadata: dict | None = None


class ReviewValue(EnrichmentValue):
    kind: Literal['reviews']
    state: Literal['available']
    total_reviews: int = Field(ge=0)
    total_positive: int = Field(ge=0)
    total_negative: int = Field(ge=0)
    positive_percent: float | None = Field(ge=0,le=100)
    review_score: int = Field(ge=0,le=10)
    review_score_desc: str


class AchievementPercent(ReadModel):
    name: str
    percent: Decimal = Field(ge=0,le=100)


class AchievementValue(EnrichmentValue):
    kind: Literal['achievements']
    state: Literal['available','unsupported']
    achievements: list[AchievementPercent]


class AchievementSchema(ReadModel):
    name: str
    display_name: str
    description: str
    hidden: int = Field(ge=0,le=1)
    icon: str | None
    icon_gray: str | None


class SchemaValue(EnrichmentValue):
    kind: Literal['achievement_schema']
    state: Literal['available','unsupported']
    achievements: list[AchievementSchema]


class NewsArticle(ReadModel):
    gid: str
    title: str
    date: int = Field(ge=0,le=253402300799)
    feedname: Literal['steam_community_announcements']
    url: str


class NewsValue(EnrichmentValue):
    kind: Literal['news']
    state: Literal['available']
    articles: list[NewsArticle]


class ReviewDelta(ReadModel):
    total_reviews: int
    total_positive: int
    total_negative: int
    since: datetime


class EnrichmentObservation(ReadModel):
    capture_id: str
    observed_at: datetime
    value: Annotated[PriceValue | ReviewValue | AchievementValue | SchemaValue | NewsValue, Field(discriminator='kind')]
    matches_current_query: bool
    net_delta: ReviewDelta | None = None


class PriceSeries(ReadModel):
    country: str
    currency: str | None
    product: str
    started_at: datetime
    latest_at: datetime
    observations: int = Field(ge=1)
    lowest_observed_minor: int | None = Field(default=None, ge=0)


class EnrichmentHistory(ReadModel):
    app_id: int = Field(ge=1, le=4294967295)
    kind: Literal["store", "reviews", "achievements", "achievement_schema", "news"]
    source: str
    source_version: str
    current_query: dict
    current_query_hash: str
    availability: Literal["no_observations", "stale", "available", "unsupported", "priced", "free", "unavailable", "unknown"]
    last_attempt: LastAttempt | None
    observations: list[EnrichmentObservation]
    series: list[PriceSeries]
    has_more: bool
    next_cursor: str | None
    methodology: str


class AppSummary(ReadModel):
    app_id: int = Field(ge=1, le=4294967295)
    name: str
    player_count: int | None = Field(default=None, ge=0)
    availability: Literal["fresh", "stale", "no_observations", "unsupported", "not_tracked"]
    observed_at: datetime | None = None
    tracking_started_at: datetime | None = None
    tracking_ended_at: datetime | None = None
    sample_count: int = Field(ge=0)
    observed_24h_peak: int | None = Field(default=None, ge=0)
    highest_recorded: int | None = Field(default=None, ge=0)
    last_attempt: LastAttempt | None = None
    expected_interval_seconds: int | None = Field(default=None, gt=0)
    source: str
    source_version: str
    catalog_source: str | None = None
    catalog_observed_at: datetime | None = None


class AppList(ReadModel):
    items: list[AppSummary]
    total: int
    tracking_scope: Literal["enrolled", "catalog"] = "enrolled"
    generated_at: datetime
    page: int | None = Field(default=None, ge=1)
    page_size: int | None = Field(default=None, ge=1, le=500)


class PlayerPoint(ReadModel):
    observed_at: datetime
    player_count: int = Field(ge=0)


class Coverage(ReadModel):
    sample_count: int = Field(ge=0)
    expected_samples: int = Field(ge=0)
    covered_seconds: float = Field(ge=0)
    requested_seconds: float = Field(ge=0)
    maximum_gap_seconds: float = Field(ge=0)
    coverage_ratio: float = Field(ge=0, le=1)
    tracked_seconds: float | None = Field(default=None, ge=0)
    tracked_coverage_ratio: float | None = Field(default=None, ge=0, le=1)
    tracking_started_at: datetime | None = None


class HistoryMetrics(ReadModel):
    observed_peak: int | None = Field(default=None, ge=0)
    average_observed_ccu: float | None = Field(default=None, ge=0)
    estimator_version: str
    observed_peak_at: datetime | None = None
    observed_minimum: int | None = Field(default=None, ge=0)
    observed_minimum_at: datetime | None = None
    integral_player_seconds: float | None = Field(default=None, ge=0)
    metric_policy_version: str | None = None


class HistoryGap(ReadModel):
    from_time: datetime = Field(alias="from")
    to: datetime
    seconds: float = Field(ge=0)


class HistoryGrowth(ReadModel):
    from_time: datetime = Field(alias="from")
    to: datetime
    comparison_from: datetime
    comparison_to: datetime
    older_average: float | None
    newer_average: float | None
    absolute_change: float | None
    percentage_change: float | None
    status: Literal["available", "insufficient_coverage", "no_observations", "zero_baseline"]
    minimum_coverage_ratio: float = Field(ge=0, le=1)
    older_coverage_ratio: float = Field(ge=0, le=1)
    newer_coverage_ratio: float = Field(ge=0, le=1)
    metric_policy_version: str


class HistoryRollup(ReadModel):
    from_time: datetime = Field(alias="from")
    to: datetime
    first: PlayerPoint | None
    last: PlayerPoint | None
    minimum: PlayerPoint | None
    maximum: PlayerPoint | None
    sample_count: int = Field(ge=0)
    integral_player_seconds: float = Field(ge=0)
    covered_seconds: float = Field(ge=0)
    requested_seconds: float = Field(ge=0)
    average_observed_ccu: float | None
    gaps: list[HistoryGap]


class PlayerHistory(ReadModel):
    app_id: int
    from_time: datetime = Field(alias="from")
    to: datetime
    points: list[PlayerPoint]
    coverage: Coverage
    metrics: HistoryMetrics
    source: str
    source_version: str
    gaps: list[HistoryGap]
    tracking_scope: Literal["recorded_by_this_instance"] = "recorded_by_this_instance"
    resolution: Literal["raw", "bucketed"] = "raw"
    bucket_seconds: int | None = Field(default=None, ge=1)
    rollups: list[HistoryRollup] = Field(default_factory=list)
    growth: HistoryGrowth | None = None


class CohortMember(ReadModel):
    app_id: int = Field(ge=1, le=4294967295)
    role: Literal["pinned", "active", "exploration"]
    since: datetime | None = None


class CohortStatus(ReadModel):
    enabled: bool
    policy_matches: bool
    state: Literal["configured", "adopted", "policy_changed"]
    event_id: int | None = None
    adopted_at: datetime | None = None
    members: list[CohortMember]
    exploration_slots_reserved: int = Field(ge=0, le=24)


class PublicStatus(ReadModel):
    read_service: Literal["ready"] = "ready"
    generated_at: datetime
    tracked_apps: int
    retained_apps: int = 0
    fresh_apps: int
    stale_apps: int
    apps_without_observations: int
    total_observations: int
    source: str
    collection_mode: Literal["manual", "scheduled"] = "manual"
    schedule_state: Literal["disabled", "enabled", "plan_changed"] = "disabled"
    last_run: dict | None = None
    cohort: CohortStatus


class RankedApp(AppSummary):
    rank: int = Field(ge=1)


class RankingExclusions(ReadModel):
    stale: int = Field(ge=0)
    no_observations: int = Field(ge=0)
    unsupported: int = Field(ge=0)
    not_initialized: int = Field(ge=0)


class Rankings(ReadModel):
    items: list[RankedApp]
    cohort_size: int = Field(ge=0)
    ranked_apps: int = Field(ge=0)
    excluded: RankingExclusions
    generated_at: datetime
    source: str
    source_version: str
    scope: Literal["fresh_observations_in_configured_cohort"] = "fresh_observations_in_configured_cohort"


class ComparisonSeries(ReadModel):
    app_id: int = Field(ge=1, le=4294967295)
    name: str
    availability: Literal["fresh", "stale", "no_observations", "unsupported", "not_tracked"]
    player_count: int | None = Field(default=None, ge=0)
    observed_at: datetime | None = None
    tracking_started_at: datetime | None = None
    tracking_ended_at: datetime | None = None
    expected_interval_seconds: int | None = Field(default=None, gt=0)
    last_attempt: LastAttempt | None = None
    history: PlayerHistory | None = None
    reason: str | None = None


class Comparison(ReadModel):
    from_time: datetime = Field(alias="from")
    to: datetime
    generated_at: datetime
    series: list[ComparisonSeries]
    returned_points: int = Field(ge=0)
    source: str
    source_version: str
    scope: Literal["recorded_by_this_instance"] = "recorded_by_this_instance"
