# Fixtures

`mfe-seed.json` is reference output from the Angular app's seed code (`movies`, `showtimes` for 2026-11-19 12:00
Europe/London, `users`). The shape tests compare our JSON with it. Generated values (showtimes, reviews) are not
expected to match, because we use Python's `random` instead of the TS PRNG.

Regenerate it from the `movies-api` folder (needs the sibling `movies-mfe` repo with `node_modules`):

```bash
../movies-mfe/node_modules/.bin/esbuild tests/fixtures/capture-mfe.ts --bundle --platform=node \
  "--banner:js=process.env.TZ='Europe/London';" --outfile="$TEMP/capture-mfe.js"
node "$TEMP/capture-mfe.js" > tests/fixtures/mfe-seed.json
```

The `TZ` banner is needed because Node on Windows ignores the `TZ` environment variable set outside the process.
