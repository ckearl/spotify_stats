# Spotify Listening History Tracker - Build Instructions

## System Overview
Build a comprehensive AWS Lambda-based Spotify listening history tracking system that automatically tracks every Spotify listen with rich metadata, infers context from schedules, and allows manual tagging. The system should be cost-optimized (free tier friendly) and provide granular data for custom "Spotify Wrapped" style analytics.

## Architecture Components

### 1. Main Sync Lambda Function (90-minute interval + manual trigger)

**Trigger:** CloudWatch Events (cron: every 90 minutes) OR API Gateway POST to `/sync`

**Functionality:**
- Fetch last 50 tracks from Spotify's Recently Played endpoint (`/v1/me/player/recently-played`)
- Deduplicate: Check if `track_id` + `played_at` already exists in Listens table before inserting
- For new tracks:
  - Check if `track_id` exists in Tracks table
  - If not in Tracks table, fetch track details + audio features from Spotify API
  - Use batch endpoint `/v1/audio-features?ids=...` (up to 100 IDs per call)
  - Store in Tracks table with all track metadata and audio features
- Calculate session boundaries:
  - Group consecutive tracks into sessions
  - New session starts if: gap between tracks > 20 minutes OR gap > previous track's duration (if previous track >20 min)
  - Generate unique `session_id` (UUID) for each session
- Infer activity and location from ACTIVITY_SCHEDULE configuration (see config section below)
- Apply last-known device from Device Tracker data (with carry-forward logic described below)
- Calculate listening duration: `next_track.played_at - current_track.played_at`, capped at `track.duration_ms`
- Store all listens in Listens table

**Device Carry-Forward Logic:**
- Apply last known device to all listens UNLESS:
  - A new device was detected (from Device Tracker Lambda)
  - OR gap between listens > 5 hours
  - OR it's a "new day" (crosses 4am threshold in user's timezone - MDT)
- This prevents device from carrying over after sleep or into new days

**Timezone Handling:**
- Store all timestamps in UTC in DynamoDB
- Convert to MDT (UTC-6 or UTC-7 depending on DST) for schedule matching and "new day" calculations
- User travels frequently, so make timezone configurable via environment variable
- Default timezone: America/Denver (Mountain Time)

**Deduplication Strategy:**
- Use composite key check: `track_id` + `played_at` timestamp
- Query Listens table GSI before inserting
- Skip if already exists (prevents duplicate listens)

---

### 2. Device Tracker Lambda Function (15-minute interval)

**Trigger:** CloudWatch Events (cron: every 15 minutes)

**Functionality:**
- Fetch Spotify's `/v1/me/player/currently-playing` endpoint
- If response is empty or null (nothing currently playing), skip silently and exit
- If actively playing, store device information in DeviceHistory table:
  - `timestamp` (current time in UTC, used as PK)
  - `device_spotify_name` (from Spotify API response: `device.name`)
  - `device_spotify_type` (from Spotify API response: `device.type`)
  - `device_custom_name` (lookup from DeviceMappings table if a mapping exists, else null)

**Retroactive Device Matching:**
- When Main Sync Lambda runs, it should match listens to device info
- Matching strategy: find the most recent device poll within a 15-minute buffer
- Example: if device was polled at 9:15pm, apply that device to all tracks played between 9:00pm-9:15pm
- Use the carry-forward logic described above (last known device applies until new one detected, 5-hour gap, or new day)

---

### 3. Manual Tagging API Endpoints (API Gateway + Lambda)

Create four POST endpoints for manual data correction and enrichment:

#### **POST /tag-listen**
Update a single listen with manual tags.

**Request Body:**
```json
{
  "listen_id": "uuid-string",
  "activity": "string (optional)",
  "location": "string (optional)",
  "device_custom_name": "string (optional)"
}
```

**Behavior:**
- Updates `activity_manual`, `location_manual`, `device_custom_name` fields for the specified listen_id
- Manual values always take precedence over inferred values in queries/analytics
- Return 200 on success, 400 if listen_id not found, 500 on error

---

#### **POST /tag-session**
Update all listens in a session with the same tags.

**Request Body:**
```json
{
  "session_id": "uuid-string",
  "activity": "string (optional)",
  "location": "string (optional)",
  "device_custom_name": "string (optional)"
}
```

**Behavior:**
- Find all listens with matching `session_id`
- Update all of them with the provided manual tags
- Retroactively applies tags to entire listening session
- Return 200 on success with count of updated records, 400 if session not found

---

#### **POST /tag-range**
Update all listens within a datetime range.

**Request Body:**
```json
{
  "start_time": "ISO 8601 timestamp (UTC)",
  "end_time": "ISO 8601 timestamp (UTC)",
  "activity": "string (optional)",
  "location": "string (optional)",
  "device_custom_name": "string (optional)"
}
```

**Behavior:**
- Find all listens where `played_at` falls between start_time and end_time (inclusive)
- Update all matching listens with provided manual tags
- Overrides session boundaries (useful for manual correction)
- Return 200 on success with count of updated records

---

#### **POST /map-device**
Create a persistent mapping from Spotify device name to custom device name.

**Request Body:**
```json
{
  "spotify_device_name": "string (e.g., 'iPhone', 'MacBook Pro')",
  "custom_name": "string (e.g., 'AirPod Pros', 'Over-ear headphones')"
}
```

**Behavior:**
- Store mapping in DeviceMappings table
- Future device polls will automatically lookup and apply custom_name
- Existing listens are NOT retroactively updated (only future ones)
- Return 200 on success, 409 if mapping already exists (allow overwrite)

---

## DynamoDB Schema

### **Tracks Table**
Stores unique track metadata and audio features.

**Primary Key:** `track_id` (string) - Spotify track ID

**Attributes:**
- `track_id` (string, PK) - Spotify track ID
- `name` (string) - Track name
- `artists` (list of strings) - Artist names
- `album` (string) - Album name
- `duration_ms` (number) - Track duration in milliseconds
- `popularity` (number) - Spotify popularity score (0-100)
- `release_date` (string) - Album release date
- `genres` (list of strings) - Track/artist genres
- `audio_features` (map) - Audio analysis from Spotify:
  - `danceability` (number, 0-1)
  - `energy` (number, 0-1)
  - `valence` (number, 0-1) - musical positivity
  - `tempo` (number) - BPM
  - `acousticness` (number, 0-1)
  - `instrumentalness` (number, 0-1)
  - `liveness` (number, 0-1)
  - `speechiness` (number, 0-1)
  - `loudness` (number, dB)
  - `key` (number, 0-11) - pitch class
  - `mode` (number, 0 or 1) - major/minor
  - `time_signature` (number) - beats per bar

**Note:** Only insert tracks once. Check existence before calling Spotify API.

---

### **Listens Table**
Stores individual track play events with context.

**Primary Key:** `listen_id` (UUID string)

**Global Secondary Index (GSI):** 
- Name: `TrackPlayedAtIndex`
- Partition Key: `track_id`
- Sort Key: `played_at`
- Purpose: Fast deduplication lookup

**Attributes:**
- `listen_id` (string, PK) - Unique UUID for this listen
- `track_id` (string) - Foreign key to Tracks table
- `session_id` (string) - UUID grouping consecutive tracks
- `played_at` (string) - ISO 8601 timestamp in UTC
- `context_type` (string, nullable) - "playlist", "album", "artist", "collection", etc.
- `context_uri` (string, nullable) - Spotify URI of context
- `device_spotify_name` (string, nullable) - Device name from Spotify (e.g., "iPhone")
- `device_spotify_type` (string, nullable) - Device type from Spotify (e.g., "Smartphone")
- `device_custom_name` (string, nullable) - User's custom device name (e.g., "AirPod Pros")
- `activity_inferred` (string, nullable) - Inferred from ACTIVITY_SCHEDULE
- `activity_manual` (string, nullable) - User-provided via POST endpoint
- `location_inferred` (string, nullable) - Inferred from ACTIVITY_SCHEDULE
- `location_manual` (string, nullable) - User-provided via POST endpoint
- `duration_played_ms` (number) - Calculated listening duration (next_track.played_at - current_track.played_at, capped at track.duration_ms)

**Query Priority:** When displaying data, always prefer `*_manual` fields over `*_inferred` fields if manual value exists.

---

### **DeviceHistory Table**
Stores device polling results from Currently Playing endpoint.

**Primary Key:** `timestamp` (ISO 8601 timestamp string in UTC)

**Attributes:**
- `timestamp` (string, PK) - When device was polled
- `device_spotify_name` (string) - Device name from Spotify
- `device_spotify_type` (string) - Device type from Spotify
- `device_custom_name` (string, nullable) - Looked up from DeviceMappings table

**TTL (optional):** Set TTL for 90 days to auto-delete old device history and save storage costs.

---

### **DeviceMappings Table**
Stores persistent mappings from Spotify device names to user's custom names.

**Primary Key:** `spotify_device_name` (string)

**Attributes:**
- `spotify_device_name` (string, PK) - Device name from Spotify (e.g., "iPhone")
- `custom_name` (string) - User's custom name (e.g., "AirPod Pros", "JBL Speaker 1")

---

## Activity/Location Schedule Configuration

The user will provide their personal ACTIVITY_SCHEDULE after initial code generation. Structure it as a Python dictionary embedded in the Lambda code:

```python
# TODO: USER MUST FILL IN THEIR SCHEDULE BEFORE DEPLOYMENT
ACTIVITY_SCHEDULE = {
    "weekday": {
        # Format: "HH:MM-HH:MM": (activity, location)
        "08:00-16:00": ("focus", "work"),
        "06:00-08:00": ("exercise", "gym"),
        "17:00-18:00": ("exercise", "outdoor"),
        # Add more time ranges as needed
    },
    "weekend": {
        "09:00-11:00": ("exercise", "gym"),
        # Add more time ranges as needed
    }
}
# If no match found, defaults to (None, None) - stored as null in DynamoDB
```

**Schedule Matching Logic:**
1. Convert `played_at` timestamp from UTC to user's timezone (MDT)
2. Determine if the date is a weekday (Mon-Fri) or weekend (Sat-Sun)
3. Extract time in HH:MM format
4. Check if time falls within any defined range in the appropriate day_type
5. If match found, apply activity and location as `activity_inferred` and `location_inferred`
6. If no match (time outside all defined ranges), leave both fields as null

**Important:** Manual tags (`activity_manual`, `location_manual`) always take precedence over inferred values.

---

## Environment Variables

Configure these in AWS Lambda environment settings:

**Required:**
- `SPOTIFY_CLIENT_ID` - Spotify app client ID
- `SPOTIFY_CLIENT_SECRET` - Spotify app client secret  
- `SPOTIFY_REFRESH_TOKEN` - OAuth refresh token for user authorization

**Optional:**
- `USER_TIMEZONE` - Default: "America/Denver" (MDT/MST)
- `DYNAMODB_REGION` - Default: "us-east-1"
- `LOG_LEVEL` - Default: "INFO" (options: DEBUG, INFO, WARNING, ERROR)

**Spotify OAuth Setup:**
Include instructions in README for obtaining refresh token using Authorization Code Flow with these scopes:
- `user-read-recently-played`
- `user-read-currently-playing`
- `user-read-playback-state`

---

## Code Requirements

### **Language & Runtime**
- Python 3.11 or higher
- AWS Lambda runtime: `python3.11`

### **Dependencies (requirements.txt)**
```
boto3>=1.28.0
requests>=2.31.0
python-dateutil>=2.8.2
```

**Note:** `boto3` is included in Lambda runtime, but include in requirements.txt for local testing.

### **Project Structure**

Organize code into modular components:

```
spotify-tracker/
├── lambda_handlers/
│   ├── sync_handler.py          # Main 90-min sync Lambda
│   ├── device_tracker_handler.py # 15-min device polling Lambda
│   ├── tag_listen_handler.py    # POST /tag-listen
│   ├── tag_session_handler.py   # POST /tag-session
│   ├── tag_range_handler.py     # POST /tag-range
│   └── map_device_handler.py    # POST /map-device
├── utils/
│   ├── spotify_client.py        # Spotify API wrapper with token refresh
│   ├── dynamo_client.py         # DynamoDB operations (CRUD)
│   ├── schedule_matcher.py      # Activity/location inference from schedule
│   ├── session_calculator.py    # Session boundary calculation logic
│   └── logger.py                # Centralized logging configuration
├── config.py                     # ACTIVITY_SCHEDULE and constants
├── requirements.txt
└── README.md
```

### **Code Quality Standards**

**Error Handling:**
- Wrap all Spotify API calls in try-except blocks
- Gracefully handle API downtime (log error, skip iteration, don't crash)
- Implement exponential backoff retry for DynamoDB throttling
- Validate all API Gateway request payloads (return 400 for invalid input)
- Return proper HTTP status codes: 200 (success), 400 (bad request), 404 (not found), 500 (server error)

**Logging:**
- Use Python's `logging` module with structured logs
- Log levels: DEBUG for detailed flow, INFO for key events, WARNING for retries, ERROR for failures
- Log key metrics: tracks processed, duplicates skipped, API calls made, errors encountered
- All logs automatically go to CloudWatch Logs

**Testing Considerations:**
- Code should handle empty responses (no recently played tracks, no currently playing)
- Handle tracks without audio features (some tracks don't have them - skip gracefully)
- Handle duplicate listens (common when user repeats tracks)
- Handle timezone edge cases (DST transitions in spring/fall)
- Handle session edge cases (tracks spanning midnight, very long tracks >20 min)
- Handle device tracker returning null (nothing playing)

**Security:**
- Never log Spotify tokens or secrets
- Use environment variables for all credentials
- Validate all user input to prevent injection attacks
- Use least-privilege IAM roles for Lambda execution

---

## Deployment Artifacts

Provide the following files for easy AWS deployment:

### 1. **Lambda Function Code**
- All handler files and utility modules (see project structure above)
- ZIP file creation script or instructions

### 2. **requirements.txt**
- All Python dependencies with pinned versions

### 3. **Infrastructure as Code**
Choose ONE of the following (user preference):

**Option A: Terraform**
- `main.tf` - DynamoDB tables, Lambda functions, IAM roles, CloudWatch Events, API Gateway
- `variables.tf` - Configurable parameters
- `outputs.tf` - Endpoint URLs and resource ARNs

**Option B: CloudFormation (YAML)**
- `template.yaml` - Complete stack definition
- SAM-compatible if possible

Include:
- DynamoDB table definitions with GSIs and TTL configurations
- Lambda function resources with proper memory/timeout settings
- CloudWatch Events rules (cron expressions)
- API Gateway REST API with all routes and Lambda integrations
- IAM roles and policies (least privilege)

### 4. **Cron Expressions**

**Main Sync Lambda:** Every 90 minutes
```
cron(0/90 * * * ? *)
```

**Device Tracker Lambda:** Every 15 minutes
```
cron(0/15 * * * ? *)
```

### 5. **Comprehensive README.md**

Include the following sections:

**Setup Instructions:**
1. Prerequisites (AWS account, Spotify developer account, AWS CLI, Terraform/SAM)
2. Clone repository and install dependencies
3. Configure Spotify OAuth (step-by-step with screenshots)
4. Obtain Spotify refresh token (provide Python script or curl commands)
5. Set environment variables
6. Fill in ACTIVITY_SCHEDULE in config.py
7. Deploy infrastructure (terraform apply or sam deploy)
8. Test endpoints with curl examples
9. Verify CloudWatch logs

**Spotify OAuth Guide:**
- Register app at developer.spotify.com
- Configure redirect URI
- Get authorization code
- Exchange for refresh token
- Test token with curl

**Cost Estimation:**
- Lambda: Free tier (1M requests/month) - expected ~500/month for cron jobs
- DynamoDB: Free tier (25GB storage, 25 RCU/WCU) - should stay within limits
- API Gateway: Free tier (1M requests/month) - manual calls only, negligible
- CloudWatch Logs: Free tier (5GB) - should be sufficient
- **Expected monthly cost: $0** (within free tier)

**Example Analytics Queries:**
Provide DynamoDB query examples for common analytics:
- Top 10 tracks in date range
- Top 10 artists in date range
- Total listening time by activity type
- Listening patterns by time of day
- Device usage breakdown
- Session statistics (average session length, tracks per session)

**Troubleshooting Guide:**
- Common errors and solutions
- How to check CloudWatch logs
- How to manually trigger sync
- How to verify Spotify token is working

---

## Bonus Features (Implement if feasible)

### **Stats Query Endpoint**
**GET /stats?start_date=YYYY-MM-DD&end_date=YYYY-MM-DD**

Returns JSON with:
```json
{
  "date_range": {"start": "...", "end": "..."},
  "total_listening_time_ms": 123456,
  "total_tracks": 150,
  "total_sessions": 12,
  "top_tracks": [
    {"track_name": "...", "artist": "...", "play_count": 5, "total_time_ms": 1234}
  ],
  "top_artists": [
    {"artist": "...", "play_count": 10, "total_time_ms": 5678}
  ],
  "listening_by_activity": {
    "focus": {"count": 50, "time_ms": 12345},
    "exercise": {"count": 30, "time_ms": 6789}
  },
  "listening_by_device": {
    "AirPod Pros": {"count": 40, "time_ms": 9876}
  }
}
```

### **Advanced Session Grouping**
If the last track in a potential session is >20 minutes long, and the next track starts within that track's duration, keep them in the same session.

Example: 
- Track A (25 min) ends at 2:00pm
- Track B starts at 2:15pm (within Track A's 25-min duration)
- Result: Track B stays in same session as Track A

### **Listening Duration Calculation**
For each listen, calculate actual listening time:
```python
if next_track_exists:
    duration_played = min(
        next_track.played_at - current_track.played_at,
        current_track.duration_ms
    )
else:
    # Last track in session - assume full duration
    duration_played = current_track.duration_ms
```

Store in `duration_played_ms` field.

---

## Testing Checklist

Before considering the system complete, verify it handles:

**Data Integrity:**
- ✅ Duplicate listens (same track played multiple times in different sessions)
- ✅ Same track played back-to-back (different `played_at` timestamps)
- ✅ Tracks without audio features (graceful skip with log message)
- ✅ Very long tracks (>20 min) affecting session boundaries

**API Edge Cases:**
- ✅ Empty recently played response (user hasn't listened in >24 hours)
- ✅ Currently playing endpoint returns null (nothing playing right now)
- ✅ Spotify API rate limiting (implement retry with backoff)
- ✅ Invalid Spotify refresh token (clear error message, don't crash)

**Timezone & Time-based Logic:**
- ✅ Listens spanning midnight (session handling)
- ✅ Daylight Saving Time transitions (spring forward, fall back)
- ✅ "New day" calculation (4am threshold in MDT)
- ✅ 5-hour gap device reset

**Manual Tagging:**
- ✅ Tagging single listen
- ✅ Tagging entire session (retroactive update)
- ✅ Tagging date range (multiple sessions)
- ✅ Device mapping application to future listens
- ✅ Manual values overriding inferred values in queries

**Cost Optimization:**
- ✅ No unnecessary Spotify API calls (check DB before fetching)
- ✅ Batch audio features requests (up to 100 per call)
- ✅ DynamoDB query patterns use GSI efficiently
- ✅ Lambda functions timeout appropriately (suggest 60s for sync, 30s for device tracker)
- ✅ CloudWatch log retention set to reasonable period (suggest 30 days)

---

## Final Deliverables

When complete, the system should include:

1. ✅ Fully functional Lambda code (all handlers + utilities)
2. ✅ requirements.txt with all dependencies
3. ✅ Infrastructure as code (Terraform or CloudFormation)
4. ✅ Comprehensive README.md with setup instructions
5. ✅ Example .env file template
6. ✅ ACTIVITY_SCHEDULE template in config.py (with TODO comment)
7. ✅ Example queries for analytics
8. ✅ Troubleshooting guide

**Production-Ready Standards:**
- Clean, readable, well-commented code
- Proper error handling throughout
- Structured logging for debugging
- No hardcoded credentials
- Modular design for easy maintenance
- Clear documentation

---

## User Notes for Claude Code

**Important Context:**
- I have Spotify developer credentials ready to use
- I will provide my personal ACTIVITY_SCHEDULE after you generate the initial code structure (add a TODO comment where I need to fill this in)
- I'll test everything locally first before deploying to AWS
- Keep all AWS resources within free tier limits
- I want this to be production-ready and maintainable
- Prioritize code quality and documentation

**Please build this system with:**
- Clear separation of concerns (handlers vs utilities)
- Comprehensive error handling
- Detailed inline comments
- A stellar README.md that makes setup painless
- Example scripts or commands for common operations

Let's make this tracking system bulletproof and insightful!
