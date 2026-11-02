"""Authenticated, bounded client error codes; no free text or personal payload."""
from typing import Literal
from fastapi import APIRouter, Header, Request
from pydantic import BaseModel, ConfigDict, Field
from auth_api.auth import _user_from_token
from rate_limit import enforce_rate_limit
from server_logging import log_event

router = APIRouter(prefix='/api/v1/diagnostics', tags=['diagnostics'])


class PortalDiagnostic(BaseModel):
    model_config = ConfigDict(extra='forbid')
    operation: Literal['requests_list']
    code: Literal['requests_empty_response', 'requests_unrecognized_page',
                  'portal_session_expired', 'portal_network_error', 'portal_http_error',
                  'portal_csrf_rejected', 'requests_access_challenge']
    platform: Literal['android', 'web', 'other']
    status: int | None = Field(default=None, ge=100, le=599)
    version: str | None = Field(default=None, max_length=64, pattern=r'^\d+\.\d+\.\d+(?:-[A-Za-z0-9.-]+)?$')


@router.post('/portal', status_code=204)
async def portal_diagnostic(data: PortalDiagnostic, request: Request,
                            x_unio_token: str | None = Header(default=None)):
    user = await _user_from_token(x_unio_token)
    await enforce_rate_limit(key=f'portal-diagnostic:{user["id"]}', limit=6, window_seconds=60)
    log_event('portal_client_failure', level=30,
              request_id=request.scope.get('unio.request_id'),
              **data.model_dump(exclude_none=True))
