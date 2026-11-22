"""`/docs` tells the truth.

Static checks of the schema, and a conformance matrix: every documented (operation, status, code) is triggered
for real, on both backends, and the response is validated against its documented schema.
"""

import re
import time
from collections.abc import Callable, Iterator
from concurrent.futures import ThreadPoolExecutor
from functools import cached_property
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from httpx import Response

from app.api.openapi import TAGS
from app.core.errors import ERROR_DOCS, ApiError, ApiErrorCode
from app.main import create_app
from app.schemas import booking, demo, movie, showtime
from app.schemas.demo import DemoSettings
from app.schemas.events import DemoReset
from tests.conftest import ADMIN_KEY, TEST_NOW
from tests.helpers import booking_request, first_future, first_past, free_seats, showtimes
from tests.schema_check import problems

JSON = "application/json"
PNG = b"\x89PNG\r\n\x1a\n" + b"fake image data"
UNREADABLE = b'{"x": "\xff"}'  # not UTF-8


def operations(spec: dict[str, Any]) -> Iterator[tuple[str, str, dict[str, Any]]]:
    for path, item in spec["paths"].items():
        for method, operation in item.items():
            yield method.upper(), path, operation


def documented(spec: dict[str, Any]) -> set[tuple[str, int, str | None]]:
    """Every (operation id, status, error code) in the schema. The code is None for a success."""
    found: set[tuple[str, int, str | None]] = set()
    for _, _, operation in operations(spec):
        for status, response in operation["responses"].items():
            if int(status) < 400:
                found.add((operation["operationId"], int(status), None))
            else:
                for code in response["content"][JSON]["examples"]:
                    found.add((operation["operationId"], int(status), code))
    return found


@pytest.fixture
def spec(client: TestClient) -> dict[str, Any]:
    return client.get("/openapi.json").json()


@pytest.mark.backends("memory")
class TestSchema:
    def test_no_operation_documents_422(self, spec):
        assert not [path for _, path, op in operations(spec) if "422" in op["responses"]]
        assert not {"HTTPValidationError", "ValidationError"} & spec["components"]["schemas"].keys()

    def test_invalid_input_is_documented_as_400_where_it_is_possible(self, spec):
        for method, path, op in operations(spec):
            takes_input = "requestBody" in op or any(p["in"] != "path" for p in op.get("parameters", []))
            if op["operationId"] == "bookings-get_booking":
                takes_input = False  # its only input is userId, a free string: it can be missing, not invalid
            examples = op["responses"].get("400", {}).get("content", {}).get(JSON, {}).get("examples", {})
            assert ("VALIDATION" in examples) == takes_input, f"{method} {path}"

    def test_no_parameter_is_nullable_because_a_url_cant_send_null(self, spec):
        for method, path, op in operations(spec):
            for parameter in op.get("parameters", []):
                assert "'null'" not in str(parameter["schema"]), f"{method} {path} {parameter['name']}"

    def test_every_operation_lists_json_api_error_responses(self, spec):
        for method, path, op in operations(spec):
            errors = {status: r for status, r in op["responses"].items() if int(status) >= 400}
            assert "500" in errors, f"{method} {path}"
            for status, response in errors.items():
                assert list(response["content"]) == [JSON], f"{method} {path} {status}"
                assert response["content"][JSON]["schema"] == {"$ref": "#/components/schemas/ApiError"}
                assert response["content"][JSON]["examples"], f"{method} {path} {status}"

    def test_every_operation_is_described(self, spec):
        for method, path, op in operations(spec):
            where = f"{method} {path}"
            assert op.get("summary") and op.get("description"), where
            assert len(op.get("tags", [])) == 1, where
            for status, response in op["responses"].items():
                assert response["description"] not in ("", "Successful Response"), f"{where} {status}"
            for parameter in op.get("parameters", []):
                assert parameter.get("description"), f"{where} {parameter['name']}"

    def test_operation_ids_are_unique_and_readable(self, spec):
        ids = [op["operationId"] for _, _, op in operations(spec)]

        assert len(ids) == len(set(ids))
        assert all(re.fullmatch(r"[a-z]+-[a-z_]+", operation_id) for operation_id in ids)

    def test_every_tag_is_described(self, spec):
        used = {op["tags"][0] for _, _, op in operations(spec)}

        assert spec["tags"] == TAGS
        assert used | {"meta"} == {tag["name"] for tag in TAGS}
        assert all(tag["description"] for tag in TAGS)

    def test_the_realtime_protocols_are_in_the_tag_description(self, spec):
        [text] = [tag["description"] for tag in spec["tags"] if tag["name"] == "realtime"]

        assert "/ws/showtimes/{showtimeId}" in text
        for word in ("snapshot", "seats-booked", "seats-released", "1001", "1013", "4404", "4410"):
            assert word in text
        for event in ("booking-created", "booking-cancelled", "demo-reset"):
            assert event in text

    def test_exactly_the_old_alias_is_deprecated(self, spec):
        assert [path for _, path, op in operations(spec) if op.get("deprecated")] == ["/api/movie-list"]

    def test_the_contracts_required_user_id_is_marked_required(self, spec):
        for path in ("/api/bookings", "/api/bookings/{booking_id}"):
            [user_id] = [p for p in spec["paths"][path]["get"]["parameters"] if p["name"] == "userId"]
            assert user_id["required"] and user_id["schema"]["type"] == "string"
        cancel = spec["paths"]["/api/bookings/{booking_id}/cancel"]["patch"]["requestBody"]
        body = spec["components"]["schemas"][cancel["content"][JSON]["schema"]["$ref"].rsplit("/", 1)[1]]
        assert cancel["required"] and body["required"] == ["userId"]

    @pytest.mark.parametrize("name", ["MovieUpdate", "ShowtimeUpdate"])
    def test_patch_schemas_do_not_claim_to_accept_null(self, spec, name):
        for field in spec["components"]["schemas"][name]["properties"].values():
            assert {"type": "null"} not in field.get("anyOf", [])
            assert field.get("default", "unset") is not None

    @pytest.mark.parametrize(
        ("name", "field"),
        [("BookingDetails", "cancelledAt"), ("MovieRecord", "deletedAt"), ("MovieRecord", "uploadedPoster")],
    )
    def test_fields_that_are_left_out_are_not_nullable(self, spec, name, field):
        component = spec["components"]["schemas"][name]

        assert component["properties"][field]["type"] == "string"
        assert field not in component["required"]

    def test_datetimes_have_their_format(self, spec):
        schemas = spec["components"]["schemas"]
        for name, field in (
            ("Showtime", "startsAt"),
            ("BookingDetails", "createdAt"),
            ("OutboxEmail", "sentAt"),
        ):
            assert schemas[name]["properties"][field]["format"] == "date-time"

    def test_the_schema_is_cached_and_stable(self, app: FastAPI, settings, frozen_clock):
        first = app.openapi()

        assert app.openapi() is first
        assert app.openapi_schema is first
        assert create_app(settings, frozen_clock).openapi() == first  # fixing one app's schema left no trace

    def test_a_route_added_later_gets_fixed_too(self, app: FastAPI):
        app.openapi()

        @app.get("/api/extra/{number}")
        async def extra(number: int) -> int:
            return number

        operation = app.openapi()["paths"]["/api/extra/{number}"]["get"]
        assert operation["operationId"] == "untagged-extra"
        assert "422" not in operation["responses"] and "500" in operation["responses"]


@pytest.mark.backends("memory")
class TestExamples:
    MODELS = {
        model.__name__: model
        for model in (
            booking.BookingDetails,
            booking.CreateBookingRequest,
            demo.DemoSettings,
            movie.MovieCreate,
            movie.MovieReplace,
            movie.MovieUpdate,
            showtime.ShowtimeCreate,
            showtime.ShowtimeUpdate,
        )
    }

    def test_every_model_example_validates_against_its_model(self, spec):
        checked = set()
        for name, component in spec["components"]["schemas"].items():
            for example in component.get("examples", []):
                assert name in self.MODELS, f"add {name} to MODELS"
                self.MODELS[name].model_validate(example)
                assert problems(example, component, spec) == []
                checked.add(name)

        assert checked == self.MODELS.keys() - {"CreateBookingRequest"}  # that one has field examples

    def test_every_field_and_parameter_example_fits_its_schema(self, spec):
        for name, component in spec["components"]["schemas"].items():
            for field, schema in component.get("properties", {}).items():
                for example in schema.get("examples", []):
                    assert problems(example, schema, spec) == [], f"{name}.{field}"
        for method, path, op in operations(spec):
            for parameter in op.get("parameters", []):
                schema = parameter.get("schema", {})
                for example in schema.get("examples", []):
                    assert problems(example, schema, spec) == [], f"{method} {path} {parameter['name']}"

    def test_the_field_examples_make_a_valid_booking(self, spec):
        fields = spec["components"]["schemas"]["CreateBookingRequest"]["properties"]

        booking.CreateBookingRequest.model_validate({k: v["examples"][0] for k, v in fields.items()})

    def test_every_request_body_example_validates(self, spec):
        count = 0
        for _, _, op in operations(spec):
            for media in op.get("requestBody", {}).get("content", {}).values():
                for example in media.get("examples", {}).values():
                    self.MODELS[media["schema"]["$ref"].rsplit("/", 1)[1]].model_validate(example["value"])
                    count += 1

        assert count == 2

    def test_every_error_example_is_an_api_error_with_its_code_and_status(self, spec):
        for method, path, op in operations(spec):
            for status, response in op["responses"].items():
                if int(status) < 400:
                    continue
                for code, example in response["content"][JSON]["examples"].items():
                    error = ApiError.model_validate(example["value"])
                    assert error.code == code, f"{method} {path}"
                    assert ERROR_DOCS[error.code].status == int(status), f"{method} {path}"

    def test_every_code_is_in_the_catalogue(self):
        assert ERROR_DOCS.keys() == set(ApiErrorCode)

    def test_dated_examples_stay_in_the_future(self, spec):
        assert showtime.SHOWTIME_EXAMPLE["startsAt"] >= "2035"
        assert booking.BOOKING_EXAMPLE["showtime"]["startsAt"] >= "2035"


class World:
    """What a scenario needs to call one operation."""

    def __init__(self, app: FastAPI, client: TestClient, spec: dict[str, Any]) -> None:
        self.app = app
        self.client = client
        self.spec = spec
        self.operations = {op["operationId"]: (m, p, op) for m, p, op in operations(spec)}

    @cached_property
    def future(self):
        return first_future(showtimes(self.app), TEST_NOW)

    @cached_property
    def past(self):
        return first_past(showtimes(self.app), TEST_NOW)

    def call(
        self, operation_id: str, key: str | None = ADMIN_KEY, query: str = "", **kwargs: Any
    ) -> Response:
        method, path, _ = self.operations[operation_id]
        ids = {
            "movie_id": kwargs.pop("movie_id", "m-deep-field"),
            "showtime_id": kwargs.pop("showtime_id", "st-nope"),
            "booking_id": kwargs.pop("booking_id", "BK-NOPE00"),
            "user_id": kwargs.pop("user_id", "u-ava"),
        }
        headers = kwargs.pop("headers", {}) | ({"X-Admin-Key": key} if key else {})
        return self.client.request(method, path.format(**ids) + query, headers=headers, **kwargs)

    def example(self, name: str) -> dict[str, Any]:
        return self.spec["components"]["schemas"][name]["examples"][0]

    def booked(self, count: int = 1) -> dict[str, Any]:
        body = booking_request(self.future, free_seats(self.future)[:count])
        response = self.client.post("/api/bookings", json=body)
        assert response.status_code == 201, response.text
        return response.json()

    def cancelled(self) -> dict[str, Any]:
        made = self.booked()
        url = f"/api/bookings/{made['id']}/cancel"
        assert self.client.patch(url, json={"userId": "u-ava"}).status_code == 200
        return made

    def started_booking(self) -> dict[str, Any]:
        made = self.booked()
        self.app.state.clock.set(self.future.starts_at)
        return made

    def one_event(self) -> Response:
        bus = self.app.state.bus
        before = bus.subscriber_count
        with ThreadPoolExecutor(1) as pool:
            stream = pool.submit(self.call, "realtime-stream_events", query="?maxEvents=1")
            deadline = time.monotonic() + 5
            while bus.subscriber_count == before and time.monotonic() < deadline:
                time.sleep(0.01)
            self.client.portal.call(bus.publish, DemoReset())
            return stream.result(timeout=5)


Scenario = Callable[[World], Response]
SCENARIOS: dict[tuple[str, int, str | None], Scenario] = {}


def case(operation_id: str, status: int, code: str | None, run: Scenario) -> None:
    key = (operation_id, status, code)
    assert key not in SCENARIOS, key
    SCENARIOS[key] = run


def unreadable(operation_id: str) -> Scenario:
    return lambda w: w.call(operation_id, content=UNREADABLE, headers={"content-type": JSON})


def new_showtime(w: World, **changes: Any) -> Response:
    return w.call("admin-create_showtime", json=w.example("ShowtimeCreate") | changes)


def edit_showtime(w: World, body: dict[str, Any], showtime_id: str | None = None) -> Response:
    return w.call("admin-update_showtime", json=body, showtime_id=showtime_id or w.future.id)


def book(w: World, seats: list[str], **changes: Any) -> Response:
    return w.call("bookings-create_booking", json=booking_request(w.future, seats) | changes)


def cancel(w: World, booking_id: str, user_id: str = "u-ava") -> Response:
    return w.call("bookings-cancel_booking", json={"userId": user_id}, booking_id=booking_id)


def upload(w: World, data: bytes = PNG, movie_id: str = "m-deep-field") -> Response:
    return w.call("admin-upload_poster", files={"file": ("p.png", data, "image/png")}, movie_id=movie_id)


for op in ("movies-list_movies", "movies-list_movies_legacy"):
    case(op, 200, None, lambda w, op=op: w.call(op))
    case(op, 400, "VALIDATION", lambda w, op=op: w.call(op, query="?limit=0"))

case("movies-get_movie", 200, None, lambda w: w.call("movies-get_movie"))
case("movies-get_movie", 404, "NOT_FOUND", lambda w: w.call("movies-get_movie", movie_id="m-nope"))

op = "movies-list_movie_showtimes"
case(op, 200, None, lambda w: w.call("movies-list_movie_showtimes"))
case(op, 400, "VALIDATION", lambda w: w.call("movies-list_movie_showtimes", query="?date=tomorrow"))
case(op, 404, "NOT_FOUND", lambda w: w.call("movies-list_movie_showtimes", movie_id="m-nope"))

op = "showtimes-get_showtime"
case(op, 200, None, lambda w: w.call("showtimes-get_showtime", showtime_id=w.future.id))
case(op, 404, "NOT_FOUND", lambda w: w.call("showtimes-get_showtime"))

case("users-list_users", 200, None, lambda w: w.call("users-list_users"))
case("users-get_user", 200, None, lambda w: w.call("users-get_user"))
case("users-get_user", 404, "NOT_FOUND", lambda w: w.call("users-get_user", user_id="u-nope"))

case("posters-get_poster", 200, None, lambda w: w.call("posters-get_poster"))
case("posters-get_poster", 404, "NOT_FOUND", lambda w: w.call("posters-get_poster", movie_id="m-nope"))

op = "bookings-list_bookings"
case(op, 200, None, lambda w: w.cancelled() and w.call("bookings-list_bookings", query="?userId=u-ava"))
case(op, 400, "BAD_REQUEST", lambda w: w.call("bookings-list_bookings"))
case(op, 400, "VALIDATION", lambda w: w.call("bookings-list_bookings", query="?userId=u-ava&status=NO"))

op = "bookings-create_booking"
case(op, 201, None, lambda w: book(w, free_seats(w.future)[:2]))
case(op, 400, "VALIDATION", lambda w: book(w, []))
case(op, 400, "BAD_REQUEST", unreadable(op))
case(op, 404, "NOT_FOUND", lambda w: book(w, ["A1"], showtimeId="st-nope"))
case(op, 409, "SEATS_TAKEN", lambda w: book(w, w.future.booked_seats[:1]))
case(
    op,
    409,
    "SHOWTIME_STARTED",
    lambda w: book(w, ["A1"], showtimeId=w.past.id, movieId=w.past.movie_id),
)

op = "bookings-get_booking"
case(
    op,
    200,
    None,
    lambda w: w.call("bookings-get_booking", booking_id=w.booked()["id"], query="?userId=u-ava"),
)
case(op, 400, "BAD_REQUEST", lambda w: w.call("bookings-get_booking"))
case(op, 404, "NOT_FOUND", lambda w: w.call("bookings-get_booking", query="?userId=u-ava"))

op = "bookings-cancel_booking"
case(op, 200, None, lambda w: cancel(w, w.booked()["id"]))
case(op, 400, "BAD_REQUEST", lambda w: w.call("bookings-cancel_booking", json={}))
case(op, 400, "VALIDATION", lambda w: w.call("bookings-cancel_booking", json={"userId": 5}))
case(op, 404, "NOT_FOUND", lambda w: cancel(w, "BK-NOPE00"))
case(op, 409, "ALREADY_CANCELLED", lambda w: cancel(w, w.cancelled()["id"]))
case(op, 409, "SHOWTIME_STARTED", lambda w: cancel(w, w.started_booking()["id"]))

op = "admin-create_movie"
case(op, 201, None, lambda w: w.call("admin-create_movie", json=w.example("MovieCreate")))
case(op, 400, "VALIDATION", lambda w: w.call("admin-create_movie", json={}))
case(op, 400, "BAD_REQUEST", unreadable(op))
case(
    op,
    409,
    "ALREADY_EXISTS",
    lambda w: w.call("admin-create_movie", json=w.example("MovieCreate") | {"id": "m-deep-field"}),
)

for op, schema in (("admin-replace_movie", "MovieReplace"), ("admin-update_movie", "MovieUpdate")):
    case(op, 200, None, lambda w, op=op, schema=schema: w.call(op, json=w.example(schema)))
    case(op, 400, "VALIDATION", lambda w, op=op: w.call(op, json={"title": None}))
    case(op, 400, "BAD_REQUEST", unreadable(op))
    case(
        op,
        404,
        "NOT_FOUND",
        lambda w, op=op, schema=schema: w.call(op, json=w.example(schema), movie_id="m-nope"),
    )

op = "admin-delete_movie"
case(op, 204, None, lambda w: w.call("admin-delete_movie"))
case(op, 404, "NOT_FOUND", lambda w: w.call("admin-delete_movie", movie_id="m-nope"))
case(op, 409, "MOVIE_HAS_BOOKINGS", lambda w: w.call("admin-delete_movie", movie_id=w.booked()["movieId"]))

op = "admin-upload_poster"
case(op, 200, None, upload)
case(op, 400, "VALIDATION", lambda w: w.call("admin-upload_poster"))
case(
    op,
    400,
    "BAD_REQUEST",
    lambda w: w.call(
        "admin-upload_poster", content=b"garbage", headers={"content-type": "multipart/form-data; boundary=b"}
    ),
)
case(op, 404, "NOT_FOUND", lambda w: upload(w, movie_id="m-nope"))
case(op, 413, "PAYLOAD_TOO_LARGE", lambda w: upload(w, PNG + bytes(2 * 1024 * 1024)))
case(op, 415, "UNSUPPORTED_MEDIA_TYPE", lambda w: upload(w, b"plain text"))

op = "admin-create_showtime"
case(op, 201, None, new_showtime)
case(op, 400, "VALIDATION", lambda w: new_showtime(w, startsAt="2020-01-01T19:00:00Z"))
case(op, 400, "BAD_REQUEST", unreadable(op))
case(op, 404, "NOT_FOUND", lambda w: new_showtime(w, movieId="m-nope"))
case(op, 409, "ALREADY_EXISTS", lambda w: new_showtime(w).is_success and new_showtime(w))

op = "admin-update_showtime"
case(op, 200, None, lambda w: edit_showtime(w, w.example("ShowtimeUpdate")))
case(op, 400, "VALIDATION", lambda w: edit_showtime(w, {"price": -1}))
case(op, 400, "BAD_REQUEST", unreadable(op))
case(op, 404, "NOT_FOUND", lambda w: edit_showtime(w, {"price": 5}, "st-nope"))
case(op, 409, "LAYOUT_CONFLICT", lambda w: edit_showtime(w, {"rows": 1, "seatsPerRow": 1}))
case(
    op,
    409,
    "SHOWTIME_HAS_BOOKINGS",
    lambda w: edit_showtime(
        w, {"movieId": "m-neon-monsoon" if w.booked()["movieId"] == "m-deep-field" else "m-deep-field"}
    ),
)

op = "admin-delete_showtime"
case(op, 204, None, lambda w: w.call("admin-delete_showtime", showtime_id=w.future.id))
case(op, 404, "NOT_FOUND", lambda w: w.call("admin-delete_showtime"))
case(
    op,
    409,
    "SHOWTIME_HAS_BOOKINGS",
    lambda w: w.call("admin-delete_showtime", showtime_id=w.booked()["showtimeId"]),
)

op = "admin-list_bookings"
case(op, 200, None, lambda w: w.cancelled() and w.call("admin-list_bookings"))
case(op, 400, "VALIDATION", lambda w: w.call("admin-list_bookings", query="?limit=101"))

case("admin-get_demo_settings", 200, None, lambda w: w.call("admin-get_demo_settings"))

op = "admin-replace_demo_settings"
case(op, 200, None, lambda w: w.call("admin-replace_demo_settings", json=w.example("DemoSettings")))
case(op, 400, "VALIDATION", lambda w: w.call("admin-replace_demo_settings", json={}))
case(op, 400, "BAD_REQUEST", unreadable(op))

case("admin-reset_demo", 204, None, lambda w: w.call("admin-reset_demo"))

op = "admin-add_sample_bookings"
case(op, 200, None, lambda w: w.call("admin-add_sample_bookings", query="?userId=u-mia"))
case(op, 400, "VALIDATION", lambda w: w.call("admin-add_sample_bookings"))
case(op, 404, "NOT_FOUND", lambda w: w.call("admin-add_sample_bookings", query="?userId=u-nope"))

case("admin-list_outbox", 200, None, lambda w: w.cancelled() and w.call("admin-list_outbox"))

case("realtime-stream_events", 200, None, World.one_event)
case(
    "realtime-stream_events",
    400,
    "VALIDATION",
    lambda w: w.call("realtime-stream_events", query="?maxEvents=0"),
)

for op in sorted({key[0] for key in SCENARIOS if key[0].startswith("admin-")}):
    case(op, 401, "UNAUTHORIZED", lambda w, op=op: w.call(op, key=None))

# Documented on every operation by rule, and checked by rule in `TestGenericErrors`.
BY_RULE = {"INTERNAL", "SIMULATED_FAILURE"}


@pytest.mark.backends("memory")
def test_the_matrix_covers_exactly_what_is_documented(spec):
    assert SCENARIOS.keys() == {key for key in documented(spec) if key[2] not in BY_RULE}


@pytest.mark.parametrize(
    "key", list(SCENARIOS), ids=[f"{op}-{status}-{code or 'ok'}" for op, status, code in SCENARIOS]
)
def test_what_is_documented_really_happens(app, client, spec, key):
    operation_id, status, code = key
    world = World(app, client, spec)

    response = SCENARIOS[key](world)

    assert response.status_code == status, response.text
    documented_response = world.operations[operation_id][2]["responses"][str(status)]
    for header, definition in documented_response.get("headers", {}).items():
        assert header in response.headers, f"{header} is documented but missing"
        if "const" in definition["schema"]:
            assert response.headers[header] == definition["schema"]["const"]
    if status == 204:
        assert response.content == b""
        return
    media = response.headers["content-type"].split(";")[0]
    assert media in documented_response["content"]
    if media != JSON:
        return
    body = response.json()
    assert problems(body, documented_response["content"][media]["schema"], spec) == []
    if code:
        assert body["code"] == code
        example = documented_response["content"][media]["examples"][code]["value"]
        if code != "VALIDATION":  # its details depend on what was wrong
            assert body.get("details", {}).keys() == example.get("details", {}).keys()


class TestGenericErrors:
    @pytest.mark.backends("memory")
    def test_503_is_documented_exactly_where_the_simulation_applies(self, app, client, spec):
        app.state.demo_settings = DemoSettings(latency_ms=0, failure_rate=1)
        for method, path, operation in operations(spec):
            url = re.sub(r"\{\w+\}", "x", path) + ("?maxEvents=0" if path == "/api/events" else "")
            response = client.request(method, url)

            assert (response.status_code == 503) == ("503" in operation["responses"]), f"{method} {path}"

    def test_500_matches_its_documentation(self, app):
        spec = app.openapi()  # not the `spec` fixture: its client would already run this app's lifespan

        @app.get("/api/explode", tags=["meta"])
        async def explode() -> None:
            raise RuntimeError("boom")

        with TestClient(app, raise_server_exceptions=False) as client:
            response = client.get("/api/explode")

        documented_response = spec["paths"]["/api/users"]["get"]["responses"]["500"]
        assert response.status_code == 500
        assert problems(response.json(), documented_response["content"][JSON]["schema"], spec) == []
