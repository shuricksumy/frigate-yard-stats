from fastapi import HTTPException, Query, Security
from fastapi.security import APIKeyHeader

import config

_api_key_header = APIKeyHeader(name="X-API-Key", auto_error=False)


def require_api_key(provided_key: str | None = Security(_api_key_header)) -> None:
    if provided_key != config.API_KEY:
        raise HTTPException(status_code=401, detail="Missing or invalid X-API-Key header")


def require_api_key_header_or_query(
    provided_key: str | None = Security(_api_key_header),
    api_key: str | None = Query(None, description="Fallback for <img>/<video> tags, which can't send custom headers"),
) -> None:
    # Same shared secret, just accepted two ways -- the web UI's fetch() calls send the header
    # like every other endpoint, but a bare <video src="..."> or <img src="..."> element has no
    # way to attach an X-API-Key header, so this endpoint alone also accepts it as a query param.
    if provided_key == config.API_KEY or api_key == config.API_KEY:
        return
    raise HTTPException(status_code=401, detail="Missing or invalid API key (X-API-Key header or api_key query param)")
