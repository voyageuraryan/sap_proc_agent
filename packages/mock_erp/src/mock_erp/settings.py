"""Runtime configuration for the mock ERP service.

One setting, read from the environment. Not a module constant, because in Step 6
the container mounts the data somewhere other than ./data/erp -- and a hardcoded
relative path is the bug that made the generator only runnable from the repo
root back in Step 2.
"""

from functools import lru_cache
from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="MOCK_ERP_", extra="forbid")

    # Only the served ERP documents. The service has no setting -- and no code
    # path -- pointing at data/labels/, which is what keeps the ground-truth
    # labels structurally unreachable rather than merely un-read.
    erp_data_dir: Path = Path("data/erp")


@lru_cache
def get_settings() -> Settings:
    """Cached so the environment is read once, and so FastAPI can override it."""
    return Settings()
