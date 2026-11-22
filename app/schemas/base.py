from typing import Any

from pydantic import BaseModel, ConfigDict, field_validator
from pydantic.alias_generators import to_camel
from pydantic_core import PydanticCustomError


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


class RequestModel(CamelModel):
    """Base of everything a client sends.

    JSON can carry a lone surrogate (an escape like `\\ud800`), and a plain `str` field accepts it. It can't
    be encoded as UTF-8, so it used to be stored and then broke every response that contained it.
    """

    @field_validator("*")
    @classmethod
    def text_is_valid_unicode(cls, value: Any) -> Any:
        for item in value if isinstance(value, list) else [value]:
            if isinstance(item, str):
                try:
                    item.encode("utf-8")
                except UnicodeEncodeError:
                    # Not a ValueError: Pydantic would prefix the message with "Value error, ".
                    raise PydanticCustomError(
                        "invalid_unicode", "Text must be valid Unicode (no lone surrogates)"
                    ) from None
        return value


class Page[T](CamelModel):
    """Envelope of a paged list: `Page[BookingDetails]` gets its own schema in OpenAPI."""

    items: list[T]
    total: int
    limit: int
    offset: int
