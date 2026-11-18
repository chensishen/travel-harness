from functools import lru_cache
from pathlib import Path

from pydantic import Field, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")
    llm_api_key: SecretStr = SecretStr("")
    llm_base_url: str = "http://127.0.0.1:3425/v1"
    llm_model: str = "workbuddy/deepseek-v4.1-flash"
    siliconflow_api_key: SecretStr = SecretStr("")
    embed_base_url: str = "https://api.siliconflow.cn/v1"
    embed_model: str = "Qwen/Qwen3-VL-Embedding-8B"
    rerank_model: str = "Qwen/Qwen3-Reranker-8B"
    milvus_uri: str = "http://127.0.0.1:19530"
    mcp_config_path: Path = Path("~/.codex/config.toml")
    mcp_search_server: str = "exa"
    mcp_fetch_server: str = "fetch"
    mcp_image_server: str = "magpie-image"
    osrm_base_url: str = "https://router.project-osrm.org"
    osrm_foot_base_url: str = "https://routing.openstreetmap.de/routed-foot"
    overpass_base_url: str = "https://overpass-api.de/api/interpreter"
    vision_model: str = "Qwen/Qwen3-VL-8B-Instruct"
    photo_similarity_min: float = 0.20
    nominatim_base_url: str = "https://nominatim.openstreetmap.org"
    http_user_agent: str = "TravelHarness/0.1 (via Codex; https://github.com/openai/codex)"
    fetch_proxy_fake_ip: bool = False
    transit_multiplier: float = 1.5
    transit_wait_min: int = 10
    transit_rail_speed_kmh: float = 35
    transit_stop_min: float = .5
    transit_transfer_min: float = 5
    taxi_time_min_multiplier: float = 1.2
    taxi_time_max_multiplier: float = 1.5
    detour_factor: float = 1.4
    research_concurrency: int = Field(default=2, ge=1, le=2)
    fetch_concurrency: int = Field(default=3, ge=1, le=3)
    data_dir: Path = Path("data")
    primary_color: str = "#176B5B"


@lru_cache
def settings() -> Settings:
    return Settings()
