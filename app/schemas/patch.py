from typing import Self

from pydantic import model_validator
from pydantic.alias_generators import to_camel

from app.core.errors import business_rule
from app.schemas.base import CamelModel


class PatchModel(CamelModel):
    """Base for PATCH bodies.

    Fields are optional so that they can be *left out*. That would also let `{"title": null}` through, and
    `exclude_unset` keeps an explicit null, so nulls are rejected here.
    """

    @model_validator(mode="after")
    def no_explicit_nulls(self) -> Self:
        for name in sorted(self.model_fields_set):
            if getattr(self, name) is None:
                raise business_rule(f"{to_camel(name)} cannot be null")
        return self
