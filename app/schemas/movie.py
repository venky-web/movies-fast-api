from datetime import date
from enum import StrEnum
from typing import Annotated, Literal

from pydantic import ConfigDict, Field
from pydantic.config import JsonDict

from app.schemas.base import CamelModel
from app.schemas.patch import PatchModel
from app.schemas.types import Text, UtcDatetime


class Genre(StrEnum):
    ACTION = "Action"
    ADVENTURE = "Adventure"
    ANIMATION = "Animation"
    COMEDY = "Comedy"
    DRAMA = "Drama"
    FANTASY = "Fantasy"
    HORROR = "Horror"
    ROMANCE = "Romance"
    SCI_FI = "Sci-Fi"
    THRILLER = "Thriller"


class MovieSort(StrEnum):
    TITLE = "title"
    RATING = "rating"
    RELEASE = "release"
    DURATION = "duration"


class Review(CamelModel):
    author: str
    stars: int = Field(ge=1, le=5)
    text: str


class Movie(CamelModel):
    """Public movie, same shape as the TypeScript `Movie`."""

    id: str
    title: str
    genres: list[Genre]
    duration_mins: int = Field(gt=0)
    rating: float = Field(ge=0, le=10)
    language: str
    poster_url: str
    synopsis: str
    release_date: date
    director: str
    cast: list[str]
    reviews: list[Review]


class MovieRecord(Movie):
    """Stored movie: the public fields plus internal ones that `response_model=Movie` filters out."""

    hue: int = Field(ge=0, le=359)
    source: Literal["seed", "admin"] = "seed"
    deleted_at: UtcDatetime | None = None
    uploaded_poster: str | None = None


# Ids end up in URLs and in upload file names, so no slashes, dots or other characters with a meaning there.
MovieId = Annotated[str, Field(pattern=r"^[a-z0-9][a-z0-9-]*$", max_length=80)]
GenreList = Annotated[list[Genre], Field(min_length=1)]
Duration = Annotated[int, Field(gt=0, le=1000)]
Rating = Annotated[float, Field(ge=0, le=10)]
Hue = Annotated[int, Field(ge=0, le=359)]

MOVIE_EXAMPLE: JsonDict = {
    "title": "The Glass Harbour",
    "genres": ["Drama"],
    "durationMins": 104,
    "rating": 7.4,
    "language": "English",
    "synopsis": "A ferry pilot finds a town that is on no chart.",
    "releaseDate": "2026-12-04",
    "director": "Lena Voss",
    "cast": ["Ines Marlow", "Pavel Dunn"],
}


class MovieReplace(CamelModel):
    """Body of `PUT`: the id comes from the path.

    `reviews` defaults to empty. An omitted `hue` keeps the current one, because the public GET does not
    show it and a GET-then-PUT round trip would otherwise lose it.
    """

    model_config = ConfigDict(json_schema_extra={"examples": [MOVIE_EXAMPLE]})

    title: Text
    genres: GenreList
    duration_mins: Duration
    rating: Rating
    language: Text
    synopsis: Text
    release_date: date
    director: Text
    cast: list[Text]
    reviews: list[Review] = Field(default_factory=list)
    hue: Hue | None = None


class MovieCreate(MovieReplace):
    id: MovieId | None = Field(None, description="Defaults to `m-{slug of the title}`")


class MovieUpdate(PatchModel):
    title: Text | None = None
    genres: GenreList | None = None
    duration_mins: Duration | None = None
    rating: Rating | None = None
    language: Text | None = None
    synopsis: Text | None = None
    release_date: date | None = None
    director: Text | None = None
    cast: list[Text] | None = None
    reviews: list[Review] | None = None
    hue: Hue | None = None
