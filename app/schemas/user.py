from app.schemas.base import CamelModel


class User(CamelModel):
    id: str
    name: str
    email: str
    avatar_color: str
