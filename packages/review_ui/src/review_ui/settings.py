"""Configuration for the review UI.

The reviewer identity lives here, and it is NOT authentication. See
decisions.md: this app has no login, and the identity it records is whatever
the form said. That is a deliberate, documented gap with a named place to
attach SSO, not an oversight dressed up as a feature.
"""

from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict

#: The OData service path the mock ERP mounts its read endpoints on. A
#: constant rather than a setting: it is part of the ERP's contract, not
#: something a deployment chooses.
ODATA_PREFIX = "/sap/opu/odata/sap/ZPROC_SRV"

#: Where the approval routes live. Deliberately NOT under ODATA_PREFIX -- that
#: separation is the gate, and it is visible in the routing table.
APPROVAL_PREFIX = "/approval"


class ReviewSettings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="REVIEW_", extra="forbid")

    #: The ERP's ROOT, not its OData prefix: this app needs both the read
    #: endpoints and the approval routes, which sit on different mounts.
    erp_base_url: str = "http://localhost:8000"

    request_timeout: float = 10.0

    #: Pre-fills the reviewer field. A convenience for a demo, nothing more.
    default_reviewer: str = "ap.supervisor@example.com"

    #: How many rows the queue shows before it stops.
    page_size: int = 50


@lru_cache
def get_settings() -> ReviewSettings:
    return ReviewSettings()
