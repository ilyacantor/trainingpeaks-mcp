# Remote read-only server on Render, connected to ChatGPT

This runs the TrainingPeaks MCP server on Render as a web service at `https://<your-service>.onrender.com/mcp`,
for ChatGPT. Everything below happens in a browser; no terminal is needed.

The local stdio server (`tp-mcp serve`) is unchanged. The remote server (`tp-mcp serve-http`) is separate and
read-only.

## What the remote server exposes

22 read-only tools. No tool that creates, changes or deletes anything is offered, and a call to any other tool
name is refused.

| Area | Tools |
|------|-------|
| Workouts | `tp_get_workouts`, `tp_get_workout`, `tp_get_strength_workouts`, `tp_get_strength_workout`, `tp_get_strength_summary` |
| Workout analysis | `tp_analyze_workout`, `tp_get_workout_prs`, `tp_get_peaks` |
| Comments and notes | `tp_get_workout_comments`, `tp_get_workout_note`, `tp_list_notes`, `tp_get_note`, `tp_get_note_comments` |
| Fitness history | `tp_get_fitness`, `tp_get_weekly_summary` |
| Athlete settings | `tp_get_athlete_settings`, `tp_get_pool_length_settings` |
| Metrics | `tp_get_metrics`, `tp_get_nutrition` |
| Events | `tp_get_events`, `tp_get_next_event`, `tp_get_focus_event` |

Not exposed: every create/update/delete tool, `tp_refresh_auth` (browser cookie extraction), `tp_auth_status`,
file download/upload, libraries, training plans, groups and equipment.

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
- `TP_AUTH_COOKIE` is read from Render's environment. It is never logged and never returned by any endpoint.

## 1. Get your TrainingPeaks cookie (browser)

1. Log in at https://app.trainingpeaks.com.
2. Open the browser's developer tools (F12, or right-click the page and choose Inspect), then go to
   **Application** (Chrome/Edge) or **Storage** (Firefox), then **Cookies**, then `https://app.trainingpeaks.com`.
3. Copy the **value** of `Production_tpAuth`. Treat it like a password: it grants full access to your account.

## 2. Deploy on Render (browser)

1. Open https://dashboard.render.com/blueprints and click **New Blueprint Instance**.
2. Connect GitHub if asked, and pick the repository `ilyacantor/trainingpeaks-mcp`, branch `main`.
3. Render reads `render.yaml` and shows one web service, `trainingpeaks-mcp`. When it asks for
   `TP_AUTH_COOKIE`, paste the cookie value from step 1. `MCP_OWNER_PASSWORD` and `MCP_SIGNING_KEY` are
   generated for you.
4. Click **Apply**. Wait until the service shows **Live**. Its address is at the top of the service page, for
   example `https://trainingpeaks-mcp-abcd.onrender.com`. Your MCP URL is that address plus `/mcp`.
5. Open the service's **Environment** tab and reveal `MCP_OWNER_PASSWORD`. You will paste it once, when
   ChatGPT connects.

If the deploy fails, open **Logs**. The server refuses to start, with a plain message, when a setting is missing or
TrainingPeaks rejects the cookie (for example
`TrainingPeaks rejected TP_AUTH_COOKIE at startup (expired: ...)`).

The `free` plan sleeps after 15 idle minutes, so the first request after a sleep waits for the service to wake. To
avoid that, change the instance type to a paid one under **Settings**.

## 3. Connect ChatGPT (browser)

ChatGPT moves these menus from time to time; the labels below were current in October 2026. Custom MCP apps need
**Developer mode**, which is available on some ChatGPT plans only, and a workspace admin can turn it off.

1. In ChatGPT, open **Settings** → **Apps** (in some versions **Apps & Connectors**) → **Advanced settings**, and
   turn on **Developer mode**.
2. Go back and choose **Create app**.
3. Name: `TrainingPeaks`. MCP server URL: `https://<your-service>.onrender.com/mcp`. Authentication: **OAuth**.
   Leave the client ID and secret empty; ChatGPT registers itself. Confirm the custom-app warning, then
   **Create**.
4. ChatGPT opens the "Connect ChatGPT to your TrainingPeaks" page from your server. Paste `MCP_OWNER_PASSWORD` and
   click **Approve**. ChatGPT returns and lists the 22 tools.
5. In a chat, select the TrainingPeaks app (or mention it by name) and ask, for example: "List my completed
   workouts from the last 90 days."

## Upkeep

- **Cookie expired** (tools answer with authentication errors, or a restart fails with "rejected TP_AUTH_COOKIE"):
  repeat step 1, paste the new value into `TP_AUTH_COOKIE` on the **Environment** tab, and save. Render redeploys.
- **Disconnect every client**: on the Environment tab, generate a new `MCP_SIGNING_KEY` and save. Reconnect from
  ChatGPT afterwards.
- **Change the owner password**: edit `MCP_OWNER_PASSWORD` (at least 16 characters) and save.

## Settings reference

| Variable | Required | Meaning |
|----------|----------|---------|
| `TP_AUTH_COOKIE` | yes | TrainingPeaks `Production_tpAuth` cookie value |
| `MCP_OWNER_PASSWORD` | yes, 16+ characters | Approves a ChatGPT connection |
| `MCP_SIGNING_KEY` | yes, 32+ characters | Signs OAuth clients and tokens |
| `MCP_PUBLIC_URL` | no | Public base URL; Render's `RENDER_EXTERNAL_URL` is used when unset (set it for a custom domain) |
| `PORT` | set by Render | Listen port |
