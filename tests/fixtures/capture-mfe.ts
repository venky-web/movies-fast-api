// Captures reference JSON from the Angular app's seed code (run from movies-api, see tests/fixtures/README.md).
import { SEED_MOVIES } from '../../../movies-mfe/projects/shared/src/lib/data/movies.seed';
import { generateShowtimes } from '../../../movies-mfe/projects/shared/src/lib/data/showtimes.seed';
import { SEED_USERS } from '../../../movies-mfe/projects/shared/src/lib/data/users.seed';

const now = new Date(2026, 10, 19, 12, 0, 0); // same instant as the Angular specs (TZ=Europe/London)
process.stdout.write(
  JSON.stringify({ now: now.toISOString(), movies: SEED_MOVIES, showtimes: generateShowtimes(SEED_MOVIES, now), users: SEED_USERS }, null, 1),
);
