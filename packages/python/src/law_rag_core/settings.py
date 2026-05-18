from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from zoneinfo import ZoneInfo

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


def get_repo_root() -> Path:
    return Path(__file__).resolve().parents[4]


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    app_env: str = Field(default="local", alias="APP_ENV")
    app_base_url: str = Field(default="http://localhost:3000", alias="APP_BASE_URL")
    api_base_url: str = Field(default="http://localhost:8000", alias="API_BASE_URL")
    app_timezone: str = Field(default="Asia/Seoul", alias="APP_TIMEZONE")

    gemini_api_key: str | None = Field(default=None, alias="GEMINI_API_KEY")
    gemini_model: str = Field(default="gemini-2.5-flash-lite", alias="GEMINI_MODEL")
    gemini_embedding_model: str = Field(default="gemini-embedding-001", alias="GEMINI_EMBEDDING_MODEL")
    embedding_dim: int = Field(default=256, alias="EMBEDDING_DIM")

    retrieval_top_k: int = Field(default=6, alias="RETRIEVAL_TOP_K")
    retrieval_min_score: float = Field(default=0.35, alias="RETRIEVAL_MIN_SCORE")

    admin_api_key: str | None = Field(default=None, alias="ADMIN_API_KEY")
    cors_allow_origins_raw: str = Field(default="*", alias="CORS_ALLOW_ORIGINS")

    daily_question_limit: int = Field(default=500, alias="DAILY_QUESTION_LIMIT")
    enable_server_chat_history: bool = Field(default=False, alias="ENABLE_SERVER_CHAT_HISTORY")
    enable_article_segment: bool = Field(default=False, alias="ENABLE_ARTICLE_SEGMENT")
    enable_admin_upload_ui: bool = Field(default=True, alias="ENABLE_ADMIN_UPLOAD_UI")
    enable_appendix_search: bool = Field(default=False, alias="ENABLE_APPENDIX_SEARCH")
    enable_vector_search: bool = Field(default=True, alias="ENABLE_VECTOR_SEARCH")
    artifact_dir_override: Path | None = Field(default=None, alias="ARTIFACT_DIR")

    law_api_key: str | None = Field(default=None, alias="LAW_API_KEY")
    data_go_kr_service_key: str | None = Field(default=None, alias="DATA_GO_KR_SERVICE_KEY")

    kakao_client_id: str = Field(default="", alias="KAKAO_CLIENT_ID")
    kakao_client_secret: str = Field(default="", alias="KAKAO_CLIENT_SECRET")
    kakao_redirect_uri: str = Field(default="http://localhost:3000/auth/callback/", alias="KAKAO_REDIRECT_URI")
    jwt_secret: str = Field(default="change-me-in-production", alias="JWT_SECRET")
    jwt_expire_hours: int = Field(default=72, alias="JWT_EXPIRE_HOURS")

    @property
    def artifact_dir(self) -> Path:
        return self.artifact_dir_override or (get_repo_root() / "data" / "artifacts")

    @property
    def law_db_path(self) -> Path:
        return self.artifact_dir / "laws.sqlite"

    @property
    def vector_matrix_path(self) -> Path:
        return self.artifact_dir / "article_embeddings.npy"

    @property
    def vector_ids_path(self) -> Path:
        return self.artifact_dir / "article_embedding_ids.json"

    @property
    def artifact_manifest_path(self) -> Path:
        return self.artifact_dir / "manifest.json"

    @property
    def state_db_path(self) -> Path:
        return self.artifact_dir / "state.sqlite"

    @property
    def timezone_info(self) -> ZoneInfo:
        return ZoneInfo(self.app_timezone)

    @property
    def sqlite_schema_path(self) -> Path:
        return get_repo_root() / "apps" / "api" / "db" / "sqlite_schema.sql"

    @property
    def cors_allow_origins(self) -> list[str]:
        origins = [item.strip() for item in self.cors_allow_origins_raw.split(",") if item.strip()]
        return origins or ["*"]

    @property
    def cors_allow_credentials(self) -> bool:
        return "*" not in self.cors_allow_origins


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()
