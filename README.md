# garmin-mcp

An MCP server that gives MCP clients read-only access to one person's Garmin
Connect account (activities, sleep, heart rate, training metrics and so on). It
serves the MCP streamable HTTP transport on the home network and runs as a
sandboxed systemd service on a Debian 13 machine, without containers.

It talks to Garmin through the unofficial
[`garminconnect`](https://github.com/cyberjunky/python-garminconnect) library,
which uses Garmin's private API. Garmin changes that API from time to time, and
logins or individual tools can then stop working until the library catches up.
The fix is to upgrade the dependency and reinstall; see [Upgrade](#upgrade).

## Install

Requirements: Debian 13 with systemd, root access, and outbound HTTPS (PyPI
while installing, Garmin while running). The installer uses the distribution's
Python (3.13; anything from 3.12 works) and installs `python3-venv` with apt if
it is missing.

```sh
git clone <repo-url> garmin-mcp
cd garmin-mcp
sudo ./install.sh
```

The installer creates the `garmin-mcp` system user, builds the package into a
virtualenv, installs the unit, starts the service, and checks that it answers
on `/healthz`. It ends by printing the steps that are still open. They are:

1. Note the bearer token. A first install generates a random
   `GARMIN_MCP_AUTH_TOKEN` in `/etc/garmin-mcp/garmin-mcp.env`, so the endpoint
   never starts open. Read it with
   `sudo grep AUTH_TOKEN /etc/garmin-mcp/garmin-mcp.env`. To change it, edit
   the file (`sudoedit /etc/garmin-mcp/garmin-mcp.env`) and run
   `sudo systemctl restart garmin-mcp`.
2. Log in to Garmin once: `sudo garmin-mcp login`. It asks for email, password
   and, if the account has MFA, the code. See
   [Authenticating with Garmin](#authenticating-with-garmin) for the
   alternative of storing the credentials.
3. Verify:

   ```sh
   curl http://<server-ip>:8765/healthz     # {"status":"ok","version":"..."}
   sudo garmin-mcp check                    # OK: logged in as <your name>

   # List the tools through the MCP endpoint itself:
   curl -s http://<server-ip>:8765/mcp \
     -H 'Authorization: Bearer <token>' \
     -H 'Content-Type: application/json' \
     -H 'Accept: application/json, text/event-stream' \
     -d '{"jsonrpc":"2.0","id":1,"method":"tools/list"}'
   ```

What ends up where:

| Path | Contents |
| --- | --- |
| `/opt/garmin-mcp/venv` | Virtualenv holding a copy of the package and its dependencies. The checkout is not used at runtime. |
| `/etc/garmin-mcp/garmin-mcp.env` | Settings and secrets, `root:garmin-mcp`, mode 0640. Created on first install, never overwritten. |
| `/etc/garmin-mcp/garmin-mcp.env.example` | Current reference copy of the settings file, refreshed on every install. |
| `/var/lib/garmin-mcp/tokens` | Garmin tokens. Owned by `garmin-mcp`, mode 0700. |
| `/etc/systemd/system/garmin-mcp.service` | The unit. Overwritten on every install; put local changes in a drop-in (`sudo systemctl edit garmin-mcp`). |
| `/usr/local/bin/garmin-mcp` | Admin wrapper for `sudo garmin-mcp login` and `sudo garmin-mcp check`. |

## Configuration

All settings are environment variables in `/etc/garmin-mcp/garmin-mcp.env`.
Every one is optional. Restart the service after a change
(`sudo systemctl restart garmin-mcp`).

| Variable | Default | Meaning |
| --- | --- | --- |
| `GARMIN_EMAIL` | unset | Garmin account email. Only needed if the service should log in by itself. |
| `GARMIN_PASSWORD` | unset | Garmin account password, stored in clear text. Only needed together with `GARMIN_EMAIL`. |
| `GARMIN_IS_CN` | `false` | `true` for accounts on Garmin's China region. |
| `GARMIN_TOKEN_DIR` | `/var/lib/garmin-mcp/tokens` | Where tokens are saved. Under systemd the default is `$STATE_DIRECTORY/tokens`; outside systemd it is `~/.garminconnect`. Leave unset on the server: the sandbox allows writes nowhere else. |
| `GARMIN_MCP_LOGIN_COOLDOWN` | `300` | Seconds to wait after a failed Garmin login before trying again. |
| `GARMIN_MCP_AUTH_TOKEN` | unset | Shared secret. When set, every MCP request needs `Authorization: Bearer <token>`. Unset means no authentication. |
| `GARMIN_MCP_ALLOWED_HOSTS` | unset | Comma-separated allowlist for the HTTP `Host` header, entries of the form `host:port` or `host:*`. Unset accepts any host. |
| `GARMIN_MCP_HOST` | `0.0.0.0` | Address to bind. |
| `GARMIN_MCP_PORT` | `8765` | TCP port. |
| `GARMIN_MCP_PATH` | `/mcp` | URL path of the MCP endpoint. |
| `GARMIN_MCP_LOG_LEVEL` | `INFO` | `DEBUG`, `INFO`, `WARNING` or `ERROR`. `DEBUG` also logs each HTTP request. |

The file is a systemd `EnvironmentFile`, not a shell script: nothing is
expanded, and there are no trailing comments. Put a value that contains special
characters in single quotes (`GARMIN_PASSWORD='p@ss #1 $x'`). The comments in
the file give the full quoting rules.

## Authenticating with Garmin

The service needs saved tokens or credentials. There are two ways to provide
them:

- **One-time login (recommended).** Run `sudo garmin-mcp login`. It prompts for
  email, password and MFA code, saves tokens to `/var/lib/garmin-mcp/tokens`,
  and restarts the service. No password is stored on disk. Accounts with MFA
  must use this, because the background service cannot answer the MFA prompt.
- **Stored credentials.** Set `GARMIN_EMAIL` and `GARMIN_PASSWORD` in the env
  file. The service logs in on the first tool call and saves tokens the same
  way. This only works for accounts without MFA.

The service always tries the saved tokens first. It refreshes them as they
expire and writes the new ones back to the token directory, so you should
rarely need to log in again. `sudo garmin-mcp check` tells you whether the
saved tokens (or the stored credentials) currently work.

`sudo garmin-mcp login` and `check` run the installed program the way the
service runs it: as the `garmin-mcp` user, with the env file loaded and the
same token directory. If `GARMIN_EMAIL` or `GARMIN_PASSWORD` is set in the env
file, `login` uses it instead of prompting.

When the tokens stop working, tool calls return an error that says so:

- With stored credentials and no MFA, the service logs in again by itself.
- Otherwise run `sudo garmin-mcp login` again.

After a failed login the service does not contact Garmin again for
`GARMIN_MCP_LOGIN_COOLDOWN` seconds (default 300), so that a wrong password or
a Garmin rate limit does not turn into a series of login attempts. During that
time tool calls return the last login error and the remaining wait. Restarting
the service clears the wait; a successful `sudo garmin-mcp login` restarts it
for you. If Garmin is rate-limiting logins (HTTP 429), wait before you try
again rather than repeating the login.

## Connecting clients

The endpoint is `http://<server-ip>:8765/mcp`, using the MCP streamable HTTP
transport. The server is stateless, so clients keep working across service
restarts. If `GARMIN_MCP_AUTH_TOKEN` is set, each request must carry
`Authorization: Bearer <token>`.

Claude Code:

```sh
claude mcp add --transport http garmin http://<server-ip>:8765/mcp \
  --header "Authorization: Bearer <token>"
```

Clients configured through an `mcpServers` JSON block:

```json
{
  "mcpServers": {
    "garmin": {
      "type": "http",
      "url": "http://<server-ip>:8765/mcp",
      "headers": {
        "Authorization": "Bearer <token>"
      }
    }
  }
}
```

Any client that supports streamable HTTP with custom headers can connect this
way. A client that only accepts HTTPS or publicly reachable URLs needs a
reverse proxy with TLS in front of the service; that is outside the scope of
this repository. With a proxy on the same machine, set
`GARMIN_MCP_HOST=127.0.0.1` so that only the proxy can reach the service.

## Security notes

- **Plain HTTP.** Requests, responses and the bearer token cross the LAN
  unencrypted. That is acceptable on a home network you trust and nowhere else.
  Do not forward the port to the internet.
- **Bearer token.** Without `GARMIN_MCP_AUTH_TOKEN`, anyone who can reach the
  port can read the account's health data. The service logs a warning at
  startup while the token is unset. `/healthz` is always open and returns only
  a status and the version.
- **Bind address and firewall.** The default `0.0.0.0` listens on every IPv4
  interface. Set `GARMIN_MCP_HOST` to one LAN address to narrow that, and
  restrict the port to the clients that need it in the machine's firewall.
- **Host allowlist.** `GARMIN_MCP_ALLOWED_HOSTS` rejects requests whose `Host`
  header is not listed, which protects against DNS rebinding from a browser on
  the LAN. List every name and address that clients use in the URL, for
  example `192.168.1.10:*,garmin.lan:*`. With the allowlist on, requests that
  carry an `Origin` header (browsers send one) are rejected too.
- **Read-only.** The tools only read from Garmin Connect. None of them changes
  anything in the account.
- **Secrets on disk.** The env file (bearer token, optionally the password) is
  readable by root and the `garmin-mcp` group only. The token file grants
  access to the Garmin account and is readable by the `garmin-mcp` user only.
- **Sandbox.** The service runs as an unprivileged user with no capabilities,
  a read-only view of the file system apart from `/var/lib/garmin-mcp`, no
  access to `/home`, and a system-call filter. The settings are in
  `deploy/garmin-mcp.service`; `systemd-analyze security garmin-mcp` shows the
  result.

## Operations

```sh
systemctl status garmin-mcp          # state and the last log lines
sudo systemctl restart garmin-mcp    # apply a config change
journalctl -u garmin-mcp -f          # follow the log
journalctl -u garmin-mcp -e          # jump to the end of the log
sudo garmin-mcp check                # do the Garmin tokens work?
```

The unit restarts the service 15 seconds after a crash, indefinitely. A service
that keeps restarting usually has a bad value in the env file; the log shows
the Python error.

### Upgrade

```sh
git pull
sudo ./install.sh
```

Re-running the installer rebuilds the package in the virtualenv, replaces the
unit and the wrapper, and restarts the service. It keeps the env file and the
tokens. Compare your env file with
`/etc/garmin-mcp/garmin-mcp.env.example` to see settings that a new version
added.

By default the installer only upgrades dependencies that no longer satisfy
`pyproject.toml`. When Garmin has changed its API and a newer `garminconnect`
fixes it, pull in the newest allowed versions of all dependencies with:

```sh
sudo ./install.sh --upgrade-deps
```

If the fix is in a release outside the pinned range (`garminconnect>=0.3.20,<0.4`),
change the pin in `pyproject.toml` first.

The installer builds the virtualenv from `/usr/bin/python3`. Set `PYTHON` to
use another interpreter (`sudo PYTHON=/path/to/python3 ./install.sh`).

### Uninstall

```sh
sudo ./uninstall.sh            # remove the service, /opt/garmin-mcp and the wrapper
sudo ./uninstall.sh --purge    # also remove the config, the tokens and the garmin-mcp user
```

Without `--purge` the config and tokens stay in `/etc/garmin-mcp` and
`/var/lib/garmin-mcp`, and a later install picks them up again.

## Development

```sh
uv sync                      # create .venv with the dev dependencies
uv run pytest                # tests
uv run ruff check            # lint
uv run garmin-mcp login      # one-time Garmin login; tokens go to ~/.garminconnect
uv run garmin-mcp serve      # serve on http://0.0.0.0:8765/mcp
```

Outside systemd the settings come from the shell environment, for example
`GARMIN_MCP_HOST=127.0.0.1 GARMIN_MCP_PORT=8766 uv run garmin-mcp serve`.

Code layout:

- `src/garmin_mcp/config.py` reads the settings from the environment.
- `src/garmin_mcp/session.py` holds the single Garmin session: login, token
  reuse, the login cooldown, and the mapping of Garmin errors to tool errors.
- `src/garmin_mcp/server.py` builds the MCP server and the HTTP app (bearer
  token check, `/healthz`).
- `src/garmin_mcp/tools/` contains the tools, one module per area. Each module
  exposes `register(mcp, garmin)` and is listed in `MODULES` in
  `tools/__init__.py`. The conventions for writing a tool (date arguments,
  calling Garmin only through `garmin.call(...)`, returning `dump(payload)`)
  are described at the top of `src/garmin_mcp/tools/_common.py`.
- `deploy/`, `install.sh` and `uninstall.sh` are the systemd deployment.

## Tools

59 tools, all read-only. Dates are `YYYY-MM-DD`, `today`, `yesterday`,
`tomorrow`, `<N>d` (N days ago) or `+<N>d` (N days ahead), in the Garmin
account's timezone. Results are compact JSON with nulls stripped, and bulky
time series are left out unless a tool parameter asks for them. Each tool's
full description and parameters are served to clients through `tools/list`.

### Profile, devices, gear and achievements

| Tool | What it covers |
| --- | --- |
| `get_user_profile` | Who the Garmin account belongs to |
| `get_devices` | List the Garmin devices registered to the account (watches, bike computers, scales) with model, firmware version and device id |
| `get_device_last_used` | The device that most recently synced with Garmin Connect and when |
| `list_gear` | The user's gear (shoes, bikes, ...) |
| `get_gear_activities` | Usage of one piece of gear |
| `get_personal_records` | The user's personal records as Garmin tracks them |
| `get_goals` | The goals the user set up on Garmin Connect's Goals page |
| `get_recent_badges` | The badges the user earned most recently in Garmin Connect |

### Activities

| Tool | What it covers |
| --- | --- |
| `list_activities` | List recorded activities (workouts), newest first, as compact summaries |
| `get_activity` | Full summary of one activity |
| `get_activity_splits` | Per-lap breakdown of one activity |
| `get_activity_zones` | Time spent in each heart-rate (or power) zone during one activity |
| `get_activity_weather` | Weather Garmin recorded for one activity from the nearest station |
| `get_activity_details` | Time series of one activity, downsampled |
| `get_activity_exercise_sets` | Sets of a strength-training activity in order |
| `get_activity_gear` | Gear (shoes, bike, ...) linked to one activity |
| `get_activity_types` | Garmin's activity type keys as a tree |
| `get_progress_summary` | Totals for a date range |

### Daily health and wellness

| Tool | What it covers |
| --- | --- |
| `get_daily_summary` | One day's headline wellness numbers in a single call |
| `get_daily_summaries` | Compare days side by side |
| `get_sleep` | The night of sleep that ended on the given date |
| `get_sleep_trend` | Sleep night by night over a date range |
| `get_heart_rate` | Heart rate for one day |
| `get_resting_heart_rate` | Resting heart rate per day over a date range, with the average, lowest and highest for the period |
| `get_hrv` | Overnight heart rate variability (HRV), one row per night |
| `get_stress` | Stress for one day |
| `get_body_battery` | Body Battery (Garmin's energy-reserve score) per day |
| `get_body_battery_events` | What moved Body Battery on one day |
| `get_respiration` | Breathing rate for one day |
| `get_spo2` | Blood oxygen saturation (pulse ox, SpO2) for one day |
| `get_steps` | Step count per day over a date range, with each day's step goal and distance |
| `get_intensity_minutes` | Intensity minutes (moderate and vigorous activity time) |
| `get_floors` | Floors climbed and descended per day over a date range, with the daily floors goal |
| `get_hydration` | Fluid intake logged for one day against the daily hydration goal, with Garmin's estimated sweat loss |

### Training and performance metrics

| Tool | What it covers |
| --- | --- |
| `get_training_readiness` | Training Readiness |
| `get_training_status` | Training status as of a date |
| `get_vo2max` | VO2 max |
| `get_race_predictions` | Garmin's predicted race times for 5K, 10K, half marathon and marathon |
| `get_endurance_score` | Endurance Score |
| `get_hill_score` | Hill Score |
| `get_fitness_age` | Fitness Age as of a date |
| `get_lactate_threshold` | Running lactate threshold |
| `get_cycling_ftp` | Cycling Functional Threshold Power (FTP) |
| `get_heart_rate_zones` | The configured heart rate zones for each sport profile |
| `get_power_zones` | The configured power zones for every sport that has them (cycling, running) |
| `get_power_curve` | Power curve |

### Body

| Tool | What it covers |
| --- | --- |
| `get_body_composition` | Weigh-ins over a date range |
| `get_latest_weight` | The most recent weigh-in on or before a date |
| `get_blood_pressure` | Blood pressure readings over a date range |

### Workouts, calendar and training plans

| Tool | What it covers |
| --- | --- |
| `list_workouts` | List the saved workouts in the user's workout library (structured workout templates, not recorded activities) as compact summaries |
| `get_workout` | Full structure of one saved workout |
| `get_training_calendar` | What is on the Garmin Connect calendar between two dates |
| `get_scheduled_workouts` | Workouts scheduled between two dates |
| `get_next_scheduled_workout` | The next workout on the calendar (today or later, within this and next month) |
| `get_upcoming_events` | Upcoming races and goal events on the user's calendar (name, date, event type, location, distance / time goal), soonest first, plus the primary ("A race") event Garmin bases training guidance on |
| `list_training_plans` | The user's Garmin training plans (Garmin Coach and other plans they have joined), each with its trainingPlanId, name, category (`FBT_ADAPTIVE` marks an adaptive Garmin Coach plan), sport/type, level, status and start/end dates |
| `get_training_plan` | Details of one training plan |
| `list_courses` | The user's saved courses (routes to follow on a device) |
| `get_course` | One saved course |
