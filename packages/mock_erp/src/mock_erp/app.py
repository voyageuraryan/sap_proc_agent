from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager

from fastapi import Depends, FastAPI

from mock_erp.api import get_store, router
from mock_erp.db import open_db
from mock_erp.odata import ODataError, odata_exception_handler
from mock_erp.repository import ProposalError, ProposalRepository
from mock_erp.settings import Settings, get_settings
from mock_erp.store import load_store
from mock_erp.write_api import agent_router, human_router, proposal_exception_handler


def create_app(settings: Settings | None = None) -> FastAPI:

    if settings is None:
        settings = get_settings()

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncGenerator[None]:
        app.state.store = load_store(settings.erp_data_dir)
        conn = open_db(settings.db_path)
        app.state.repository = ProposalRepository(conn, app.state.store)
        yield
        conn.close()

    app = FastAPI(
        exception_handlers={
            ODataError: odata_exception_handler,
            ProposalError: proposal_exception_handler,
        },
        lifespan=lifespan,
    )
    app.include_router(router)
    app.include_router(human_router)
    app.include_router(agent_router)

    @app.get("/healthz")
    async def healthz(store=Depends(get_store)) -> dict:
        res = {}
        res["vendors"] = len(store.vendors)
        res["materials"] = len(store.materials)
        res["plants"] = len(store.plants)
        res["purchase_orders"] = len(store.purchase_orders)
        res["goods_receipts"] = sum(len(v) for v in store.grs_by_po.values())
        res["invoices"] = len(store.invoices)

        return res

    return app


app = create_app(get_settings())
