"""Runtime configuration for the Newspapers.com scraper."""

from __future__ import annotations

from pathlib import Path
import os

from pydantic import BaseModel, Field
from pydantic_settings import BaseSettings, SettingsConfigDict


_Base: type[BaseModel] = BaseSettings  # type: ignore[assignment]


class Settings(_Base):
    newspapers_email: str | None = Field(
        default_factory=lambda: os.getenv("NEWSCOM_LOGIN_EMAIL"),
        alias="NEWSCOM_LOGIN_EMAIL",
    )
    newspapers_password: str | None = Field(
        default_factory=lambda: os.getenv("NEWSCOM_LOGIN_PASSWORD"),
        alias="NEWSCOM_LOGIN_PASSWORD",
    )

    data_dir: Path = Field(
        default_factory=lambda: Path(os.getenv("NEWSCOM_DATA_DIR", "./data")),
        alias="NEWSCOM_DATA_DIR",
    )
    chrome_app_name: str = Field(
        default_factory=lambda: os.getenv("NEWSCOM_CHROME_APP_NAME", "Google Chrome"),
        alias="NEWSCOM_CHROME_APP_NAME",
    )
    chrome_app_path: Path = Field(
        default_factory=lambda: Path(
            os.getenv("NEWSCOM_CHROME_APP_PATH", "/Applications/Google Chrome.app")
        ),
        alias="NEWSCOM_CHROME_APP_PATH",
    )
    chrome_debug_port: int = Field(
        default_factory=lambda: int(os.getenv("NEWSCOM_CHROME_DEBUG_PORT", "9223")),
        alias="NEWSCOM_CHROME_DEBUG_PORT",
    )
    chrome_profile_dir: Path = Field(
        default_factory=lambda: Path(
            os.getenv("NEWSCOM_CHROME_PROFILE_DIR", "./data/chrome_profile")
        ),
        alias="NEWSCOM_CHROME_PROFILE_DIR",
    )
    login_url: str = Field(
        default_factory=lambda: os.getenv(
            "NEWSCOM_LOGIN_URL", "https://www.newspapers.com/signin/"
        ),
        alias="NEWSCOM_LOGIN_URL",
    )
    home_url: str = Field(
        default_factory=lambda: os.getenv(
            "NEWSCOM_HOME_URL", "https://www.newspapers.com/"
        ),
        alias="NEWSCOM_HOME_URL",
    )
    page_load_seconds: float = Field(
        default_factory=lambda: float(os.getenv("NEWSCOM_PAGE_LOAD_SECONDS", "6")),
        alias="NEWSCOM_PAGE_LOAD_SECONDS",
    )
    sleep_between_downloads: float = Field(
        default_factory=lambda: float(
            os.getenv("NEWSCOM_SLEEP_BETWEEN_DOWNLOADS", "60")
        ),
        alias="NEWSCOM_SLEEP_BETWEEN_DOWNLOADS",
    )
    browser_start_timeout_seconds: float = Field(
        default_factory=lambda: float(
            os.getenv("NEWSCOM_BROWSER_START_TIMEOUT_SECONDS", "30")
        ),
        alias="NEWSCOM_BROWSER_START_TIMEOUT_SECONDS",
    )
    papers_search_wait_seconds: float = Field(
        default_factory=lambda: float(
            os.getenv("NEWSCOM_PAPERS_SEARCH_WAIT_SECONDS", "4")
        ),
        alias="NEWSCOM_PAPERS_SEARCH_WAIT_SECONDS",
    )
    login_poll_seconds: float = Field(
        default_factory=lambda: float(os.getenv("NEWSCOM_LOGIN_POLL_SECONDS", "5")),
        alias="NEWSCOM_LOGIN_POLL_SECONDS",
    )
    auth_env_file: Path = Field(
        default_factory=lambda: Path(os.getenv("NEWSCOM_AUTH_ENV_FILE", ".env.local")),
        alias="NEWSCOM_AUTH_ENV_FILE",
    )

    model_config: SettingsConfigDict = SettingsConfigDict(
        env_file=(".env", ".env.local"),
        extra="ignore",
    )

    @property
    def chrome_debug_base(self) -> str:
        return f"http://127.0.0.1:{self.chrome_debug_port}"
