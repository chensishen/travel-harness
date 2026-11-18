from datetime import UTC, date, datetime
from datetime import date as Date
from typing import Literal

from pydantic import BaseModel, Field, HttpUrl, field_validator, model_validator


def now() -> str:
    return datetime.now(UTC).isoformat()


class TaskSpec(BaseModel):
    type: Literal["travel_guide", "culture_intro"]
    city: str = Field(min_length=1, max_length=80)
    days: int | None = Field(default=None, ge=1, le=14)
    travelers: int = Field(default=1, ge=1, le=50)
    interests: list[str] = Field(default_factory=list)
    excluded_pois: list[str] = Field(default_factory=list)
    budget_level: str = "适中"
    season_or_dates: str = "未指定"
    origin: str = "未指定"
    assumptions: list[str] = Field(default_factory=list)
    start_date: date | None = None
    date_assumed: bool = False
    departure_time: str = Field(default="09:00", pattern=r"^\d{2}:\d{2}$")
    transport_preference: str = "公共交通"

    @model_validator(mode="after")
    def defaults(self):
        if self.type == "travel_guide" and self.days is None:
            self.days = 3
            if "默认行程 3 天 2 晚" not in self.assumptions:
                self.assumptions.append("默认行程 3 天 2 晚")
        if self.type == "culture_intro":
            self.days = None
        return self


class SourceRef(BaseModel):
    id: str
    title: str
    url: HttpUrl
    level: Literal["L1", "L2", "L3"]
    fetched_at: str
    text: str = ""
    published_at: date | None = None


class FactRecord(BaseModel):
    field: str
    value: str
    source_ids: list[str] = Field(default_factory=list)
    evidence: str = ""
    confidence: float = Field(default=0, ge=0, le=1)
    verified_at: date = Field(default_factory=date.today)
    published_at: date | None = None
    freshness_note: str = ""
    note: str | None = None

    @field_validator("verified_at", mode="before")
    @classmethod
    def default_verification_date(cls, value):
        return date.today() if value is None else value


class OpeningHours(BaseModel):
    open: str = Field(pattern=r"^\d{2}:\d{2}$")
    close: str = Field(pattern=r"^\d{2}:\d{2}$")
    last_entry: str | None = Field(default=None, pattern=r"^\d{2}:\d{2}$")
    closed_days: list[int] = Field(default_factory=list)
    note: str = ""
    source_ids: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def valid_window(self):
        for value in [self.open, self.close, self.last_entry]:
            if value:
                hour, minute = map(int, value.split(":"))
                if not 0 <= hour < 24 or not 0 <= minute < 60:
                    raise ValueError("开放时间格式无效")
        if any(day not in range(7) for day in self.closed_days):
            raise ValueError("闭馆日必须是周一=0至周日=6")
        return self


class ImageRef(BaseModel):
    url: HttpUrl
    author: str
    license: str
    source_page: HttpUrl
    local_path: str = ""
    generated: bool = False
    relevance_verified: bool = False
    similarity: float | None = None
    perceptual_hash: str = ""
    license_url: str = ""
    subject_verified: bool = False
    caption: str = ""


class Block(BaseModel):
    text: str
    source_ids: list[str]
    kind: Literal["fact", "advice"] = "fact"


class POI(BaseModel):
    id: str
    name: str
    lat: float | None = Field(default=None, ge=-90, le=90)
    lng: float | None = Field(default=None, ge=-180, le=180)
    location_note: str = ""
    location_source: str = ""
    duration_min: int = Field(default=90, ge=15, le=480)
    facts: list[FactRecord] = Field(default_factory=list)
    blocks: list[Block] = Field(default_factory=list)
    images: list[ImageRef] = Field(default_factory=list)
    hours: OpeningHours | None = None
    minimum_duration_min: int = Field(default=45, ge=15, le=480)
    official_url: str = ""
    verification_url: str = ""
    contact: str = ""
    verification_attempts: list[str] = Field(default_factory=list)
    photo_status: str = "暂无合规实拍图"
    location_verified: bool = False
    category: str = ""
    aliases: list[str] = Field(default_factory=list)
    wikidata: str = ""
    osm_tags: dict = Field(default_factory=dict)
    map_thumbnail: str = ""
    official_transit: list[dict] = Field(default_factory=list)
    dining: list[dict] = Field(default_factory=list)
    nationally_rated: bool = False
    planning_priority: int = 0


class Segment(BaseModel):
    from_poi: str
    to_poi: str
    mode: str = "公共交通（估算）"
    duration_min: float = Field(ge=0)
    distance_m: float = Field(ge=0)
    polyline: list[list[float]]
    source: str
    estimated: bool = True
    from_name: str = ""
    to_name: str = ""
    route_verified: bool = False
    geometry_mode: str = ""
    instructions: list[str] = Field(default_factory=list)
    parts: list[dict] = Field(default_factory=list)
    transit: dict = Field(default_factory=dict)


class ScheduleEntry(BaseModel):
    start: str = Field(pattern=r"^\d{2}:\d{2}$")
    end: str = Field(pattern=r"^\d{2}:\d{2}$")
    kind: Literal["visit", "travel", "meal", "wait"]
    place: str
    poi_id: str = ""
    transport: str = ""
    note: str = ""


class DayPlan(BaseModel):
    day: int
    theme: str
    stops: list[POI]
    segments: list[Segment] = Field(default_factory=list)
    map_png: str = ""
    meals: list[str] = Field(default_factory=lambda: ["12:00–13:00 午餐", "18:00–19:00 晚餐"])
    notes: list[str] = Field(default_factory=list)
    date: Date | None = None
    timeline: list[ScheduleEntry] = Field(default_factory=list)
    total_minutes: int = 0
    map_coverage: float = 0
    basemap: dict = Field(default_factory=dict)
    arrival_segment: Segment | None = None
    origin: POI | None = None
    buffer_minutes: int = 0


class CultureChapter(BaseModel):
    title: str
    period: str = ""
    blocks: list[Block]
    images: list[ImageRef] = Field(default_factory=list)


class PracticalItem(BaseModel):
    category: str
    label: str
    value: str
    source_ids: list[str] = Field(default_factory=list)
    estimated: bool = False
    amount_range: list[float] = Field(default_factory=list)
    basis: str = ""


class ReaderCheck(BaseModel):
    poi: str
    text: str
    official_url: str
    targets: list[dict] = Field(default_factory=list)


class Deliverable(BaseModel):
    spec: TaskSpec
    days: list[DayPlan] = Field(default_factory=list)
    pois: list[POI] = Field(default_factory=list)
    chapters: list[CultureChapter] = Field(default_factory=list)
    sources: list[SourceRef] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)
    created_at: str = Field(default_factory=now)
    prd_version: str = "1.0"
    alternatives: list[dict] = Field(default_factory=list)
    practical: list[PracticalItem] = Field(default_factory=list)
    reader_checks: list[ReaderCheck] = Field(default_factory=list)
    overview_basemap: dict = Field(default_factory=dict)
    decorations: list[ImageRef] = Field(default_factory=list)
    candidate_count: int = 0
    origin_hints: list[dict] = Field(default_factory=list)


GenerationMode = Literal["fast", "balanced", "complete"]
PHOTO_SKIPPED = "快速模式未检索实拍图"


class JobRequest(BaseModel):
    prompt: str = Field(min_length=2, max_length=2000)
    options: dict = Field(default_factory=dict)
    generation_mode: GenerationMode = "complete"
    style: dict = Field(default_factory=dict)


class Revision(BaseModel):
    instruction: str = Field(min_length=2, max_length=2000)


NODES = [
    "Router",
    "Planner",
    "Researcher",
    "Verifier",
    "Writer",
    "RoutePlanner",
    "Illustrator",
    "Reviewer",
    "Assembler",
]
