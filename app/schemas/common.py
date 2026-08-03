from pydantic import BaseModel, ConfigDict
from pydantic.alias_generators import to_camel


class CamelModel(BaseModel):
    """Wire models speak camelCase to match the mobile client."""

    model_config = ConfigDict(alias_generator=to_camel, populate_by_name=True)
