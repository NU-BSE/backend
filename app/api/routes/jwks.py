from fastapi import APIRouter, Depends

from app.api.deps import get_jit_signer
from app.services.jit import LocalFileEd25519Signer

router = APIRouter(tags=["keys"])


@router.get("/.well-known/jwks.json")
async def jwks(
    signer: LocalFileEd25519Signer = Depends(get_jit_signer),
) -> dict[str, list[dict[str, str]]]:
    return {"keys": [signer.public_jwk()]}
