# Remote TrainingPeaks MCP on Render

This runs the TrainingPeaks MCP server on Render as a web service at `https://<your-service>.onrender.com/mcp`,
for ChatGPT. Everything below happens in a browser; no terminal is needed.

The local stdio server (`tp-mcp serve`) is unchanged. The remote server (`tp-mcp serve-http`) is a separate protected service, read-only by default.

## What the remote server exposes

By default the remote endpoint exposes **29 read-only tools**. An optional
`TP_MCP_ENABLE_WRITES=true` setting adds **17 specifically allowlisted
coaching-write tools** (46 total) on a client or plan that supports them.
Unlisted tools are refused at dispatch time, regardless of the client.

| Area | Read-only tools |
|------|-----------------|
| Workouts | `tp_get_workouts`, `tp_get_workout`, `tp_get_strength_workouts`, `tp_get_strength_workout`, `tp_get_strength_summary` |
| Workout analysis | `tp_analyze_workout`, `tp_get_workout_prs`, `tp_get_peaks`, `tp_validate_structure` |
| Comments and notes | `tp_get_workout_comments`, `tp_get_workout_note`, `tp_list_notes`, `tp_get_note`, `tp_get_note_comments` |
| Fitness history | `tp_get_fitness`, `tp_get_weekly_summary`, `tp_get_atp` |
| Athlete settings | `tp_get_athlete_settings`, `tp_get_pool_length_settings`, `tp_get_workout_types` |
| Metrics | `tp_get_metrics`, `tp_get_nutrition` |
| Events | `tp_get_events`, `tp_get_next_event`, `tp_get_focus_event` |
| Libraries and exercises | `tp_get_libraries`, `tp_get_library_items`, `tp_get_library_item`, `tp_search_exercises` |

Never exposed: deletion, upload, browser-cookie extraction, authentication refresh,
FTP/zone updates, pairing/unpairing, cross-athlete mutation, bulk plan application.

## How access is protected

ChatGPT connects only through OAuth, so the server is its own OAuth authorization server for one owner: you.

- `/mcp` answers only requests that carry a valid access token. Anything else gets `401`.
- When ChatGPT connects, it opens a page on your server that asks for the **owner password**
  (`MCP_OWNER_PASSWORD`). Without it, no token is issued. After 10 wrong passwords in 15 minutes the page locks
  for 15 minutes.
- Only ChatGPT's own callback addresses (`https://chatgpt.com/connector_platform_oauth_redirect` and
  `https://chatgpt.com/connector/oauth/...`) can register.
- Access tokens last 1 hour and refresh tokens 30 days. All of them are signed with `MCP_SIGNING_KEY`, so a
  Render restart or redeploy does not disconnect ChatGPT, and changing that key disconnects every client at once.
- Preferred: Render stores `TP_USERNAME` and `TP_PASSWORD` as private secrets, and the service signs in to TrainingPeaks, exchanging the returned session cookie for short-lived API tokens. The password is not logged or copied into GitHub.
- The legacy `TP_AUTH_COOKIE` setting is optional, for accounts that already possess a valid cookie.

## 1. Connect TrainingPeaks without DevTools

**Do not extract a browser cookie.** For the existing service, go to
[Render → trainingpeaks-mcp → Environment](https://dashboard.render.com/web/srv-dbi3po0m7kps73bqanf0).
Add these two private environment variables and save:

- `TP_USERNAME`: your **TrainingPeaks username**, which may be different from your email
- `TP_PASSWORD`: your TrainingPeaks password

Do not paste either value in a chat or into GitHub. The remote server sends
them directly to TrainingPeaks' own HTTPS login endpoint, obtains a session
cookie itself, and automatically signs in again when its session expires.
Its existing `TP_AUTH_COOKIE` setting is **not required**.

A username/password-based login is an unofficial TrainingPeaks web interface.
If TrainingPeaks changes its form or requires SSO/additional verification,
the server reports a clean failure. For accounts with SSO-only access, this
particular method may not work.

## 2. Render deployment

The service is already created at
`https://trainingpeaks-mcp-5giu.onrender.com`, and the MCP endpoint is
`https://trainingpeaks-mcp-5giu.onrender.com/mcp`. It uses the free instance
type. The remote build is verified by running Python tests, Ruff, and mypy.
After the login secrets are saved, Render restarts and validates the
TrainingPeaks session before serving MCP traffic.

An existing, functioning `TP_AUTH_COOKIE` remains a supported legacy
alternative. You do not need to set it when using the username/password
method.

The free service sleeps after 15 idle minutes; a paid plan avoids cold starts.

## 3. Connect ChatGPT (browser)

Follow the current ChatGPT custom MCP plugin workflow (the older Developer Mode
path has been superseded for eligible accounts):

1. In ChatGPT web, open **Plugins** and choose **+ → Add custom MCP server**.
2. Name it **TrainingPeaks**. Set the server URL to
   `https://<your-service>.onrender.com/mcp`. Choose **OAuth** authentication,
   with dynamic client registration (no preassigned client ID/secret).
3. Acknowledge the custom-server risk warning, then choose **Create as a plugin**.
4. During connection, your server asks for `MCP_OWNER_PASSWORD` from Render.
   Enter it on your server's approval page, not in the chat.
5. Install the new plugin and @mention **TrainingPeaks** to ask for workouts.

**ChatGPT Pro limitation:** the published Help Center currently documents Pro
custom MCP access as read/fetch only, while Business and Enterprise/Edu support
full read/write MCP actions. The server supports an optional write mode, but
enabling that does not override the client's plan restrictions.

## Upkeep

- **Session expired**: when `TP_USERNAME` and `TP_PASSWORD` are configured, the server automatically logs in again. If login fails (e.g. a changed password or SSO challenge), review the Render logs and update the secrets.
- **Disconnect every client**: on the Environment tab, generate a new `MCP_SIGNING_KEY` and save. Reconnect from
  ChatGPT afterwards.
- **Change the owner password**: edit `MCP_OWNER_PASSWORD` (at least 16 characters) and save.

## Settings reference

| Variable | Required | Meaning |
|----------|----------|---------|
| `TP_USERNAME` | preferred (with password) | TrainingPeaks login username; may differ from email |
| `TP_PASSWORD` | preferred (with username) | TrainingPeaks login password, stored privately in Render |
| `TP_AUTH_COOKIE` | optional alternative | Legacy session-cookie login; not needed if username/password provided |
| `MCP_OWNER_PASSWORD` | yes, 16+ characters | Approves a ChatGPT connection |
| `MCP_SIGNING_KEY` | yes, 32+ characters | Signs OAuth clients and tokens |
| `TP_MCP_ENABLE_WRITES` | no (default false) | Enable 17 non-delete coaching-write tools on supported clients |
| `MCP_PUBLIC_URL` | no | Public base URL; Render's `RENDER_EXTERNAL_URL` is used when unset (set it for a custom domain) |
| `PORT` | set by Render | Listen port |


## Optional coaching write mode

Keep the default TP_MCP_ENABLE_WRITES=false for ChatGPT Pro read/fetch use. If using
a client or plan that supports write-enabled custom MCP, set the Render environment
variable TP_MCP_ENABLE_WRITES=true and redeploy, then reconnect the app with
the write authorization.

The opt-in write set supports creating/editing/copying/reordering workouts,
creating/updating strength sessions, adding workout and calendar-note comments,
setting workout notes, creating/editing notes and race events, and creating/editing
workout-library templates and scheduling library workouts for the owner.

The server still blocks all deletes, file uploads, pairing/unpairing,
threshold/zone updates, bulk training-plan applications, and cross-athlete writes.
It removes the athlete-targeting argument from advertised tools and refuses
explicit athlete-targeting arguments at dispatch time.

Because create/comment/copy actions are non-idempotent, avoid retries after
ambiguous network failures without first fetching the target date or comments.
The client should request user confirmation before sending any coaching write.
The server does not independently implement a per-write approval dialog.
