from __future__ import annotations

import os
from dataclasses import dataclass, field
from functools import lru_cache

from dotenv import load_dotenv

load_dotenv()


def _env_bool(name: str, default: bool) -> bool:
    value = os.getenv(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


def _env_int(name: str, default: int) -> int:
    value = os.getenv(name)
    if value is None or not value.strip():
        return default
    return int(value)


def _env_csv(name: str, default: tuple[str, ...]) -> tuple[str, ...]:
    value = os.getenv(name)
    if value is None:
        return default
    items = tuple(item.strip() for item in value.split(",") if item.strip())
    return items or default


@dataclass(frozen=True)
class JiraSettings:
    base_url: str = field(default_factory=lambda: os.getenv("JIRA_BASE_URL", ""))
    email: str = field(default_factory=lambda: os.getenv("JIRA_EMAIL", ""))
    api_token: str = field(default_factory=lambda: os.getenv("JIRA_API_TOKEN", ""))
    project_key: str = field(default_factory=lambda: os.getenv("JIRA_PROJECT_KEY", "SOC"))
    issue_type: str = field(default_factory=lambda: os.getenv("JIRA_ISSUE_TYPE", "Task"))
    default_labels: tuple[str, ...] = field(
        default_factory=lambda: _env_csv("JIRA_DEFAULT_LABELS", ("soc-alert",))
    )
    request_timeout: int = field(default_factory=lambda: _env_int("JIRA_REQUEST_TIMEOUT", 20))

    @property
    def api_url(self) -> str:
        return f"{self.base_url.rstrip('/')}/rest/api/3"


@dataclass(frozen=True)
class SoarSettings:
    host: str = field(default_factory=lambda: os.getenv("SOAR_HOST", ""))
    port: int = field(default_factory=lambda: _env_int("SOAR_PORT", 443))
    org: str = field(default_factory=lambda: os.getenv("SOAR_ORG", ""))
    api_key_id: str = field(default_factory=lambda: os.getenv("SOAR_API_KEY_ID", ""))
    api_key_secret: str = field(default_factory=lambda: os.getenv("SOAR_API_KEY_SECRET", ""))
    # CA bundle path, or "false" to disable certificate verification (self-signed lab instances)
    cafile: str = field(default_factory=lambda: os.getenv("SOAR_CAFILE", ""))
    jira_field_names: tuple[str, ...] = field(
        default_factory=lambda: _env_csv("SOAR_JIRA_FIELD_NAMES", ("jira_key", "jira_issue_key"))
    )

    @property
    def configured(self) -> bool:
        return all((self.host, self.org, self.api_key_id, self.api_key_secret))


@dataclass(frozen=True)
class MailSettings:
    imap_host: str = field(default_factory=lambda: os.getenv("MAIL_IMAP_HOST", ""))
    imap_port: int = field(default_factory=lambda: _env_int("MAIL_IMAP_PORT", 993))
    username: str = field(default_factory=lambda: os.getenv("MAIL_USERNAME", ""))
    password: str = field(default_factory=lambda: os.getenv("MAIL_PASSWORD", ""))
    # "password" for classic IMAP login, "oauth2" for XOAUTH2 (Exchange Online / Microsoft 365)
    auth_method: str = field(
        default_factory=lambda: os.getenv("MAIL_AUTH_METHOD", "password").lower()
    )
    oauth_tenant_id: str = field(default_factory=lambda: os.getenv("MAIL_OAUTH_TENANT_ID", ""))
    oauth_client_id: str = field(default_factory=lambda: os.getenv("MAIL_OAUTH_CLIENT_ID", ""))
    oauth_client_secret: str = field(
        default_factory=lambda: os.getenv("MAIL_OAUTH_CLIENT_SECRET", "")
    )
    oauth_refresh_token: str = field(
        default_factory=lambda: os.getenv("MAIL_OAUTH_REFRESH_TOKEN", "")
    )
    use_ssl: bool = field(default_factory=lambda: _env_bool("MAIL_USE_SSL", True))
    folder: str = field(default_factory=lambda: os.getenv("MAIL_FOLDER", "INBOX"))
    processed_folder: str = field(
        default_factory=lambda: os.getenv("MAIL_PROCESSED_FOLDER", "Processed")
    )
    jira_key_pattern: str = field(
        default_factory=lambda: os.getenv("MAIL_JIRA_KEY_PATTERN", r"[A-Z][A-Z0-9]+-\d+")
    )
    closure_keywords: tuple[str, ...] = field(
        default_factory=lambda: _env_csv("MAIL_CLOSURE_KEYWORDS", ("closed", "resolved"))
    )
    notification_senders: tuple[str, ...] = field(
        default_factory=lambda: _env_csv("MAIL_NOTIFICATION_SENDERS", tuple())
    )

    @property
    def configured(self) -> bool:
        if self.auth_method == "oauth2":
            return all(
                (
                    self.imap_host,
                    self.username,
                    self.oauth_tenant_id,
                    self.oauth_client_id,
                    self.oauth_client_secret,
                    self.oauth_refresh_token,
                )
            )
        return all((self.imap_host, self.username, self.password))


@dataclass(frozen=True)
class PollingSettings:
    interval_seconds: int = field(default_factory=lambda: _env_int("POLL_INTERVAL_SECONDS", 120))
    # Empty means: every issue of JIRA_PROJECT_KEY, most recently updated first
    jira_jql_filter: str = field(default_factory=lambda: os.getenv("JIRA_JQL_FILTER", ""))
    soar_poll_enabled: bool = field(default_factory=lambda: _env_bool("POLL_SOAR_ENABLED", True))
    mail_poll_enabled: bool = field(default_factory=lambda: _env_bool("POLL_MAIL_ENABLED", True))
    max_results: int = field(default_factory=lambda: _env_int("POLL_MAX_RESULTS", 50))


@dataclass(frozen=True)
class SyncSettings:
    master_system: str = field(default_factory=lambda: os.getenv("MASTER_SYSTEM", "jira"))
    closed_status_names: tuple[str, ...] = field(
        default_factory=lambda: _env_csv("CLOSED_STATUS_NAMES", ("Done", "Closed", "Resolved"))
    )
    status_mapping: dict[str, str] = None  # type: ignore[assignment]
    reverse_status_mapping: dict[str, str] = None  # type: ignore[assignment]

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "status_mapping",
            {
                "To Do": "New",
                "In Progress": "Active",
                "In Review": "Active",
                "Waiting for Customer": "Pending",
                "Done": "Closed",
                "Closed": "Closed",
                "Resolved": "Closed",
            },
        )
        object.__setattr__(
            self,
            "reverse_status_mapping",
            {
                "Closed": "Done",
                "Active": "In Progress",
                "New": "To Do",
            },
        )


@dataclass(frozen=True)
class RetrySettings:
    max_attempts: int = field(default_factory=lambda: _env_int("RETRY_MAX_ATTEMPTS", 3))
    backoff_base: int = field(default_factory=lambda: _env_int("RETRY_BACKOFF_BASE", 5))
    backoff_max: int = field(default_factory=lambda: _env_int("RETRY_BACKOFF_MAX", 60))


@dataclass(frozen=True)
class DatabaseSettings:
    url: str = field(
        default_factory=lambda: os.getenv("DATABASE_URL", "sqlite:///data/sync_engine.db")
    )


@dataclass(frozen=True)
class CacheSettings:
    enabled: bool = field(default_factory=lambda: _env_bool("CACHE_ENABLED", True))
    max_db_bytes: int = field(
        default_factory=lambda: _env_int("CACHE_MAX_DB_BYTES", 120 * 1024 * 1024)
    )
    max_seen_rows: int = field(default_factory=lambda: _env_int("CACHE_MAX_SEEN_ROWS", 250000))
    prune_batch_size: int = field(default_factory=lambda: _env_int("CACHE_PRUNE_BATCH_SIZE", 2000))
    prune_every_n_writes: int = field(
        default_factory=lambda: _env_int("CACHE_PRUNE_EVERY_N_WRITES", 500)
    )


@dataclass(frozen=True)
class LoggingSettings:
    level: str = field(default_factory=lambda: os.getenv("LOG_LEVEL", "INFO"))
    dir_name: str = field(default_factory=lambda: os.getenv("LOG_DIR", "logs"))
    file: str = field(default_factory=lambda: os.getenv("LOG_FILE", "sync_engine.log"))
    backup_days: int = field(default_factory=lambda: _env_int("LOG_BACKUP_DAYS", 30))
    max_folder_bytes: int = field(
        default_factory=lambda: _env_int("LOG_MAX_FOLDER_BYTES", 2 * 1024 * 1024 * 1024)
    )
    truncate_length: int = field(default_factory=lambda: _env_int("LOG_TRUNCATE_LENGTH", 1500))
    prune_check_interval_seconds: int = field(
        default_factory=lambda: _env_int("LOG_PRUNE_CHECK_INTERVAL_SECONDS", 300)
    )


@dataclass(frozen=True)
class RuntimeSettings:
    mode: str = field(default_factory=lambda: os.getenv("APP_MODE", "both"))
    host: str = field(default_factory=lambda: os.getenv("APP_HOST", "0.0.0.0"))
    port: int = field(default_factory=lambda: _env_int("APP_PORT", 8080))
    # When set, POST endpoints require "Authorization: Bearer <token>"
    api_token: str = field(default_factory=lambda: os.getenv("APP_API_TOKEN", ""))


@dataclass(frozen=True)
class AppConfig:
    jira: JiraSettings
    soar: SoarSettings
    mail: MailSettings
    polling: PollingSettings
    sync: SyncSettings
    retry: RetrySettings
    database: DatabaseSettings
    cache: CacheSettings
    logging: LoggingSettings
    runtime: RuntimeSettings


@lru_cache(maxsize=1)
def get_settings() -> AppConfig:
    return AppConfig(
        jira=JiraSettings(),
        soar=SoarSettings(),
        mail=MailSettings(),
        polling=PollingSettings(),
        sync=SyncSettings(),
        retry=RetrySettings(),
        database=DatabaseSettings(),
        cache=CacheSettings(),
        logging=LoggingSettings(),
        runtime=RuntimeSettings(),
    )
