# Listening Dashboard — Design

Date: 2026-10-24
Status: approved 2026-10-24

## Intent

A private, personal stats explorer for the owner's Spotify listening, served at
`https://<your-domain>/dashboard`. Think stats.fm, but far more
detailed. The owner is "a very curious guy": the core move is searching any
artist, album, or track (e.g. "Led Zeppelin") and seeing every number about it —
minutes listened, plays per track, totals per album, time per track and album,
when it was played, and more. The front page shows recently played plus an
overview of listening, driven by a timeline.

The owner will use it on both phone and laptop, test it over a few weeks, and
send feedback. v1 should be solid and complete enough to live with, not a
prototype.

## Goals

- Front page = overview + timeline (chosen over "search first" or
  "timeline only"):
  - Search box at the top.
  - Minutes-per-week chart; drag on it (or use presets: 7d / 30d / 90d / all
    time / custom) to set a date range.
  - Everything below follows the selected range: headline totals (hours,
    plays, unique artists, avg per day), recently played, top artists / tracks /
    albums, and an hour × weekday "when you listen" heatmap.
  - Known data outages are shown as a marked gap on the timeline, not as zero
    listening.
- Entity pages for artist, album, and track, reached from search or any list.
  Artist page (mocked with real data; album and track pages follow the same
  shape):
  - Header: name, first heard, last heard, rank among all artists.
  - Totals: minutes, plays, distinct tracks, distinct albums.
  - Plays-over-time chart.
  - Albums table (minutes, plays) and tracks table (plays, minutes), sortable by
    plays / minutes / last played.
  - Their own hour × weekday heatmap.
  - A full log of every play.
- Search is instant, across artists, albums, and tracks, including liked songs
  that have never been played (shown with 0 plays).
- "Virtually everything": beyond the above, the builder should add stats the
  data supports. Expected at minimum: longest listening day, listening streaks,
  new artists discovered in range, sessions (count, average and longest
  length), how listening started (playlist / album / artist context), release
  decade breakdown, and "liked but never played". Activity and location tags
  only appear if the data has values (today it effectively doesn't).
- Works well on phone (single column, stacked) and laptop.

## Non-goals (v1)

- Device stats. Every listen's device is "Unknown" or empty today because the
  device tracker isn't being joined to listens. Hide device stats until that's
  fixed (separate task).
- Importing Spotify's extended streaming history (years of back data). Valuable
  follow-up, separate project.
- Album art / images (not stored today).
- Locking down the existing open endpoints (`/stats`, `/iphone-widget`, tagging,
  `/sync`). They stay as they are so the iPhone widget keeps working.
- Editing tags from the dashboard.
- "Currently playing" live status.

## Anti-goals (failure even if it works)

- Listening data readable without the password, including through the raw
  `execute-api` URL or by reading the page source.
- Breaking the tracker, its schedules, or the iPhone widget.
- Slow or janky on a phone.
- Totals that look wrong because the same album/track is split across
  re-releases.

## Constraints and existing system

- Backend: the existing `spotify-tracker` SAM stack (this repo,
  us-west-2). API Gateway REST API on
  custom domain `<your-domain>` with no base path. Stack has
  `disable_rollback = true` in `samconfig.toml`; avoid changes that *replace*
  existing resources (a replace fails on this stack).
- This backend directory is not a git repo.
- Data (as of 2026-10-24): ~3,700 listens since 2026-04-03, ~12,000 tracks
  (most are Liked Songs from the backfill, never played). Known outage:
  2026-07-20 to 2026-10-24 (expired Spotify token).
- Listen fields: `listen_id`, `track_id`, `played_at` (ISO UTC),
  `duration_played_ms`, `session_id`, `context_type`, `context_uri`, device
  fields (empty), activity/location fields (mostly empty).
- Track fields: `track_id`, `name`, `album`, `artists` (list of names),
  `duration_ms`, `release_date`, `popularity`, `genres` (empty today).
- `duration_played_ms` is an estimate: the gap until the next play, capped at the
  track length. Label minutes as estimated somewhere unobtrusive.
- Owner's timezone: America/Denver (for days, hours, weekdays).
- Sync runs every 90 minutes, so "recently played" can lag by up to 90 minutes.

## Decided

**Hosting and privacy (owner):** dashboard lives at `/dashboard` on the API
domain. Private behind a password (only its SHA-256 is stored).

**How privacy works (owner agreed):**
- The dashboard page contains no data and can be public.
- A new data endpoint in the tracker stack returns the dataset only when the
  request carries the right password (checked server-side, constant-time
  compare against a SHA-256 hash stored as a NoEcho stack parameter, not the
  plaintext).
- The browser remembers the password after first entry; a logout control clears
  it.

**Repo (owner said "however you need"):** new private GitHub repo
`spotify-stats-dashboard` under the owner's account (`ckearl`), holding the
frontend and its deploy script. Backend additions (data endpoint, serving
`/dashboard`) live in the tracker stack.

**Data model in the browser:** the dashboard downloads the whole dataset once
per visit and does search and all math client-side. Instant searches; new stats
need no backend changes.

**Merging editions:** albums and tracks that differ only by re-release labels
("Remaster", "Deluxe Edition", "2014 Remaster", "- Remastered 2011",
"Anniversary Edition", "Expanded", etc.) are merged and display the clean name.
"Live", "Demo", "Acoustic", and remix versions stay separate.

## My calls (builder may revisit with reason)

- **Serving `/dashboard`:** a small Lambda behind `GET /dashboard` and
  `GET /dashboard/{proxy+}` that returns the built `index.html` from an S3
  bucket created in the tracker stack. The frontend builds to a single
  self-contained HTML file. This avoids CloudFront, a us-east-1 certificate,
  and moving DNS. The dashboard repo's deploy = build + upload `index.html` to
  that bucket. Path-based client routing under `/dashboard/...`; the Lambda
  returns `index.html` for every `/dashboard/*` path.
- **Data endpoint:** `GET /dashboard-data`, password in a request header.
  On success it returns a short-lived presigned URL to a gzipped JSON snapshot
  in S3 (compact arrays, only needed fields) of all listens and all tracks.
  The snapshot is rebuilt only when the newest listen differs from the one it
  was built from, when it is over 24 hours old, or on `?refresh=1`. Reason
  (found while planning): the Tracks table is provisioned at 5 RCU, so scanning
  ~12k tracks on every page load would throttle; the snapshot also sidesteps
  Lambda's 6 MB response limit. The bucket allows CORS GET from the dashboard
  origin and `http://localhost:5173` (dev).
- **Brute-force:** throttle the data endpoint at the API Gateway method level
  (low rate). The password is a long phrase.
- **Artist credit:** a play counts for every artist listed on the track
  (features included), like stats.fm. Rankings use the same rule.
- **Outage gaps:** listed in a small config in the frontend (start/end dates),
  not auto-detected, so a vacation isn't mislabeled as an outage.
- **Frontend stack:** Vite + React + TypeScript, built to a single HTML file.
  Charts and heatmaps via a small, fast charting library or hand-rolled SVG.
  Unit tests (Vitest) for the aggregation and name-normalization logic.
- **Minutes vs plays toggle** on top lists; default ranking by minutes.
- **Album identity (no backend changes in v1):** Tracks store only the album
  name and track artists — no album ID or album artist. Group tracks into an
  album when their normalized album name matches *and* they share at least one
  artist. This keeps featured-artist tracks on their album and keeps
  same-named albums by different artists ("Greatest Hits") apart. Known
  limitation: various-artists compilations may split. Follow-up (not v1):
  store album ID, album artist and album art in sync, and backfill existing
  tracks from Spotify — would also enable album art.
- **Song identity:** a "song" is normalized track name + primary artist,
  merged across albums (studio album, compilation, soundtrack). The track page
  shows a per-album breakdown of its plays. Rankings and "distinct tracks"
  counts use songs.
- **Listening context:** type breakdown only (playlist / album / artist /
  other) in v1. Resolving playlist names needs Spotify lookups; follow-up.
- **"Liked":** a track counts as liked if it's in the Tracks table from the
  liked-songs backfill. It isn't kept in sync (later likes/unlikes aren't
  reflected). Good enough for v1; note the limitation next to the
  "liked but never played" stat.

## Left to the builder

- Visual design (dark, Spotify-adjacent is fine), typography, exact layout
  within the agreed structure.
- Search matching (fuzzy/prefix), result grouping and ordering.
- URL shapes for entity pages (slugs vs IDs).
- Exactly which extra stats make the cut beyond the minimum list, and where
  they sit.
- How the password prompt looks.
- Deploy script details; whether to add a GitHub Action later.
