# Spotify Listening History Tracker

A serverless backend that records every song I play on Spotify, going back to 2020, and serves it
to a personal stats dashboard ([spotify-stats-dashboard](https://github.com/ckearl/spotify-stats-dashboard)).
Think Spotify Wrapped, but any day of the year and for any date range.

Spotify's API only remembers your last 50 plays, so this keeps its own history: a sync every 90
minutes, a poller every minute for clients Spotify doesn't report (librespot players), and a
one-time import of Spotify's Extended Streaming History export for everything before that.
Plays are grouped into listening sessions, deduplicated across all three sources, and can be
tagged with an activity or location.

It runs on AWS Lambda, DynamoDB, API Gateway and S3, provisioned to stay inside the free tier.

## How it works

```
EventBridge schedules
  ├─ every 90 min ──> spotify-sync ───────────> recently-played → Tracks, Listens
  ├─ every minute ──> spotify-device-tracker ─> playback poller → Listens (librespot plays)
  └─ daily ────────> spotify-dashboard-data ──> refresh the dashboard snapshot

API Gateway (custom domain, every endpoint password-protected)
  ├─ GET  /dashboard ────────> the dashboard (one HTML file from S3)
  ├─ GET  /dashboard-data ───> presigned URL to the snapshot
  ├─ POST /sync ─────────────> run a sync now
  ├─ POST /tag-listen | /tag-session | /tag-range ─> manual activity/location tags
  ├─ POST /map-device ───────> friendly names for Spotify devices
  ├─ GET  /stats ────────────> totals and top lists for a date range
  └─ GET  /iphone-widget ────> recent plays and today/this week totals
```

**Storage**
- DynamoDB: `Tracks` (track metadata) and `Listens` (one row per play), with GSIs for
  dedupe (track + time), sessions, and time ranges. Plus small tables for device history,
  device names and the poller's state.
- S3: the dashboard page, and a gzipped JSON snapshot of every listen and track. Whatever
  writes listens also appends to the snapshot with conditional writes, so the dashboard never
  waits on a rebuild.

**Some details**
- **Deduplication:** the sync, the poller and the export see the same play with timestamps up
  to ~90 s apart. The same track within 90 s counts as one play; relinked track IDs fall back
  to name + artist.
- **Sessions:** a new session starts after 20 minutes of silence.
- **Throughput:** DynamoDB is provisioned at 5 RCU/WCU per table to stay free, so nothing on
  a Lambda scans the full table. The daily snapshot refresh only re-reads the last 7 days.
  Full rebuilds run locally (`scripts/rebuild_snapshot.py`).

## Setup

**Prerequisites:** an AWS account with the CLI configured, the
[SAM CLI](https://docs.aws.amazon.com/serverless-application-model/latest/developerguide/install-sam-cli.html),
Python 3.11+, a [Spotify developer app](https://developer.spotify.com/dashboard), and a domain
in Route 53.

1. **Spotify app:** create an app and add `http://127.0.0.1:7777/callback` as a redirect URI.
2. **Refresh token:** run `python scripts/get_refresh_token.py` and approve access in the
   browser (scopes: recently played, currently playing, playback state).
3. **Configuration:** copy `.env.example` to `.env` and fill it in:
   - the Spotify client ID, secret and refresh token
   - `DASHBOARD_PASSWORD_HASH`, from `echo -n "<password>" | shasum -a 256`
   - `DOMAIN_NAME` and `HOSTED_ZONE_ID`
4. **Schedule (optional):** edit `ACTIVITY_SCHEDULE` in `config.py` to tag plays with an
   activity and location by time of day.
5. **Deploy:** run `scripts/deploy.sh`. It builds and deploys with every parameter taken from
   `.env`.
   - Preview first with `scripts/deploy.sh --no-execute-changeset --no-confirm-changeset`.
   - The stack runs with `disable_rollback`, so a change that replaces a resource fails.
     Rename the resource's logical ID instead.

## Using the API

Every API request needs the dashboard password in an `X-Dashboard-Password` header:

```bash
H="X-Dashboard-Password: $PASSWORD"
curl -X POST -H "$H" https://$DOMAIN_NAME/sync
curl -H "$H" "https://$DOMAIN_NAME/stats?start_date=2026-01-01&end_date=2026-01-31"
curl -X POST -H "$H" -H "Content-Type: application/json" https://$DOMAIN_NAME/tag-session \
  -d '{"session_id": "…", "activity": "exercise", "location": "gym"}'
```

`/tag-listen` takes a `listen_id` and `/tag-range` takes `start_time` and `end_time`; both
accept `activity`, `location` and `device_custom_name`. `/map-device` takes
`spotify_device_name` and `custom_name`.

## Importing your full history

Spotify will email you an **Extended Streaming History** export on request (Account → Privacy
→ Download your data). It covers every play since the account was created.

```bash
python scripts/import_extended_history.py            # dry run: what would be written
python scripts/import_extended_history.py --apply    # write it, then rebuild the snapshot
```

**What gets imported:** music plays of 30 s or more. Podcasts, audiobooks and video are
dropped. Plays the tracker already logged are skipped, but they get the export's exact
listening time.

**Running it:** the import fetches missing track metadata from Spotify and caches everything in
`~/.cache/spotify_stats/`, so a stopped run resumes where it left off.

**Speed:** by default it paces writes to fit the free-tier capacity (~16 hours for 85k plays).
To go faster, raise capacity temporarily with `aws dynamodb update-table`, run with
`--listen-rate 80 --track-rate 40` (about an hour), then set the capacity back to
`template.yaml`'s values.

## Development

```bash
python3 -m pytest tests -q
```

| Path | What's there |
|------|--------------|
| `lambda_handlers/` | one module per Lambda |
| `utils/` | Spotify and DynamoDB clients, sessions, the playback poller, the snapshot, auth |
| `scripts/` | deploy, token setup, history imports, backfills, the local snapshot rebuild |
| `template.yaml` | the SAM stack |
| `spotify_tracker_prompt.md` | the original build brief |

Secrets stay in `.env` (git-ignored). SAM packages the whole project directory into each
Lambda, so keep data files and caches outside it.

## Cost

Everything stays within the AWS free tier at single-user volume. The provisioned DynamoDB
capacity (about 20 RCU and 20 WCU in total) is under the free 25 of each, and the Lambda and
API Gateway calls are far under their limits. Expected cost is about $0 a month, plus Route 53
for the domain.
