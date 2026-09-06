"""Fixtures for the eval harness tests.

The ERP is real -- FastAPI's TestClient is an httpx.Client subclass, so it is
injected straight into ErpClient and requests go through real routing, real
serialisation and the real store, with no socket. The "model" is either the
rule baseline or a hand-built script, because model output is the one input
that cannot be made deterministic.
"""

import tempfile
from pathlib import Path

import pytest
from agent.erp_client import ErpClient
from agent.settings import AgentSettings
from evals.dataset import repo_root
from fastapi.testclient import TestClient
from mock_erp.app import create_app
from mock_erp.settings import Settings

ODATA_PREFIX = "/sap/opu/odata/sap/ZPROC_SRV"


@pytest.fixture(scope="session")
def root() -> Path:
    return repo_root()


@pytest.fixture
def erp_app(root):
    settings = Settings(
        erp_data_dir=root / "data" / "erp",
        db_path=Path(tempfile.mkdtemp()) / "approvals.sqlite3",
    )
    with TestClient(create_app(settings), base_url="http://erp" + ODATA_PREFIX) as client:
        yield client


@pytest.fixture
def erp_client(erp_app) -> ErpClient:
    return ErpClient(base_url=str(erp_app.base_url), client=erp_app)


@pytest.fixture
def settings() -> AgentSettings:
    return AgentSettings(model="baseline/rules", max_iterations=8, tracing=False)
