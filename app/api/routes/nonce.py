from fastapi import APIRouter, Depends, Request

from app.api.deps import get_nonce_store
from app.core.security import CurrentUser, get_current_user
from app.schemas.attestation import NonceRequest, NonceResponse
from app.services.nonce import RedisNonceStore

router = APIRouter(prefix="/attest", tags=["attestation"])


@router.post("/nonce", response_model=NonceResponse)
async def issue_nonce(
    body: NonceRequest,
    request: Request,
    user: CurrentUser = Depends(get_current_user),
    store: RedisNonceStore = Depends(get_nonce_store),
) -> NonceResponse:
    record = await store.issue(
        user_id=user.user_id,
        client_ts=body.client_ts,
        client_ip=request.client.host if request.client else "",
        user_agent=request.headers.get("user-agent", ""),
    )
    return NonceResponse(nonce=record.nonce, serverIssuedAt=record.server_issued_at)
