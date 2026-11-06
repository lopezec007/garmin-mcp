# garmin-mcp

An MCP server that gives MCP clients read-only access to one person's Garmin
Connect account (activities, sleep, heart rate, training metrics and so on). It
serves the MCP streamable HTTP transport and runs as a sandboxed systemd
service on a Debian 13 machine, without containers.

There are two ways in, and both are on out of the box. A client can send a
bearer token in a header. Or it can sign in with OAuth: the server is its own
OAuth 2.1 authorization server, and every client has to be approved in a
browser with a password that only the owner knows. OAuth needs no
configuration and no public address, but most clients only do it over HTTPS;
see [Connecting clients](#connecting-clients). To open the server to the
internet, put a TLS reverse proxy in front of it; see
[Public access with OAuth](#public-access-with-oauth).

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

That is a complete setup for the home network. To use the server from outside
it, continue with [Public access with OAuth](#public-access-with-oauth).

What ends up where:

| Path | Contents |
| --- | --- |
| `/opt/garmin-mcp/venv` | Virtualenv holding a copy of the package and its dependencies. The checkout is not used at runtime. |
| `/etc/garmin-mcp/garmin-mcp.env` | Settings and secrets, `root:garmin-mcp`, mode 0640. Created on first install, never overwritten. |
| `/etc/garmin-mcp/garmin-mcp.env.example` | Current reference copy of the settings file, refreshed on every install. |
| `/var/lib/garmin-mcp/tokens` | Garmin tokens. Owned by `garmin-mcp`, mode 0700. |
| `/var/lib/garmin-mcp/oauth.db` | OAuth state: registered clients (including, for clients that have one, their client secret), hashed tokens and the hash of the owner password. An SQLite database; the `oauth.db-wal` and `oauth.db-shm` files next to it are part of it. Owned by `garmin-mcp`, mode 0600. Created when OAuth is first used. |
| `/etc/systemd/system/garmin-mcp.service` | The unit. Overwritten on every install; put local changes in a drop-in (`sudo systemctl edit garmin-mcp`). |
| `/usr/local/bin/garmin-mcp` | Admin wrapper for `sudo garmin-mcp login`, `check`, `set-password`, `clients` and `revoke`. |

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
| `GARMIN_MCP_AUTH_TOKEN` | unset | Shared secret: an MCP request that carries `Authorization: Bearer <token>` is let in. It works next to OAuth; unset leaves OAuth as the only way in. With `GARMIN_MCP_OAUTH=off`, unset means no authentication at all. Must be at least 32 characters when `GARMIN_MCP_PUBLIC_URL` is set. |
| `GARMIN_MCP_OAUTH` | `on` | The built-in OAuth sign-in. `off` removes the OAuth endpoints and leaves the bearer token as the only protection. Any other value is a configuration error. |
| `GARMIN_MCP_ALLOWED_HOSTS` | unset | Comma-separated allowlist for the HTTP `Host` header on the MCP endpoint. An entry is `host:port`, `host:*` (any port, but the header must have one) or a bare `host` (a name used without a port, such as the public name behind a TLS proxy). Unset accepts any host. |
| `GARMIN_MCP_PUBLIC_URL` | unset | Optional. The origin the server is reachable at, for example `https://garmin.example.com`: scheme and host name, no path. Unset, the server names itself in the OAuth discovery documents by the address each request was sent to, so it works under any address without configuration. Set, it always uses this value, whatever a request claims; do that once the server is on the internet. Must be `https` (plain `http` is accepted for `localhost` only). With a value that is not usable the service exits with a configuration error. |
| `GARMIN_MCP_TRUSTED_PROXIES` | `127.0.0.1` | Comma-separated addresses or networks of the reverse proxies whose `X-Forwarded-For` header is believed. Only needs changing when the proxy runs on another machine. |
| `GARMIN_MCP_OAUTH_ACCESS_TTL` | `3600` | Lifetime of an OAuth access token in seconds (at least 60). |
| `GARMIN_MCP_OAUTH_REFRESH_TTL` | `2592000` | Lifetime of an OAuth refresh token in seconds (30 days; not shorter than the access token's). |
| `GARMIN_MCP_OAUTH_DB` | `/var/lib/garmin-mcp/oauth.db` | The OAuth database. Under systemd the default is `$STATE_DIRECTORY/oauth.db`; outside systemd it is `~/.local/state/garmin-mcp/oauth.db`. Leave unset on the server, for the same reason as `GARMIN_TOKEN_DIR`. |
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

None of this has anything to do with the owner password of
[Public access with OAuth](#public-access-with-oauth): that one decides who may
use this server, and Garmin never sees it.

## Connecting clients

The server speaks the MCP streamable HTTP transport and is stateless, so
clients keep working across service restarts. There are two ways for a client
to get in.

**Bearer token, on the home network.** The endpoint is
`http://<server-ip>:8765/mcp`. If `GARMIN_MCP_AUTH_TOKEN` is set, each request
must carry `Authorization: Bearer <token>`.

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
way.

**OAuth.** An MCP client that implements the MCP authorization flow (OAuth 2.1
with dynamic client registration) needs nothing but the endpoint URL:

```sh
claude mcp add --transport http garmin https://garmin.example.com/mcp
```

The client discovers the sign-in endpoints from the URL, registers itself, and
sends your browser to the approval page, where you type the owner password
(set it first: `sudo garmin-mcp set-password`). See
[The approval page](#the-approval-page).

The server offers this under whatever address it is reached at, the LAN
address included. Whether a client will use it there is up to the client, and
the ones from Anthropic currently will not over plain HTTP:

- **Claude Code** runs the OAuth flow, but, according to its documentation,
  sends OAuth credentials only to a token endpoint that is HTTPS or on
  `localhost`. Against `http://<server-ip>:8765/mcp` the sign-in stops with
  "Refusing to send credentials to non-https token endpoint". The documented
  way around it is the environment variable `MCP_SDK_GENERATION=v1`; the
  documentation recommends serving HTTPS instead. Note also that Claude Code
  does not try OAuth for a server that has an `Authorization` header
  configured: use one method or the other.
- **claude.ai, Claude Desktop and the mobile apps** (custom connectors)
  connect from Anthropic's servers, not from your device. They cannot reach
  an address that only exists on your network, whatever it serves.

So in practice: on the plain-HTTP LAN address use the header; OAuth becomes
useful once the server has HTTPS in front of it, which for the hosted clients
also has to be reachable from the internet. The header method keeps working
next to OAuth for as long as `GARMIN_MCP_AUTH_TOKEN` is set.

## Public access with OAuth

The server is an OAuth 2.1 authorization server for itself (unless
`GARMIN_MCP_OAUTH=off`). It serves, next to `/mcp`:

- the discovery documents `/.well-known/oauth-protected-resource/mcp` and
  `/.well-known/oauth-authorization-server`,
- `/register` (dynamic client registration), `/authorize`, `/token` and
  `/revoke`,
- the approval page `/oauth/login`.

A client that is given the endpoint URL works out the rest by itself. What you
do is approve it: the browser opens the approval page, you type the **owner
password** and click Approve. The owner password is a password for this server
only. It is not the Garmin password, and it is set on the server with
`sudo garmin-mcp set-password`.

### Prerequisites

- **A DNS name** for the server, for example `garmin.example.com`, that
  resolves to the machine (or to the tunnel, see below). The server needs the
  whole host name: it cannot live under a sub-path such as
  `https://example.com/garmin/`.
- **TLS in front of the server.** The server itself still speaks plain HTTP on
  port 8765. Something has to terminate HTTPS and pass the requests on:

  - A reverse proxy with a certificate on the same machine. Caddy takes the
    least effort on Debian: `sudo apt install caddy`, put this in
    `/etc/caddy/Caddyfile`, and run `sudo systemctl reload caddy`.

    ```
    garmin.example.com {
        reverse_proxy 127.0.0.1:8765
    }
    ```

    Caddy gets and renews a Let's Encrypt certificate by itself. For that the
    name must resolve to the machine, and ports 80 and 443 must be reachable
    from the internet (forward them on the router; do not forward 8765).
  - Or a tunnel service that terminates TLS on its side and forwards to
    `http://127.0.0.1:8765`. No inbound port is needed then. Follow the
    service's own documentation; the requirements below are the same.

  Whatever sits in front must forward every path at the root of the host name
  unchanged (no prefix, no rewriting), keep the `Host` header, and put the
  client's address in `X-Forwarded-For`. Caddy's `reverse_proxy` does all of
  that without further settings.

### Setup

1. Edit `/etc/garmin-mcp/garmin-mcp.env`:

   ```
   GARMIN_MCP_PUBLIC_URL=https://garmin.example.com
   GARMIN_MCP_HOST=127.0.0.1
   ```

   `GARMIN_MCP_PUBLIC_URL` is the bare origin: no path and no trailing `/mcp`.
   OAuth works without it, because the server otherwise goes by the address
   each request arrives under. Set it anyway once the server is public: then
   the addresses it publishes no longer depend on what a request claims.
   `GARMIN_MCP_HOST=127.0.0.1` makes the plain-HTTP port reachable through the
   local proxy only. Leave it at `0.0.0.0` only if clients on the LAN still
   connect to port 8765 directly with the bearer token. If you use
   `GARMIN_MCP_ALLOWED_HOSTS`, add the public name to it as a bare
   `garmin.example.com`.

   The proxy on the same machine is trusted to report client addresses by
   default. If the proxy runs on another machine, also set
   `GARMIN_MCP_TRUSTED_PROXIES` to its address.

2. Set the owner password:

   ```sh
   sudo garmin-mcp set-password
   ```

   It asks twice and wants at least 12 characters. Only a hash is stored (in
   `/var/lib/garmin-mcp/oauth.db`). When it is not run from a terminal it
   reads the password as one line from standard input instead. Run it again
   at any time to change the password: the change applies at once, without a
   restart, and does not sign out clients that are already authorized.

3. Restart and look at the log:

   ```sh
   sudo systemctl restart garmin-mcp
   journalctl -u garmin-mcp -e
   ```

   It should say `OAuth enabled: public endpoint https://garmin.example.com/mcp`.
   A warning that no owner password is set means step 2 is still open; until
   then nobody can authorize a client.

4. Check from a machine outside your network:

   ```sh
   curl https://garmin.example.com/healthz
   curl https://garmin.example.com/.well-known/oauth-protected-resource/mcp
   curl https://garmin.example.com/.well-known/oauth-authorization-server
   curl -i -X POST https://garmin.example.com/mcp
   ```

   The first document must name `"resource":"https://garmin.example.com/mcp"`,
   exactly the URL you will give to clients. Every URL in the second must
   start with `https://garmin.example.com`. The last command must answer
   `401` with a `WWW-Authenticate` header that points at the first document:
   that is how clients find the sign-in.

5. Add the server to a client by URL only, as shown under
   [Connecting clients](#connecting-clients), and approve it.

### The approval page

When a client asks for access, your browser shows a page with:

- the name of the application. The application picks that name itself when it
  registers, and nothing verifies it: anything can call itself anything.
- where the browser will be sent after approval. This is the part to read,
  because the code that grants access goes there and nowhere else. For a
  hosted service it is that service's own domain; for a program on your own
  computer it is `localhost` or `127.0.0.1` with a port. Only the host is
  shown, so it cannot be dressed up with a misleading prefix. For an app with
  its own URL scheme (`cursor://...`) the whole link is shown instead, with a
  note that it is not a web address.
- how long ago the request was started.
- a field for the owner password, and the buttons Approve and Deny.

Approve only when you started the connection yourself a moment ago and the
destination fits the application you are connecting. If the page appears
without your doing, or the destination is a domain you do not recognise, click
Deny or close it. **A link to the approval page that someone sends you is
always an attack**, however genuine the page looks: the page is real, but the
access would go to whoever started that request. Nothing is granted without
the password.

The page is valid for 5 minutes and for one approval. There is no login
session: every approval asks for the password again. Approving a client that
was already authorized replaces its earlier sign-in.

### Managing clients

```sh
sudo garmin-mcp clients              # list the authorized clients
sudo garmin-mcp revoke <client_id>   # sign one out and forget it
sudo garmin-mcp revoke --all         # sign out every client
```

`clients` prints, for each client you approved, its id, the name it gave
itself, when it was authorized, when it last got a token, until when it stays
signed in, and its redirect URIs. `revoke` takes the id from that list. A
revoked client is cut off at once and has to register and be approved again.
None of these commands needs a restart.

If the owner password may have leaked, set a new one first, then run
`sudo garmin-mcp revoke --all` and approve your clients again.

### Token lifetimes

- An access token, which the client sends with every request, lives 1 hour
  (`GARMIN_MCP_OAUTH_ACCESS_TTL`).
- A refresh token, with which the client gets the next access token without
  asking you, lives 30 days (`GARMIN_MCP_OAUTH_REFRESH_TTL`) and is replaced
  by a new one every time it is used. If a refresh token that was already
  replaced shows up again more than a minute later, it has been copied, and
  the server signs that client out entirely (it logs "Rotated refresh token
  replayed"); the client then has to be approved again.

So a client stays signed in for as long as it is used at least once every 30
days, and has to be approved again after a longer pause. Authorized clients
survive service restarts and upgrades. A changed lifetime applies to tokens
issued after the restart. The static `GARMIN_MCP_AUTH_TOKEN` is not an OAuth
token and never expires.

### Troubleshooting

- **`/mcp` answers 401.** Expected for a request without a valid token; it is
  the client's cue to start the sign-in. A client that reports only "401" or
  "unauthorized" probably does not implement the OAuth flow: use the bearer
  token header with it.
- **The discovery documents answer 404.** OAuth is switched off
  (`GARMIN_MCP_OAUTH=off` in `/etc/garmin-mcp/garmin-mcp.env`). With it on,
  the service logs `OAuth sign-in enabled` at startup. (Version 0.2.0 served
  them only while `GARMIN_MCP_PUBLIC_URL` was set, and answered 401 otherwise,
  which looked like the documents were behind the login.)
- **Claude Code says "Refusing to send credentials to non-https token
  endpoint".** It will not do OAuth against a plain-HTTP address other than
  `localhost`. Use the bearer token header on the LAN, or put HTTPS in front
  of the server; see [Connecting clients](#connecting-clients).
- **The page says "Not set up yet" / no owner password.** Run
  `sudo garmin-mcp set-password` on the server, then start connecting again
  from the client.
- **The page says "Too many attempts".** Five wrong passwords from one address
  lock that address out until 15 minutes after the first of them (see
  [Security notes](#security-notes)). Wait, or restart the service, which
  clears the counters. If it happens although you did not mistype, either
  someone else is guessing and the overall limit is reached, or other visitors
  are counted under your address because the proxy does not pass
  `X-Forwarded-For` or is not listed in `GARMIN_MCP_TRUSTED_PROXIES`. The
  log shows which.
- **The page says "Request expired".** The approval page was open for more
  than 5 minutes, was already used, or the service was restarted in between.
  Start connecting again from the client.
- **The client fails before any page opens, or the browser goes to the wrong
  host.** The discovery documents name a different address than the one the
  client uses. Compare the output of the `curl` commands in step 4 with the
  URL you gave the client: scheme, host name and path must be identical. With
  `GARMIN_MCP_PUBLIC_URL` set, the documents use that value, so fix the
  value. Without it they use the `Host` header and the scheme of the request:
  if they say `http` behind a TLS proxy, the proxy is not sending
  `X-Forwarded-Proto` or is not listed in `GARMIN_MCP_TRUSTED_PROXIES`; if
  the host is wrong, the proxy is rewriting `Host`. Setting
  `GARMIN_MCP_PUBLIC_URL` settles both.
- **The service does not start after the change.** `journalctl -u garmin-mcp -e`
  shows a line starting with `configuration error` that names the problem,
  usually a `GARMIN_MCP_PUBLIC_URL` with a path, or `http` instead of `https`.
- **The proxy serves the server under a sub-path** (for example
  `https://example.com/garmin/`). That cannot work: the discovery documents
  must be at `/.well-known/...` of the host, and a `GARMIN_MCP_PUBLIC_URL`
  with a path is refused. Give the server a host name of its own.
- **`/mcp` answers 421 "Invalid Host header".** `GARMIN_MCP_ALLOWED_HOSTS` is
  set and lacks the public name. Add it without a port
  (`garmin.example.com`); the `host:*` form does not match a name that
  arrives without a port.
- **A client that worked asks to sign in again.** It was not used for 30
  days, it was revoked, the same client was approved again elsewhere, one of
  its refresh tokens was replayed (see [Token lifetimes](#token-lifetimes)),
  or `/var/lib/garmin-mcp/oauth.db` was removed.
- **Registering a client fails with "too many pending registrations".**
  Someone is flooding `/register`. Registrations made in the last 15 minutes
  are never dropped to make room, so sign-ins already in progress finish, but
  new clients have to wait. Rate-limit `/register` in the reverse proxy if it
  keeps happening.
- **Who tried?** Failed and refused attempts on the approval page are logged
  with the client address:
  `journalctl -u garmin-mcp | grep -i 'login attempt'`.

## Security notes

For the home network only:

- **Plain HTTP.** Requests, responses, the bearer token and, if you sign in
  with OAuth there, the owner password cross the LAN unencrypted. That is
  acceptable on a home network you trust and nowhere else. Do not forward
  port 8765 to the internet.
- **Nothing is open by default.** The MCP endpoint needs the bearer token or
  an OAuth sign-in. Only with `GARMIN_MCP_OAUTH=off` *and* no
  `GARMIN_MCP_AUTH_TOKEN` can anyone who reaches the port read the account's
  health data; the service logs a warning at startup in that state.
- **The OAuth endpoints are there too.** Anyone on the network can reach the
  registration endpoint and the approval page. Neither grants anything
  without the owner password, and until one is set nobody can sign in at all.

With public access:

- **What protects the data.** The owner password and TLS, nothing else. Anyone
  on the internet can reach the approval page, and whoever knows the password
  can authorize a client and read the account's health data. Choose a long
  password that is used nowhere else (a generated one from a password manager;
  the minimum of 12 characters is a floor, not a target), and not the Garmin
  password. It is stored only as an scrypt hash.
- **TLS is not optional.** The password and the tokens travel in requests to
  the server. Expose the server only through the TLS proxy or tunnel, never by
  forwarding port 8765, and bind it to `127.0.0.1` when the proxy is on the
  same machine. `GARMIN_MCP_PUBLIC_URL` must be `https` for the same reason.
- **Password guessing is rate limited:** 5 failed attempts per client address
  (per /64 network for IPv6) and 100 overall per 15 minutes; after that the
  page refuses further tries until the oldest ones age out. The counters are
  kept in memory, so a restart resets them. Form posts that a browser marks as
  coming from another site are refused outright and do not count. The
  per-address limit depends on the real client address, which
  comes from `X-Forwarded-For` and is believed only from the addresses in
  `GARMIN_MCP_TRUSTED_PROXIES`. With a proxy on another machine that is not
  listed there, all visitors share one address and one limit. The overall
  limit also means that someone who keeps guessing can keep you from approving
  new clients for a while; clients that are already authorized are not
  affected. The rate limits slow guessing down; they do not make a weak
  password safe.
- **Registration is open.** Any OAuth client on the internet can register
  itself at `/register`; MCP clients depend on that. Registering grants
  nothing: every authorization needs the owner password. What a client may
  register is restricted: redirect targets must be `https`, `http` on
  localhost, or an app-specific scheme, without a user-info part, and names
  and sizes are capped. Registrations that were never approved are capped at
  2000 and dropped after a day. The limits bound what a flood can cost; they
  do not stop one, so put a request rate limit on the reverse proxy if the
  server attracts that kind of attention. The service is also capped at 1 GiB
  of memory (`MemoryMax=` in the unit).
- **Phishing is the realistic attack.** Nobody can get in without the
  password, so an attacker's best move is to get you to type it for them: by
  starting a sign-in from their own client and sending you the approval link.
  The page cannot tell who started a request. Treat any approval link you did
  not just trigger yourself as hostile.
- **What an anonymous visitor can reach:** `/healthz` (status and version), the
  two discovery documents, `/register`, `/authorize` and the approval page
  (useless without the password), `/token` and `/revoke` (useless without a
  valid code or token). `/mcp` answers 401 with a pointer to the discovery
  document.
- **The static bearer token still works,** on the public URL too, and never
  expires. It is as good as the owner password for reading the data, so keep
  it as secret, or comment out `GARMIN_MCP_AUTH_TOKEN` and restart to make
  OAuth the only way in. With a public URL set, the service refuses to start
  if the token is shorter than 32 characters; the one the installer generated
  is 64.
- **Tokens.** OAuth tokens are stored as hashes and are only valid for this
  server's endpoint. `sudo garmin-mcp revoke` ends a client's access at once.
  Client secrets of clients that register with one are stored as they are,
  because the server has to compare them; they are worthless without the
  owner's approval.

In both cases:

- **`/healthz` is always open** and returns only a status and the version.
- **Bind address and firewall.** The default `0.0.0.0` listens on every IPv4
  interface. Set `GARMIN_MCP_HOST` to one LAN address, or to `127.0.0.1` behind
  a local proxy, to narrow that, and restrict the port to the clients that
  need it in the machine's firewall.
- **Host allowlist.** `GARMIN_MCP_ALLOWED_HOSTS` rejects requests to the MCP
  endpoint whose `Host` header is not listed, which protects against DNS
  rebinding from a browser on the LAN. List every name and address that
  clients use in the URL, for example
  `192.168.1.10:*,garmin.lan:*,garmin.example.com`. With the allowlist on,
  requests that carry an `Origin` header (browsers send one) are rejected too.
- **Read-only.** The tools only read from Garmin Connect. None of them changes
  anything in the account.
- **Secrets on disk.** The env file (bearer token, optionally the Garmin
  password) is readable by root and the `garmin-mcp` group only. The Garmin
  token file grants access to the Garmin account, and `oauth.db` holds the
  password hash, the token hashes and client registrations; both are readable
  by the `garmin-mcp` user only.
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
sudo garmin-mcp clients              # which OAuth clients are authorized?
sudo garmin-mcp revoke <client_id>   # sign one of them out (or: revoke --all)
sudo garmin-mcp set-password         # set or change the owner password
```

`sudo garmin-mcp` runs each command as the `garmin-mcp` user with the env file
and the state directory of the service, so the files it writes keep the right
owner. Only `login` restarts the service; the OAuth commands take effect on
the running service at once.

The unit restarts the service 15 seconds after a crash, indefinitely. A service
that keeps restarting usually has a bad value in the env file; the log shows
the error.

All state is in `/var/lib/garmin-mcp`. To back it up, copy the whole directory:
`oauth.db` alone is incomplete without its `-wal` file. Losing the directory
costs a new Garmin login, a new owner password and approving the clients
again.

### Upgrade

```sh
git pull
sudo ./install.sh
```

Re-running the installer rebuilds the package in the virtualenv, replaces the
unit and the wrapper, and restarts the service. It keeps the env file, the
Garmin tokens and the OAuth state, so authorized clients stay signed in.
Compare your env file with `/etc/garmin-mcp/garmin-mcp.env.example` to see
settings that a new version added.

Upgrading from 0.1.0 needs nothing else. The bearer token keeps working as
before. OAuth sign-in is on from the first start, and unusable until you set
an owner password. One difference: a server that ran without
`GARMIN_MCP_AUTH_TOKEN` was open to the whole network and is now closed; set
`GARMIN_MCP_OAUTH=off` to get the open server back.

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
sudo ./uninstall.sh --purge    # also remove the config, the state and the garmin-mcp user
```

Without `--purge` the config stays in `/etc/garmin-mcp` and the state (Garmin
tokens, OAuth clients, owner password) in `/var/lib/garmin-mcp`, and a later
install picks them up again. Removing the server does not remove a reverse
proxy you set up in front of it.

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

To try the OAuth side locally, no TLS and no further settings are needed:

```sh
uv run garmin-mcp set-password       # stored in ~/.local/state/garmin-mcp/oauth.db
uv run garmin-mcp serve              # sign-in at http://localhost:8765/mcp
```

Code layout:

- `src/garmin_mcp/config.py` reads the settings from the environment.
- `src/garmin_mcp/session.py` holds the single Garmin session: login, token
  reuse, the login cooldown, and the mapping of Garmin errors to tool errors.
- `src/garmin_mcp/server.py` builds the MCP server and the HTTP app (bearer
  token check, OAuth wiring, `/healthz`).
- `src/garmin_mcp/oauth.py` is the OAuth authorization server behind the MCP
  SDK's endpoints: the SQLite store, codes and tokens, and the owner password.
  `src/garmin_mcp/login.py` is the approval page and its rate limiter.
- `src/garmin_mcp/cli.py` is the command line: `serve`, `login`, `check`,
  `set-password`, `clients`, `revoke`.
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
