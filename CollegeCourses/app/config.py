from typing import Optional

from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    app_host: str = "0.0.0.0"
    app_port: int = 8000

    default_max_depth: int = 3
    default_max_pages: int = 100
    default_timeout_seconds: int = 120
    crawler_concurrency: int = 10
    crawler_delay: float = 0.25

    college_scorecard_api_key: Optional[str] = None

    model_config = {"env_file": ".env", "env_file_encoding": "utf-8"}


settings = Settings()
