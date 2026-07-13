from fastapi import HTTPException, Security
from fastapi.security import APIKeyHeader

import config

_api_key_header = APIKeyHeader(name="X-API-Key", auto_error=False)


def require_api_key(provided_key: str | None = Security(_api_key_header)) -> None:
    if provided_key != config.API_KEY:
        raise HTTPException(status_code=401, detail="Missing or invalid X-API-Key header")
