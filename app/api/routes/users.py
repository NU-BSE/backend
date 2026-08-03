from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_current_user, get_db
from app.db import repositories as repo
from app.db.models import User
from app.schemas.users import UpdateUserRequest, UserResponse

router = APIRouter(prefix="/users", tags=["users"])


def _to_response(user: User) -> UserResponse:
    return UserResponse(
        user_id=user.user_id,
        email=user.email,
        name=user.name,
        created_at=user.created_at,
    )


@router.get("/me", response_model=UserResponse)
async def get_me(user: User = Depends(get_current_user)) -> UserResponse:
    return _to_response(user)


@router.patch("/me", response_model=UserResponse)
async def update_me(
    body: UpdateUserRequest,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> UserResponse:
    await repo.update_user_name(db, user.user_id, body.name)
    user.name = body.name
    return _to_response(user)
