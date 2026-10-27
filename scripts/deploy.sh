#!/usr/bin/env bash
# Build and deploy the spotify-tracker stack with secrets from .env.
# Extra args pass through to `sam deploy` (e.g. --no-execute-changeset).
set -euo pipefail
cd "$(dirname "$0")/.."

set -a
source .env
set +a

sam build
sam deploy --parameter-overrides \
  "SpotifyClientId=$SPOTIFY_CLIENT_ID" \
  "SpotifyClientSecret=$SPOTIFY_CLIENT_SECRET" \
  "SpotifyRefreshToken=$SPOTIFY_REFRESH_TOKEN" \
  "DashboardPasswordHash=$DASHBOARD_PASSWORD_HASH" \
  "UserTimezone=America/Denver" \
  "LogLevel=INFO" \
  "DomainName=$DOMAIN_NAME" \
  "HostedZoneId=$HOSTED_ZONE_ID" \
  "$@"
