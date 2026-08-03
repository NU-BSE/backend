from fastapi import APIRouter, Depends, Request
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_db

router = APIRouter(tags=["health"])


@router.get("/healthz")
async def healthz(
    request: Request,
    db: AsyncSession = Depends(get_db),
) -> dict[str, object]:
    settings = request.app.state.settings
    checks: dict[str, bool] = {
        "config": bool(settings.jwt_secret),
        "llm_configured": bool(settings.llm_upstream_url) or settings.llm_mock,
    }
    try:
        await db.execute(text("SELECT 1"))
        checks["database"] = True
    except Exception:
        checks["database"] = False

    return {"ok": all(checks.values()), "checks": checks, "version": settings.app_version}
