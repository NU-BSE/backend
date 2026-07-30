from fastapi import APIRouter, Depends

from app.core.config import Settings, get_settings
from app.core.errors import ApiError
from app.core.security import create_access_token
from app.schemas.attestation import DevTokenRequest, DevTokenResponse

router = APIRouter(prefix="/dev", tags=["development"])


@router.post("/token", response_model=DevTokenResponse)
async def issue_dev_token(
    body: DevTokenRequest,
    settings: Settings = Depends(get_settings),
) -> DevTokenResponse:
    if (
        settings.environment != "development"
        or not settings.allow_dev_token_endpoint
    ):
        raise ApiError(404, "NOT_FOUND", "Endpoint is disabled")
    # if settings.is_production or not settings.allow_dev_token_endpoint:
    #     raise ApiError(404, "NOT_FOUND", "Endpoint is disabled")
    return DevTokenResponse(
        accessToken=create_access_token(body.user_id, settings),
    )
