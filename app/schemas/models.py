from app.schemas.common import CamelModel


class ModelFileResponse(CamelModel):
    name: str
    role: str
    bytes: int
    sha256: str
    url: str


class ModelBundleResponse(CamelModel):
    profile: str
    model: str
    total_bytes: int
    files: list[ModelFileResponse]


class ModelCatalogResponse(CamelModel):
    bundles: list[ModelBundleResponse]
    # False when the caller has no paid subscription. The catalogue is still
    # returned so the app can show the download size before paying; the bytes
    # themselves are refused.
    download_allowed: bool
