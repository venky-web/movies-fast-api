from pydantic import BaseModel, ConfigDict
from pydantic.alias_generators import to_camel


class CamelModel(BaseModel):
    """Base for every API model: snake_case in Python, camelCase in JSON.

    - `validate_by_name` + `validate_by_alias`: accept both `user_id` and `userId` on input.
    - `serialize_by_alias`: `model_dump()` gives camelCase by default, so hand-made payloads (SSE, WebSocket)
      match the HTTP responses without remembering `by_alias=True`.
    """

    model_config = ConfigDict(
        alias_generator=to_camel,
        validate_by_name=True,
        validate_by_alias=True,
        serialize_by_alias=True,
    )


class Page[T](CamelModel):
    """Envelope of a paged list: `Page[BookingDetails]` gets its own schema in OpenAPI."""

    items: list[T]
    total: int
    limit: int
    offset: int
