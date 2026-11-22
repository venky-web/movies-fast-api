# Learning notes

## What was surprising, phase by phase

**0. Setup.** Installing proves nothing. Every package installed cleanly, and seven phases later one of them
turned out to be blocked at import by Windows Smart App Control. Import and run what you install.

**1. Catalogue.** Response models do a lot of work: returning a record with internal fields is safe, because
`response_model` filters them. Decimal becomes a JSON string unless told otherwise.

**2. Errors and dependencies.** The code after a dependency's `yield` runs after the response has been sent, and
a dependency that swallows an exception breaks the response. Both only show when measured.

**3. Bookings.** There is no race until something really awaits between check and write. And a lock doesn't
help when two different strings (`A2`, `A02`) mean the same seat.

**4. Admin and files.** The body is parsed before any dependency runs, so a key check in a dependency still
receives a 20 MB upload from a stranger. PATCH fields that are optional also accept null.

**5. Middleware and lifespan.** The 500 handler runs outside all middleware, app logs are invisible until
configured, and lifespan shutdown starts too late to end an open stream.

**6. Real-time.** The interesting bugs only exist on a real server: a close code lost to a race in 14 % of
shutdowns, a doubled heartbeat, an event that browsers ignore because it has no data.

**7. Database.** The textbook designs were the broken ones: commit in a dependency, modern SQLite transactions
and saving a list all lost or duplicated data under concurrency. Decide when a lock is released first.

**8. Polish.** 875 passing tests and 98 % coverage missed a 211-byte request that caused a 500 and a single
character that broke the catalogue for everybody. A fuzzer found both in a minute, and comparing with the real
fake backend found parity bugs that the ported tests never asked about.

## Versions (pinned 2026-11-21)

The plan assumed Python 3.15.2, but only **Python 3.13.0** is installed on this machine. 3.13 has wheels for
everything (including `pydantic-core` and `greenlet`), so the project uses 3.13 and pitfall 1 did not apply.

| Package | Version |
|---|---|
| Python | 3.13.0 |
| fastapi | 0.165.0 |
| fastapi-cli | 0.0.34 |
| starlette | 1.10.0 |
| pydantic | 2.14.4 |
| pydantic-core | 2.50.5 |
| pydantic-settings | 2.16.3 |
| uvicorn | 0.54.0 |
| httpx | 0.28.1 |
| anyio | 4.16.0 |
| SQLAlchemy | 2.1.9 |
| aiosqlite | 0.22.1 |
| alembic | 1.20.1 |
| greenlet | 3.5.6 |

## Phase 0

- `create_app()` is a factory; `app = create_app()` at module level is only for `fastapi dev`. Tests build a fresh
  app per test with a frozen clock and a seeded RNG.
- `Settings(_env_file=None, ...)` in tests keeps a local `.env` out of the test run.
- pytest needs `pythonpath = ["."]` so `import app` works from `tests/`.

## Phase 1

- **Query parameter models honour the alias generator** in FastAPI 0.165: `ShowtimeFilters.upcoming_only` shows up
  as `upcomingOnly` in OpenAPI. Because `CamelModel` also validates by name, `?upcoming_only=true` works too.
- Pydantic 2.11+ splits the old `populate_by_name` into `validate_by_name` / `validate_by_alias`, and adds
  `serialize_by_alias`. With `serialize_by_alias=True`, `model_dump()` already gives camelCase (pitfall 2 mostly
  disappears for hand-made payloads).
- **Response filtering:** routes return `MovieRecord` (with `hue`, `source`, ...) and `response_model=Movie` drops
  the internal fields. No separate mapping code is needed.
- A method called `list` inside a `Protocol` shadows the builtin for the rest of the class body, so
  `-> list[X]` on a later method fails at import time. Renamed to `list_all`.
- A field called `date` with type `date` is risky; `import datetime as dt` and `date: dt.date` avoids it.
- `Money` (Decimal) needs a `PlainSerializer(float)`; otherwise it is a JSON string.
- The poster route is a plain `def` (runs in the threadpool) but depends on an **async** dependency that loads the
  movie: dependencies and routes can mix sync and async freely.
- Reference fixtures were captured from the real TS seed code with the `esbuild` in `movies-mfe` (see
  `tests/fixtures/README.md`). Node on Windows ignores an outside `TZ`; setting `process.env.TZ` in the bundle works.
  The Python poster port produces byte-identical SVGs.
- Validation errors are still FastAPI's 422 here; Phase 2 turns them into 400 `ApiError`s.

## Phase 2

- **`yield` dependency timing (FastAPI 0.165):** the code after `yield` runs **after the response has been sent**,
  even for a `StreamingResponse` (observed order: before → handler/body → after-yield). So a `yield` dependency
  cannot change the response, and a DB session from one is still open while a streaming body is produced.
- An exception from the route is re-raised at the `yield`. If the dependency **swallows** it (no `raise`), the
  client gets a plain-text `500 Internal Server Error`, not a normal response. Always re-raise.
- `app.add_exception_handler(Exception, ...)` is handled by Starlette's `ServerErrorMiddleware`: the JSON body is
  sent, but the exception is still re-raised, so tests need `TestClient(app, raise_server_exceptions=False)`.
- Starlette's 404 (no route) and 405 (wrong method) arrive as `starlette.exceptions.HTTPException`, so the handler
  must be registered for Starlette's class, not FastAPI's subclass. The 405 keeps its `Allow` header.
- Pydantic error `loc` uses the **alias** (`upcomingOnly`), so validation messages are already camelCase.
- `PydanticCustomError("business_rule", "...")` gives our own error type, so the handler can show business-rule
  messages unchanged (used by the booking validators in Phase 3).
- `dependency_overrides[get_now]` affects request handling only: overriding it to 2030 made `upcomingOnly` return
  nothing, while the seed (built in lifespan) still used the frozen 2026 app clock.
- A **class** can be an override too: `dependency_overrides[get_repos] = StubRepos` (FastAPI calls `StubRepos()`).
- `Pagination` is a class dependency (`Annotated[Pagination, Depends()]`); it sits next to the `MovieFilters`
  query model on the same route without problems.

## Phase 3

- **Race-condition exercise (plan 5.4).** With in-memory repos the async repo methods never suspend, so there is
  no race at all until something real awaits between "check seats" and "write". `BookingContext.pause` (set to
  `asyncio.sleep(0)` in tests via `app.state.booking_pause`) stands in for that I/O. Results:
  - lock off + pause: two concurrent requests for the same seats → **201 and 201**, two bookings, seats sold twice
    (and the second write overwrote the first one's `bookedSeats`: a lost update);
  - lock on + pause: **201 and 409 `SEATS_TAKEN`**;
  - a concurrent cancel and create on the same showtime leave `bookedSeats` consistent, because cancel takes the
    same per-showtime lock.
  The seats are re-read *inside* the lock; checking a copy fetched before waiting for the lock would still race.
- **`ASGITransport` doesn't run lifespan** (pitfall 12, confirmed): without `app.router.lifespan_context(app)` the
  first request fails with `AttributeError: 'State' object has no attribute 'repos'`.
- `WrapValidator` around `EmailStr` trims the value first and swaps Pydantic's message for the contract's
  "A valid email is required". `EmailStr` normalises the domain: `Ava@Example.COM` → `Ava@example.com`.
- Raising `PydanticCustomError` directly in a `field_validator` gives a message without the "Value error, " prefix.
- `Body(embed=True, alias="userId")` with a `None` default: no body, `{}` and `{"userId": ""}` all reach the route
  as `None`/empty, so the route returns the contract's `BAD_REQUEST` instead of a `VALIDATION` error.
- A required `Query` would make a missing `userId` a `VALIDATION` error; declaring it optional and checking in the
  route keeps the fake backend's `BAD_REQUEST`.
- With a frozen clock many bookings share `createdAt`; "newest first" needs a tie-breaker (most recently added).
- Dependencies can assemble other dependencies into one object (`get_booking_context` → `BookingContext`), which
  keeps route signatures short. Phases 5–6 add the notifier/publisher there.

## Phase 4

### Bugs in earlier phases, found and fixed here

- **Double booking by spelling (Phase 3).** `is_valid_seat` accepted `A02`, `A2\n` and non-ASCII digits as seats
  different from a booked `A2`. In Python `\d` is any Unicode digit and `$` allows a trailing newline, so use
  `[0-9]` and `fullmatch`. The leading-zero variant was copied from the Angular fake backend, which still has it.
  A lock cannot help when two strings mean the same seat.
- **Stale checks (Phase 3).** Layout, movie and start time were checked on the showtime read *before* waiting
  for the lock. That was harmless until admins could edit showtimes. All checks now run inside the lock.
- **The 400 handler could itself fail with a 500 (Phase 2):** it echoes the input, and `jsonable_encoder` cannot
  handle non-UTF-8 bytes, `inf` or lone surrogates. See `json_safe`.
- **Validators must raise `ValueError`:** `OverflowError` (`astimezone`) and `InvalidOperation` (`quantize`)
  escape Pydantic and become 500s.

### FastAPI

- **Dependencies run after the body is read.** With only a router dependency, malformed JSON without a key was
  400 (not 401) and an anonymous 20 MB upload was received in full before its 401. `route_class=AdminRoute`
  checks first. The dependency stays for OpenAPI / Swagger "Authorize". Such a 401 skips `request_audit`.
- With a valid key an upload is still spooled to a temp file **before** the route runs: reading in chunks caps
  memory, not what the server accepts. *(Corrected in Phase 5: no middleware or proxy is needed. FastAPI 0.165
  has `max_body_size` on the app, a router or a single route. It is not configured yet.)*
- Pitfall 18: `APIKeyHeader(auto_error=True)` answers 401 `{"detail": "Not authenticated"}` in FastAPI 0.165. We
  use `auto_error=False` so that the body is an `ApiError`.
- `compare_digest` raises `TypeError` for non-ASCII strings, so compare bytes.
- PATCH: optional fields also accept an explicit `null`, and `exclude_unset` keeps it. `PatchModel` rejects it.
- `model_copy(update=...)` does not validate and leaves nested dicts as dicts; merge with `model_validate`.
- `StaticFiles(check_dir=False)` only skips the check at construction. The first request still needs the folder.
- `Page[T]` uses Python 3.12 generic syntax; each concrete type gets its own OpenAPI schema.
- An uploaded SVG is served as-is from our origin and can run script (stored XSS). Out of scope, see plan 5.13.

### Tooling

- Windows Application Control blocks coverage's C tracer on this machine, so `pytest --cov` takes about 4 minutes
  instead of under 1. Plain `pytest` is unaffected.

## Phase 5

### Measured, and different from what the plan assumed

- **Order after a request** (default `yield` scope): response sent -> background tasks -> dependency exit. With
  `Depends(dep, scope="function")`: dependency exit -> response sent -> background tasks. For a unit of work the
  default means a failed commit is a **201** for the client, and a task's own SQLite session gets "database is
  locked". Phase 7 must use function scope.
- **Uvicorn shutdown order:** close listeners -> wait for connections -> wait for background tasks -> lifespan
  shutdown. An event set in lifespan shutdown can never end a stream. `fastapi dev` hung on reload with one open
  stream until `core/shutdown.py` set the event from the exit signal. Stopping from code
  (`server.should_exit = True`) sends no signal, so it is not covered.
- **The `Exception` handler runs in `ServerErrorMiddleware`, outside all user middleware:** its 500 had no CORS
  and no request id headers, and the context var was already reset. `RequestContextMiddleware` sends the 500 and
  re-raises; the handler only logs.
- **`app.*` INFO logs were never printed** on a real server: uvicorn configures only its own loggers. The Phase 2
  audit log worked in `caplog` only.

### Middleware

- The middleware added last runs first. CORS answers preflights itself, so those are never simulated and carry
  no request id. A refused preflight is plain text (`Disallowed CORS origin`), not an `ApiError`.
- Pitfall 18: Starlette 1.10's GZip skips `text/event-stream` (tested). Phase 6 still checks with `curl -N`.
- Pure ASGI middleware shares its context with the route and its background tasks, so email log lines have the
  request id.
- `request_audit` now includes background-task time (it exits after them) and excludes simulated latency. It does
  not run for a simulated 503, which never reaches the route.
- Uvicorn's access log is left as it is, so the request id is on `app.*` lines only.

### Lifespan state exercise (done in a throwaway copy)

Replacing `app.state.repos` with `yield {"repos": ...}` / `request.state.repos`:
- `TestClient` tests still pass (it runs the lifespan protocol, which copies the state into each request);
- `ASGITransport` + `lifespan_context` tests fail (nothing copies the yielded state);
- reset breaks, because requests keep the startup store after `app.state.repos` is swapped.
So `app.state` stays (plan 5.1).

### Background work

- A task takes a repos *factory* and checks that the booking still exists, so a reset between the response and
  the task leaves no orphan email.
- The refresh takes every stored showtime lock, because it rewrites `bookedSeats`. It generates from the full
  seed list, deleted movies included, so screen numbers don't shift.

### Windows

- Testing `fastapi dev` reload needs a real console (uvicorn sends Ctrl+C to the console group). With redirected
  output in an automated run it looks like a hang.

## Phase 6

### Built-in SSE vs a hand-written StreamingResponse (gap D9)

Both were built and compared, on the wire and in Edge. Same headers, same events (only the field order differs).

| | Hand-written | `fastapi.sse` (used) |
|---|---|---|
| Encoding, multi-line data, field checks | ours to write | built in |
| Keep-alive | ours | every 15 s, not configurable |
| OpenAPI | a plain response | an event stream schema |
| Cancelling on disconnect | Starlette | Starlette plus a producer task |

- **An event without a `data:` line never fires in a browser**, so `demo-reset` sends `{}`.
- The built-in ping added to ours sent every default heartbeat **twice**. We only add heartbeats shorter than 15 s;
  a test fails if an upgrade changes FastAPI's interval.
- Subscribe in a request-scoped `yield` dependency, not inside the generator: it is ready before the first byte,
  and it is released even when the generator is left suspended.
- EventSource sent `Last-Event-ID` on reconnect without a preflight in Edge, so CORS needs no change.
- On uvicorn a disconnect is noticed in milliseconds without a write, so no `is_disconnected()` polling.

### WebSockets

- One writer task per socket and a bounded outbox: a slow client cannot block the hub, and two tasks never send
  at once.
- Events only say "look again". The hub sends differences from stored state, which fixes three races with one
  rule: late events, repeated events and a booking during connect.
- The hub is a subscriber like any other, so it can be dropped too. It then resubscribes and sends snapshots.
- `Request` dependencies (the audit) don't work on WebSocket routes, hence the separate `ws_router`. A dependency
  that awaits nothing should still be `async` there, or every connect takes a threadpool trip.
- HTTP middleware (CORS, request id, simulation) does not apply to WebSockets. In particular **any website can
  open one**: check `Origin` before adding holds.
- `/api/events` sends everybody's bookings (name, email) to every listener. Acceptable only because identity is
  mocked; it needs filtering as soon as there is real auth.

### Shutdown, measured on real uvicorn

- Uvicorn's 1012 beat our 1001 in 14 % of shutdowns (1 socket) and 4 % (50 sockets). `TestClient` can't show
  this. The exit signal is now handed on after the closes are written (at most 1 s), and then 18,000 of 18,000
  closes in the large runs were 1001.
- A connected client that stops reading blocks shutdown indefinitely, for SSE and WebSocket alike, because
  closing a transport waits for its buffer. With `--timeout-graceful-shutdown 2` it exits in 2-3 s. `fastapi dev`
  has no such option; press Ctrl+C twice.
- While the old process says goodbye during a reload it still answers requests, so "the server responds" does
  not mean "the new code is running".

## Phase 7

### Windows Smart App Control (found here, missed in Phase 0)

- SQLAlchemy's compiled wheel was blocked at import. Phase 0 only installed it. The official `py3-none-any` wheel
  of the same version has no binaries and works (README). `greenlet`, which async SQLAlchemy needs, is allowed.
  If that changes, async SQLAlchemy cannot run on this machine.
- Launchers generated by pip are judged per file: one fresh venv blocked `pip` and `alembic`, the next one `pip`,
  `fastapi` and `uvicorn`. `python -m <tool>` always worked.
- The setting was left on. Don't turn it off for this.

### The decision: locks are held until after the commit

Measured alternatives:

| Tempting design | What happened |
|---|---|
| Commit in the dependency, release locks in the service | the next request checks uncommitted data |
| Modern sqlite3 transactions (`autocommit=False`) | stale re-read inside the lock, then "database is locked" |
| Default identity map | stale re-read while the first object is still referenced |
| `showtimes.save` writes the seat list | two 201s for one seat, constraint never fired |
| Wait for a lock holding a connection | pool deadlock until the timeout |
| Leave writers to the busy timeout | "database is locked" in a burst of 700 |

The order at the end of a request is: commit, publish, release the locks, send the response, background tasks.
That needs `Depends(get_uow, scope="function")`.

### SQLAlchemy / SQLite / Alembic

- Relationships are `lazy="raise"`, so a forgotten `selectinload` fails loudly.
- `Numeric(10, 2)` round-tripped every cent up to 6000.00 exactly on SQLite.
- `from_attributes=True` can be passed to `model_validate`; properties like `ShowtimeRow.booked_seats` work too.
- With aiosqlite each statement costs several thread hops: batching took a 10-day refresh from 8.5 s to 1.35 s.
- `alembic check` doesn't compare `ondelete`, so a test also compares the migrated schema with `create_all`.
- Migrations use transactional DDL and don't import app types (`render_item`).

### Limits

- One process only: the locks, writer queue and event bus are in memory. The constraint still protects seats
  across processes, the other rules don't. Stop the server before `python -m app.seed --reset`.
- About 20 bookings/s here (pure-Python SQLAlchemy, SQLite). 400 at once all succeed. At 700 at once about 3 %
  wait over 30 s for a pooled connection and get a 500, and an email can be lost (it is logged). A transactional
  outbox (the email row written in the booking's transaction) would fix the latter.
- Loading every showtime (refresh, reset, sample bookings) keeps the event loop busy for part of a second.
- PostgreSQL is untested. The design assumes READ COMMITTED, which is its default.

### Tooling (corrects the Phase 4 note)

- `pytest --cov` no longer needs coverage's blocked C tracer: `core = "sysmon"` uses Python's own monitoring.
  Both backends (875 tests) take about 4 minutes with coverage. `-m "not sql"` is the quick loop.

## Phase 8

### Found by fuzzing every operation (about 13,300 requests)

- A body nested about 100 levels deep (211 bytes) was a 500: the error response echoes the input and the
  serializer has a depth limit. The echo is now cut at 16 levels.
- A plain `str` field accepts a lone surrogate. It was stored, and then every response containing it failed.
  All request models derive from `RequestModel`, which rejects it.
- `astimezone` overflows at the edges of the calendar. `UtcDatetime` keeps a year of slack.

### Found by comparing with the real fake backend (103 scenarios)

- `EmailStr` accepts `Ava <ava@example.com>` and silently keeps the address.
- JavaScript's `trim()` and `.length` are not Python's `strip()` and `len()`.
- The remaining differences are the Deviations list in the README.

### OpenAPI

- `responses=` can't remove the automatic 422. That takes a custom `app.openapi()`, which must work on a copy:
  the generated schema contains the very dicts from the routers' `responses=`, shared by every app.
- FastAPI documents extra responses in the route's own media type, so the event stream's 400 claimed to be
  `text/event-stream`.
- `PlainSerializer(return_type=str)` loses `format: date-time` (use `WithJsonSchema`). `X | None` says "nullable"
  for fields that can only be left out (use `X | SkipJsonSchema[None]`). Query parameters have the same
  problem (Swagger shows `string | (string | null)`), so `openapi()` strips null from them.
- `generate_unique_id_function` must be on the app before routers are included. It also names the generated
  `Body_...` schemas.
- `deprecated=True` is shown on `GET /api/movie-list`, an alias that exists only to demonstrate it.
- The conformance test triggers every documented (operation, status, code), which caught a documented error
  that could not happen.
- An example with a date is a time bomb: the showtime example would have turned invalid on 15 January 2027.

### Test hygiene

- `Settings(_env_file=None)` still reads environment variables, so the README's `$env:STORAGE_BACKEND` broke the
  tests. They now clear those variables.
- The Phase 3 pause hook is gone from the app. The race tests patch the repository instead.

### A correction to Phase 7

"The README works as written" was tested with `powershell -ExecutionPolicy RemoteSigned`, which hid that
`Activate.ps1` is blocked under Windows' default policy. Test setup steps with no overrides.
