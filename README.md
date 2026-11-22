# Movies API

A FastAPI practice backend that rebuilds, and extends, the fake backend of the Movies MFE Angular app:
a catalogue, bookings without double booking, an admin area, live seat maps (WebSocket), an event stream (SSE),
and storage in memory or SQLite.

It is a **learning project, not for production**. [NOTES.md](NOTES.md) records what was learned in each phase.
References such as "plan 5.4" in comments point to design notes that are not part of this repository.

- [Run](#run)
- [Run on SQLite](#run-on-sqlite)
- [Test](#test)
- [Settings](#settings)
- [Endpoint tour](#endpoint-tour)
- [Deviations from the fake backend](#deviations-from-the-fake-backend)
- [Known limits](#known-limits)

## Run

Windows, PowerShell, Python 3.13:

```powershell
py -3.13 -m venv .venv
$env:PATH = "$PWD\.venv\Scripts;$env:PATH"
python -m pip install -e ".[dev,db]"
copy .env.example .env        # optional; the defaults work without it
python -m fastapi dev app/main.py
```

- The second line activates the venv for this terminal, so repeat it in every new one. `Activate.ps1` does the
  same, but Windows blocks scripts by default ("running scripts is disabled on this system").
- The commands use `python -m ...` on purpose. Smart App Control may block the launchers that pip generates
  (`pip.exe`, `fastapi.exe`, `alembic.exe`, ...), and which ones it blocks differs from one venv to the next.

Open <http://127.0.0.1:8000/docs> (Swagger UI, click *Authorize* and enter `dev-admin-key`) or
<http://127.0.0.1:8000/redoc>. Both pages load their scripts from a CDN, so they need internet access.

By default everything is in memory and is gone after a restart or reload.

## Run on SQLite

```powershell
python -m alembic upgrade head       # creates movies.db (DATABASE_URL)
$env:STORAGE_BACKEND = "sql"         # or put STORAGE_BACKEND=sql in .env
python -m fastapi dev app/main.py    # seeds an empty database, then keeps its data across restarts
```

- `python -m app.seed` seeds without starting the server.
- `python -m app.seed --reset` **deletes all data** and seeds again. Stop the server first.
- After a model change: `python -m alembic revision --autogenerate -m "what changed"`, review the file, then
  `python -m alembic upgrade head`.
- Both modes share the `uploads/` folder, so a reset in one also deletes the other's uploaded posters.

### "An Application Control policy has blocked this file"

Windows Smart App Control may refuse SQLAlchemy's compiled speedups, which SQL mode, Alembic and the tests need.
Leave Smart App Control on and install SQLAlchemy's official pure-Python wheel instead (same version, somewhat
slower):

```powershell
$dir = New-Item -ItemType Directory -Path (Join-Path $env:TEMP "sqlalchemy-pure-$(Get-Random)")
python -m pip download --no-deps --only-binary=:all: --platform any --dest $dir.FullName "sqlalchemy==2.1.9"
python -m pip install --force-reinstall --no-deps (Get-ChildItem $dir.FullName -Filter *.whl).FullName
```

## Test

```powershell
python -m pytest                        # both storage backends, several minutes
python -m pytest -m "not sql"           # quick loop: memory only
python -m pytest --cov=app --cov-report=term-missing --cov-fail-under=90
python -m ruff check . ; python -m ruff format --check . ; python -m mypy app
```

The tests ignore your `.env` and the settings in your shell.

## Settings

Environment variables, or a `.env` file in the folder you start from ([.env.example](.env.example)).

| Variable | Default | Meaning |
|---|---|---|
| `APP_ENV` | `dev` | `dev`, `test` or `prod`. Only a label, nothing depends on it |
| `ADMIN_API_KEY` | `dev-admin-key` | The `X-Admin-Key` of the admin routes |
| `STORAGE_BACKEND` | `memory` | `memory` or `sql` |
| `DATABASE_URL` | `sqlite+aiosqlite:///./movies.db` | Used in SQL mode, by Alembic and by `python -m app.seed` |
| `THEATER_TZ` | `Europe/London` | Time zone of showtime slots and calendar days |
| `DEMO_LATENCY_MS` | `0` | Starting latency of the simulation (0 to 5000) |
| `DEMO_FAILURE_RATE` | `0` | Starting failure rate of the simulation (0 to 1) |
| `SHOWTIME_REFRESH_SECONDS` | `3600` | How often to check whether the showtime window has to move |
| `SSE_HEARTBEAT_SECONDS` | `15` | Ping interval of the event stream. Above 15 has no effect |
| `SAMPLE_BOOKINGS_FOR` | unset | A user id that gets sample bookings when the data is seeded |
| `USE_SHOWTIME_LOCKS` | `true` | Leave it on. Turning it off is only for race-condition experiments |
| `UPLOADS_DIR` | `uploads` | Where uploaded posters are stored |
| `CORS_ORIGINS` | `["http://localhost:4200"]` | JSON list of allowed browser origins |

## Endpoint tour

Every endpoint, in an order that works. Start the server, open a **second** PowerShell in this folder and paste
the blocks from top to bottom. The tour uses `curl.exe`, because plain `curl` is an alias for
`Invoke-WebRequest` in Windows PowerShell. It creates its own movie and deletes it again.

```powershell
$env:PATH = "$PWD\.venv\Scripts;$env:PATH"
$api = "http://127.0.0.1:8000"
$json = "Content-Type: application/json"
$admin = "X-Admin-Key: dev-admin-key"
```

### Health and catalogue

```powershell
curl.exe -s "$api/health"
curl.exe -s -i "$api/api/movies?genre=Drama&genre=Comedy&sort=rating&limit=2"   # see X-Total-Count
curl.exe -s "$api/api/movie-list?limit=1"                                        # deprecated alias
curl.exe -s "$api/api/movies/m-deep-field"
curl.exe -s "$api/api/posters/m-deep-field.svg" -o "$env:TEMP\movies-api-poster.svg"
curl.exe -s "$api/api/users"
curl.exe -s "$api/api/users/u-ava"

$showtime = (curl.exe -s "$api/api/movies/m-deep-field/showtimes?upcomingOnly=true" | ConvertFrom-Json)[-1]
curl.exe -s "$api/api/showtimes/$($showtime.id)"
```

### Book and cancel

The JSON goes through the pipe (`--data-binary "@-"`), because Windows PowerShell strips quotes from arguments.

```powershell
$seats = 65..(64 + $showtime.rows) | ForEach-Object { $row = [char]$_; 1..$showtime.seatsPerRow | ForEach-Object { "$row$_" } }
$seat = $seats | Where-Object { $showtime.bookedSeats -notcontains $_ } | Select-Object -First 1
$body = @{ userId = "u-ava"; movieId = $showtime.movieId; showtimeId = $showtime.id; seats = @($seat); customerName = "Ava Patel"; email = "ava.patel@example.com" } | ConvertTo-Json -Compress
$booking = $body | curl.exe -s -X POST -H $json --data-binary "@-" "$api/api/bookings" | ConvertFrom-Json
"Booked $seat as $($booking.id)"

curl.exe -s "$api/api/bookings?userId=u-ava"
curl.exe -s "$api/api/bookings/$($booking.id)?userId=u-ava"
'{"userId":"u-ava"}' | curl.exe -s -X PATCH -H $json --data-binary "@-" "$api/api/bookings/$($booking.id)/cancel"
```

### Live updates

```powershell
python scripts/watch_seats.py "$($showtime.id)" --seconds 3    # WebSocket /ws/showtimes/{showtimeId}
curl.exe -s -N --max-time 3 "$api/api/events"                  # Server-Sent Events
```

To see changes arrive, run either one without its time limit in a third terminal, then book in this one.

### Admin: movies and showtimes

```powershell
$movie = "m-tour-$(Get-Random)"
$fields = @{ title = "The Tour"; genres = @("Drama"); durationMins = 95; rating = 7.1; language = "English"; synopsis = "A walk through every endpoint."; releaseDate = "2026-12-04"; director = "Lena Voss"; cast = @("Ines Marlow") }
($fields + @{ id = $movie }) | ConvertTo-Json -Compress | curl.exe -s -i -X POST -H $admin -H $json --data-binary "@-" "$api/api/admin/movies"
$fields | ConvertTo-Json -Compress | curl.exe -s -X PUT -H $admin -H $json --data-binary "@-" "$api/api/admin/movies/$movie"
'{"rating":8.2}' | curl.exe -s -X PATCH -H $admin -H $json --data-binary "@-" "$api/api/admin/movies/$movie"

$png = "$env:TEMP\movies-api-tour.png"
[IO.File]::WriteAllBytes($png, [Convert]::FromBase64String("iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNkYPhfDwAChwGA60e6kgAAAABJRU5ErkJggg=="))
$uploaded = curl.exe -s -X POST -H $admin -F "file=@$png;type=image/png" "$api/api/admin/movies/$movie/poster" | ConvertFrom-Json
curl.exe -s "$api$($uploaded.posterUrl)" -o "$env:TEMP\movies-api-downloaded.png"

$startsAt = (Get-Date).AddDays(20).ToString("yyyy-MM-dd") + "T19:00:00Z"
$body = @{ movieId = $movie; startsAt = $startsAt; screen = "Screen 1"; price = 12.5; rows = 6; seatsPerRow = 8 } | ConvertTo-Json -Compress
$created = $body | curl.exe -s -X POST -H $admin -H $json --data-binary "@-" "$api/api/admin/showtimes" | ConvertFrom-Json
'{"price":11.5}' | curl.exe -s -X PATCH -H $admin -H $json --data-binary "@-" "$api/api/admin/showtimes/$($created.id)"

curl.exe -s -X DELETE -H $admin "$api/api/admin/showtimes/$($created.id)"
curl.exe -s -X DELETE -H $admin "$api/api/admin/movies/$movie"
```

### Admin: bookings and demo tools

```powershell
curl.exe -s -X POST -H $admin "$api/api/admin/sample-bookings?userId=u-leo"
curl.exe -s -H $admin "$api/api/admin/bookings?status=CANCELLED&limit=5"
curl.exe -s -H $admin "$api/api/admin/outbox"
curl.exe -s -H $admin "$api/api/admin/settings"
'{"latencyMs":300,"failureRate":0}' | curl.exe -s -X PUT -H $admin -H $json --data-binary "@-" "$api/api/admin/settings"
'{"latencyMs":0,"failureRate":0}' | curl.exe -s -X PUT -H $admin -H $json --data-binary "@-" "$api/api/admin/settings"
```

### Reset

**This deletes all bookings, admin changes, emails and uploads**, also in SQLite.

```powershell
curl.exe -s -X POST -H $admin "$api/api/admin/reset"
```

## Deviations from the fake backend

Found and checked by sending the same 103 scenarios to the Angular `FakeBackend` and to this API. In those
scenarios, everything not listed here behaved the same.

1. `posterUrl` is a relative URL (`/api/posters/{id}.svg` or `/uploads/posters/...`) instead of a `data:` URI.
2. A wrong method on a known path gives 405 `METHOD_NOT_ALLOWED`. The fake backend answers 404.
3. Pydantic checks the shape before the service checks the user. With several faults in one request, the
   reported one can differ.
4. The wording for shape errors (wrong type, missing field, a body that isn't an object) differs.
   Business-rule messages are the same.
5. Cancelling with a body that isn't a JSON object, or with a `userId` that isn't a string, gives `VALIDATION`
   instead of `BAD_REQUEST`.
6. Generated showtimes and reviews don't match the TypeScript random generator. Shapes and rules match.
7. Emails are checked more strictly: special-use domains (`.test`, `.local`), misplaced dots, invalid domain
   names, IP literals and angle brackets are rejected. The domain is stored in lower case.
8. A seat has one spelling, so `A02` is an unknown seat. The fake backend accepts it as a different seat from
   `A2`, which lets a booked seat be sold again.
9. Routing: an unknown path says "No endpoint for ..." (not "No fake endpoint"), a trailing slash redirects
   (307), and an encoded `/` inside an id doesn't match the route.
10. Query parameters that the fake backend ignores are validated here (`sort`, `status`, `limit`, ...). Of a
    repeated single-value parameter the last one wins, not the first.
11. Text with a lone UTF-16 surrogate is rejected, because it can't be sent back as UTF-8.
12. Additions: the new endpoints (such as `GET /api/bookings/{id}`, where the fake backend has 404),
    `seatsRemaining` on showtimes, and `details.errors` on validation errors.

## Known limits

- **One process only.** Locks, the event bus and the demo settings live in memory.
- **No real authentication.** Anyone can pass any `userId`, and `/api/events` shows every user's bookings.
- **WebSockets bypass CORS**, so any website can open one. They are read-only. Seat holds are not built.
- A poster over 2 MB is refused only after the server received it, and uploaded SVGs are served as they are
  (they can run script).
- A client that stops reading a stream can block shutdown: press Ctrl+C twice.
- SQLite handles about 20 bookings a second here. PostgreSQL is untested.
