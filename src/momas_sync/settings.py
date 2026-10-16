from dataclasses import dataclass
import math
import os
from pathlib import Path
from zoneinfo import ZoneInfo
from .client import BafangClient


@dataclass(frozen=True)
class Settings:
    data_dir: Path = Path("data/sync")
    app_timezone: str = "Europe/Zurich"
    endurain_enabled: bool = False
    interval: float = 1800
    timeout: float = 30
    max_pages: int = 1000
    page_size: int = 20
    retry_attempts: int = 4
    retry_base: float = 2
    retry_cap: float = 60
    health_stale: float = 3900

    def __post_init__(self):
        ZoneInfo(self.app_timezone)
        numbers = (self.interval, self.timeout, self.retry_base, self.retry_cap, self.health_stale)
        if any(not math.isfinite(n) or n <= 0 for n in numbers):
            raise ValueError("Invalid synchronization timing")
        if not 1 <= self.page_size <= 100 or not 1 <= self.max_pages <= 10000 or not 1 <= self.retry_attempts <= 10:
            raise ValueError("Invalid synchronization limits")
        if self.retry_base > self.retry_cap or self.health_stale < self.interval + 60:
            raise ValueError("Invalid retry or health timing")

    @classmethod
    def from_env(cls):
        return cls(
            data_dir=Path(os.getenv("DATA_DIR", "data/sync")),
            app_timezone=os.getenv("APP_TIMEZONE", "Europe/Zurich"),
            endurain_enabled=os.getenv('ENDURAIN_ENABLED','false').lower() == 'true',
            interval=float(os.getenv("SYNC_INTERVAL_SECONDS", "1800")),
            timeout=float(os.getenv("HTTP_TIMEOUT_SECONDS", "30")),
            max_pages=int(os.getenv("SYNC_MAX_PAGES", "1000")),
            page_size=int(os.getenv("BAFANG_PAGE_SIZE", "20")),
            retry_attempts=int(os.getenv("RETRY_ATTEMPTS", "4")),
            retry_base=float(os.getenv("RETRY_BASE_SECONDS", "2")),
            retry_cap=float(os.getenv("RETRY_CAP_SECONDS", "60")),
            health_stale=float(os.getenv("HEALTH_STALE_SECONDS", "3900")),
        )

    def client(self):
        # Read secrets on each cycle, allowing rotation without image rebuilds.
        from .auth import LoginSession, device_id, secret_value
        mode = os.getenv('BAFANG_AUTH_MODE', 'auto').lower()
        if mode not in ('auto', 'login', 'token'):
            raise ValueError('Invalid Bafang authentication mode')
        account = secret_value('BAFANG_ACCOUNT') if mode != 'token' else ''
        password = secret_value('BAFANG_PASSWORD', password=True) if mode != 'token' else ''
        if mode == 'login' or (mode == 'auto' and (account or password)):
            return LoginSession(self.data_dir, account, password,
                                device_id(self.data_dir), timeout=self.timeout)
        token_file = os.getenv("BAFANG_TOKEN_FILE")
        token = Path(token_file).read_text().strip() if token_file else os.getenv("BAFANG_TOKEN", "").strip()
        return BafangClient(token, os.getenv("BAFANG_DEVICE_ID", ""), timeout=self.timeout)

    def endurain_client(self):
        from .endurain import EndurainClient
        filename = os.getenv('ENDURAIN_API_KEY_FILE')
        key = Path(filename).read_text().strip() if filename else os.getenv('ENDURAIN_API_KEY','').strip()
        return EndurainClient(os.getenv('ENDURAIN_URL',''),key)
