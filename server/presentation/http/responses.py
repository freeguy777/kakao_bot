from __future__ import annotations

from typing import Any

from fastapi.responses import JSONResponse

from server.core.contracts import build_standard_response

JSON_MEDIA_TYPE = "application/json; charset=utf-8"


def build_json_response(*, status_code: int = 200, **payload: Any) -> JSONResponse:
    return JSONResponse(status_code=status_code, content=build_standard_response(**payload), media_type=JSON_MEDIA_TYPE)


def build_payload_response(payload: dict[str, Any], *, status_code: int = 200) -> JSONResponse:
    return JSONResponse(status_code=status_code, content=payload, media_type=JSON_MEDIA_TYPE)
