import re
from collections.abc import Mapping

from fastapi import Request
from fastapi.responses import JSONResponse


class ODataError(Exception):
    def __init__(self, code: str, message: str, status: int = 400):
        self.code = code
        self.message = message
        self.status = status

def entity(payload: dict) -> dict:
    return {"d": payload}

def collection(rows: list[dict]) -> dict:
    return {"d": {"results":rows}}

def error_body(code: str, message: str) -> dict:
    return {"error": {"code":code, "message": {"lang":"en", "value":message},}}

async def odata_exception_handler(request: Request, exc: ODataError):
    return JSONResponse(
        status_code = exc.status,
        content = error_body(exc.code, exc.message),
    )

def parse_po_filter(raw: str | None) -> str:
    if raw is None:
        raise ODataError(
            code = "FILTER_REQUIRED",
            message = "A filter is required: PurchaseOrder eq '4500000047'",
            status = 400
        )
    shape_check = re.fullmatch(r"PurchaseOrder\s+eq\s+'([^']+)'", raw.strip())
    if not shape_check:
        raise ODataError(
            code = "UNSUPPORTED_FILTER",
            message = "The Query String shape doesn't match",
            status = 400
        )
    po_number = shape_check.group(1)
    if not re.fullmatch(r"(45\d{8})", po_number):
        raise ODataError(
            code = "INVALID_KEY",
            message = "The Query String PO Number is incorrect in length",
            status = 400
        )
    return str(po_number)

def reject_unsupported_options(params: Mapping[str, str], allowed:set[str]) -> None:
    for key in params:
        if key.startswith("$") and key not in allowed:
            raise ODataError(
                code="UNSUPPORTED_OPTION",
                message=f"{key} parameter is not allowed",
                status = 400
            )

