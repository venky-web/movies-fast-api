# Movies API

A FastAPI practice backend that rebuilds (and extends) the fake backend of the Movies MFE Angular app.
It is a learning project, not for production. The plan lives in
[.claude/plans/movies-api-fastapi-plan.md](.claude/plans/movies-api-fastapi-plan.md).

## Run

```powershell
py -3.13 -m venv .venv
.venv\Scripts\Activate.ps1
python -m pip install -e ".[dev,db]"
copy .env.example .env        # optional; defaults work without it
python -m fastapi dev app/main.py
```

The commands use `python -m ...` on purpose. Smart App Control may block the launchers that pip generates
(`pip.exe`, `fastapi.exe`, `alembic.exe`, ...), and which ones it blocks differs from one venv to the next.

Open <http://127.0.0.1:8000/docs> (Swagger UI) or <http://127.0.0.1:8000/redoc>.

By default everything is in memory and is gone after a restart or reload.

## Run on SQLite

```powershell
python -m alembic upgrade head    # creates movies.db (DATABASE_URL)
$env:STORAGE_BACKEND = "sql"      # or put STORAGE_BACKEND=sql in .env
python -m fastapi dev app/main.py    # seeds an empty database, then keeps its data across restarts
```

- `python -m app.seed` seeds without starting the server.
- `python -m app.seed --reset` **deletes all data** and seeds again. Stop the server first.
- After a model change: `python -m alembic revision --autogenerate -m "what changed"`, review the file, then
  `python -m alembic upgrade head`.
- Both modes share the `uploads/` folder, so a reset in one also deletes the other's uploaded posters.

### "An Application Control policy has blocked this file"

Windows Smart App Control may refuse SQLAlchemy's compiled speedups. Leave Smart App Control on and install
SQLAlchemy's official pure-Python wheel instead (same version, somewhat slower):

```powershell
$dir = New-Item -ItemType Directory -Path (Join-Path $env:TEMP "sqlalchemy-pure-$(Get-Random)")
python -m pip download --no-deps --only-binary=:all: --platform any --dest $dir.FullName "sqlalchemy==2.1.9"
python -m pip install --force-reinstall --no-deps (Get-ChildItem $dir.FullName -Filter *.whl).FullName
```

## Test

```powershell
python -m pytest                        # both storage backends
python -m pytest -m "not sql"           # quick loop: memory only
python -m pytest --cov=app --cov-report=term-missing
python -m ruff check . ; python -m ruff format --check .
```
