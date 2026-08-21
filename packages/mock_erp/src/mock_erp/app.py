from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager

from fastapi import Depends, FastAPI

from mock_erp.api import get_store, router
from mock_erp.odata import ODataError, odata_exception_handler
from mock_erp.settings import Settings, get_settings
from mock_erp.store import load_store


def create_app(settings: Settings | None = None) -> FastAPI:
    
    if settings is None:
        settings = get_settings()
        
    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncGenerator[None]:
        app.state.store = load_store(settings.erp_data_dir)
        yield
    
    app = FastAPI(exception_handlers={ODataError: odata_exception_handler}, lifespan=lifespan)
    app.include_router(router)
    
    @app.get("/healthz")
    async def healthz(store = Depends(get_store)) -> dict:
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


    