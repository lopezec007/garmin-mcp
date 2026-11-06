"""The built-in OAuth authorization server, driven over HTTP like a real client."""

from __future__ import annotations

import base64
import hashlib
import io
import re
import time
from dataclasses import replace
from urllib.parse import parse_qs, urlsplit

import pytest
from starlette.testclient import TestClient

from garmin_mcp import cli
from garmin_mcp.config import Settings
from garmin_mcp.login import AttemptLimiter, rate_limit_key
from garmin_mcp.oauth import (
    MAX_CLIENT_NAME_LENGTH,
    MAX_STATE_LENGTH,
    PENDING_TTL,
    REFRESH_GRACE,
    REFRESH_GRACE_USES,
    OAuthStore,
    describe_redirect,
    hash_password,
    normalize_url,
    printable,
    verify_password,
)
from garmin_mcp.server import build_app

PUBLIC = "http://localhost:8765"
STATIC_TOKEN = "static-secret-0123456789abcdef0123456789"
PASSWORD = "correct horse battery staple"
REDIRECT = "http://localhost:9999/callback"
VERIFIER = "v" * 64
CHALLENGE = (
    base64.urlsafe_b64encode(hashlib.sha256(VERIFIER.encode()).digest()).decode().rstrip("=")
)
MCP_HEADERS = {
    "Content-Type": "application/json",
    "Accept": "application/json, text/event-stream",
}


@pytest.fixture
def oauth_settings(settings, tmp_path) -> Settings:
    return replace(settings, public_url=PUBLIC, oauth_db=tmp_path / "oauth.db")


@pytest.fixture
def store(oauth_settings) -> OAuthStore:
    store = OAuthStore(oauth_settings.oauth_db)
    store.set_password(PASSWORD)
    return store


@pytest.fixture
def http(oauth_settings, garmin, store):
    with TestClient(build_app(oauth_settings, garmin), base_url=PUBLIC) as client:
        yield client


def register(http, **overrides) -> dict:
    body = {
        "client_name": "Test Client",
        "redirect_uris": [REDIRECT],
        "token_endpoint_auth_method": "none",
        **overrides,
    }
    response = http.post("/register", json=body)
    assert response.status_code == 201, response.text
    return response.json()


def start_authorization(http, client_id: str, **params) -> str:
    """GET /authorize and return the transaction id of the approval page."""
    query = {
        "response_type": "code",
        "client_id": client_id,
        "redirect_uri": REDIRECT,
        "code_challenge": CHALLENGE,
        "code_challenge_method": "S256",
        "state": "xyz",
        **params,
    }
    response = http.get("/authorize", params=query, follow_redirects=False)
    assert response.status_code == 302, response.text
    location = urlsplit(response.headers["location"])
    assert f"{location.scheme}://{location.netloc}{location.path}" == f"{PUBLIC}/oauth/login"
    return parse_qs(location.query)["txn"][0]


def approve(http, txn: str, password: str = PASSWORD, action: str = "approve"):
    return http.post(
        "/oauth/login",
        data={"txn": txn, "password": password, "action": action},
        follow_redirects=False,
    )


def code_from(response) -> str:
    assert response.status_code == 303, response.text
    location = urlsplit(response.headers["location"])
    assert response.headers["location"].startswith(REDIRECT)
    query = parse_qs(location.query)
    assert query["state"] == ["xyz"]
    return query["code"][0]


def exchange(http, client_id: str, code: str, verifier: str = VERIFIER):
    return http.post(
        "/token",
        data={
            "grant_type": "authorization_code",
            "client_id": client_id,
            "code": code,
            "code_verifier": verifier,
            "redirect_uri": REDIRECT,
        },
    )


def refresh(http, client_id: str, refresh_token: str):
    return http.post(
        "/token",
        data={
            "grant_type": "refresh_token",
            "client_id": client_id,
            "refresh_token": refresh_token,
        },
    )


def sign_in(http) -> tuple[str, dict]:
    """Run the whole flow; returns (client_id, token response)."""
    client_id = register(http)["client_id"]
    code = code_from(approve(http, start_authorization(http, client_id)))
    response = exchange(http, client_id, code)
    assert response.status_code == 200, response.text
    return client_id, response.json()


def list_tools(http, token: str | None):
    headers = dict(MCP_HEADERS)
    if token:
        headers["Authorization"] = f"Bearer {token}"
    return http.post(
        "/mcp", json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"}, headers=headers
    )


# --- discovery -------------------------------------------------------------------


def test_discovery_documents(http):
    resource = http.get("/.well-known/oauth-protected-resource/mcp").json()
    assert resource["resource"] == f"{PUBLIC}/mcp"
    assert resource["authorization_servers"] == [PUBLIC]

    server = http.get("/.well-known/oauth-authorization-server").json()
    assert server["issuer"] == PUBLIC
    assert server["authorization_endpoint"] == f"{PUBLIC}/authorize"
    assert server["token_endpoint"] == f"{PUBLIC}/token"
    assert server["registration_endpoint"] == f"{PUBLIC}/register"
    assert server["revocation_endpoint"] == f"{PUBLIC}/revoke"
    assert server["code_challenge_methods_supported"] == ["S256"]
    # Clients without a secret (most MCP clients) must see they are supported.
    assert "none" in server["token_endpoint_auth_methods_supported"]


def test_unauthenticated_request_points_at_the_metadata(http):
    response = list_tools(http, None)
    assert response.status_code == 401
    challenge = response.headers["www-authenticate"]
    assert f'resource_metadata="{PUBLIC}/.well-known/oauth-protected-resource/mcp"' in challenge
    assert list_tools(http, "made-up-token").status_code == 401
    assert http.get("/healthz").status_code == 200


# --- the happy path ----------------------------------------------------------------


def test_full_flow_then_tool_call(http, store):
    client_id, tokens = sign_in(http)
    assert tokens["token_type"] == "Bearer"
    assert tokens["expires_in"] == 3600
    assert tokens["refresh_token"]

    response = list_tools(http, tokens["access_token"])
    assert response.status_code == 200
    assert response.json()["result"]["tools"]

    [listed] = store.list_clients()
    assert listed["client_id"] == client_id
    assert listed["name"] == "Test Client"
    assert listed["valid_until"] > time.time()


def test_approval_page_shows_who_is_asking(http):
    client_id = register(http, client_name="<script>alert(1)</script>")["client_id"]
    txn = start_authorization(http, client_id)
    page = http.get("/oauth/login", params={"txn": txn})
    assert page.status_code == 200
    assert "&lt;script&gt;alert(1)&lt;/script&gt;" in page.text
    assert "<script>" not in page.text
    assert '<span class="dest">localhost:9999</span>' in page.text  # where the code goes
    assert "less than a minute ago" in page.text
    assert page.headers["x-frame-options"] == "DENY"
    assert "frame-ancestors 'none'" in page.headers["content-security-policy"]
    assert page.headers["cache-control"] == "no-store"


def test_tokens_are_stored_hashed(http, oauth_settings):
    _, tokens = sign_in(http)
    raw = b"".join(path.read_bytes() for path in oauth_settings.oauth_db.parent.glob("oauth.db*"))
    assert tokens["access_token"].encode() not in raw
    assert tokens["refresh_token"].encode() not in raw
    assert PASSWORD.encode() not in raw


def test_static_token_still_works_next_to_oauth(oauth_settings, garmin, store):
    app = build_app(replace(oauth_settings, auth_token=STATIC_TOKEN), garmin)
    with TestClient(app, base_url=PUBLIC) as http:
        assert list_tools(http, STATIC_TOKEN).status_code == 200
        assert list_tools(http, STATIC_TOKEN[:-1] + "X").status_code == 401
        assert list_tools(http, STATIC_TOKEN[:-1]).status_code == 401


def test_short_static_token_is_refused_in_public_mode(oauth_settings):
    with pytest.raises(ValueError, match="at least 32 characters"):
        replace(oauth_settings, auth_token="hunter2")
    Settings(auth_token="hunter2")  # still allowed on a LAN-only server


# --- the approval page says no -------------------------------------------------------


def test_wrong_password_is_refused(http):
    client_id = register(http)["client_id"]
    txn = start_authorization(http, client_id)
    response = approve(http, txn, "not the password")
    assert response.status_code == 401
    assert "Wrong password" in response.text
    assert approve(http, txn, "").status_code == 401
    # The same page can still be completed with the right one.
    code_from(approve(http, txn))


def test_password_guessing_is_rate_limited(http):
    client_id = register(http)["client_id"]
    txn = start_authorization(http, client_id)
    for _ in range(5):
        assert approve(http, txn, "guess").status_code == 401
    blocked = approve(http, txn, PASSWORD)  # even the right password must wait
    assert blocked.status_code == 429
    assert int(blocked.headers["retry-after"]) > 0


def test_no_owner_password_means_no_authorization(oauth_settings, garmin):
    with TestClient(build_app(oauth_settings, garmin), base_url=PUBLIC) as http:
        client_id = register(http)["client_id"]
        response = approve(http, start_authorization(http, client_id), "anything at all")
    assert response.status_code == 503
    assert "set-password" in response.text


def test_deny_sends_the_client_an_error(http):
    client_id = register(http)["client_id"]
    txn = start_authorization(http, client_id)
    response = approve(http, txn, "", action="deny")
    assert response.status_code == 303
    assert response.headers["location"].startswith(REDIRECT)
    query = parse_qs(urlsplit(response.headers["location"]).query)
    assert query["error"] == ["access_denied"]
    assert query["state"] == ["xyz"]


def test_approval_transaction_is_single_use_and_unforgeable(http):
    client_id = register(http)["client_id"]
    txn = start_authorization(http, client_id)
    payload, signature = txn.split(".")

    # Any change to the signed request, or a made-up one, is refused.
    other = register(http)["client_id"]
    forged = (
        base64.urlsafe_b64encode(
            base64.urlsafe_b64decode(payload + "=" * (-len(payload) % 4)).replace(
                client_id.encode(), other.encode()
            )
        )
        .decode()
        .rstrip("=")
    )
    assert forged != payload
    for bad in (f"{forged}.{signature}", f"{payload}.{signature[:-2]}AA", payload, "made-up", ""):
        assert approve(http, bad).status_code == 400
        assert http.get("/oauth/login", params={"txn": bad}).status_code == 400
    assert http.get("/oauth/login").status_code == 400

    code_from(approve(http, txn))
    assert approve(http, txn).status_code == 400  # one approval, one code
    assert http.get("/oauth/login", params={"txn": txn}).status_code == 400


def test_approval_page_expires(http, monkeypatch):
    client_id = register(http)["client_id"]
    txn = start_authorization(http, client_id)
    assert http.get("/oauth/login", params={"txn": txn}).status_code == 200
    real_time = time.time
    monkeypatch.setattr("garmin_mcp.oauth.time.time", lambda: real_time() + PENDING_TTL + 1)
    assert http.get("/oauth/login", params={"txn": txn}).status_code == 400
    assert approve(http, txn).status_code == 400


def test_approval_fails_if_the_client_was_removed_meanwhile(http, store):
    client_id = register(http)["client_id"]
    txn = start_authorization(http, client_id)
    store.revoke_client(client_id)
    assert approve(http, txn).status_code == 400


# --- authorization requests ------------------------------------------------------------


def test_resource_indicator_must_be_this_server(http):
    client_id = register(http)["client_id"]
    start_authorization(http, client_id, resource=f"{PUBLIC}/mcp")
    start_authorization(http, client_id, resource=f"{PUBLIC.upper()}/mcp/")
    response = http.get(
        "/authorize",
        params={
            "response_type": "code",
            "client_id": client_id,
            "redirect_uri": REDIRECT,
            "code_challenge": CHALLENGE,
            "resource": "https://elsewhere.example/mcp",
        },
        follow_redirects=False,
    )
    assert response.status_code == 302
    assert parse_qs(urlsplit(response.headers["location"]).query)["error"] == ["invalid_target"]


def test_unregistered_redirect_uri_is_not_followed(http):
    client_id = register(http)["client_id"]
    response = http.get(
        "/authorize",
        params={
            "response_type": "code",
            "client_id": client_id,
            "redirect_uri": "https://attacker.example/steal",
            "code_challenge": CHALLENGE,
        },
        follow_redirects=False,
    )
    assert response.status_code == 400  # shown to the user, never redirected


@pytest.mark.parametrize(
    "uri",
    [
        "http://attacker.example/cb",  # plain http off-machine
        "javascript:alert(1)",
        "data:text/html,hi",
        "https://ok.example/cb#fragment",
    ],
)
def test_dangerous_redirect_uris_cannot_be_registered(http, uri):
    response = http.post("/register", json={"redirect_uris": [uri]})
    assert response.status_code == 400
    assert response.json()["error"] in ("invalid_redirect_uri", "invalid_client_metadata")


@pytest.mark.parametrize(
    "uri", ["https://claude.ai/api/mcp/auth_callback", "http://127.0.0.1:33418/cb", "cursor://cb"]
)
def test_normal_redirect_uris_can_be_registered(http, uri):
    assert http.post("/register", json={"redirect_uris": [uri]}).status_code == 201


def test_redirect_uri_with_userinfo_cannot_be_registered(http):
    # "https://claude.ai@evil.example/" is evil.example dressed up as claude.ai.
    for uri in (
        "https://claude.ai@evil.example/api/mcp/auth_callback",
        "https://claude.ai%2Fapi%2Fmcp@evil.example/",
        "https://user:pw@ok.example/cb",
        "http://localhost@evil.example/cb",
    ):
        response = http.post("/register", json={"redirect_uris": [uri], "client_name": "Claude"})
        assert response.status_code == 400, uri


@pytest.mark.parametrize(
    "uri",
    [
        "ftp://evil.example/x", "wss://evil.example/x", "smb://evil.example/share",
        "webcal://evil.example/c", "mailto:someone@evil.example", "intent://x#Intent;end",
        "view-source:https://evil.example/", "filesystem:https://evil.example/x",
        "chrome://settings/", "ms-msdt:/id", "search-ms:query=x", "ssh://evil.example",
    ],
)  # fmt: skip
def test_network_and_os_handler_schemes_cannot_be_registered(http, uri):
    assert http.post("/register", json={"redirect_uris": [uri]}).status_code == 400


def test_destination_shown_to_the_owner():
    assert describe_redirect("https://claude.ai/api/mcp/auth_callback") == ("claude.ai", True)
    assert describe_redirect("https://ok.example:8443/cb?x=1") == ("ok.example:8443", True)
    assert describe_redirect("http://[::1]:3000/cb") == ("[::1]:3000", True)
    # Even if one slipped through registration, user-info is never displayed as the host.
    assert describe_redirect("https://claude.ai@evil.example/cb") == ("evil.example", True)
    # App links are shown whole, not summarised as "an app on this device".
    assert describe_redirect("cursor://anysphere.cursor-mcp/oauth/cb") == (
        "cursor://anysphere.cursor-mcp/oauth/cb",
        False,
    )


def test_app_link_destination_is_shown_in_full(http):
    client_id = http.post(
        "/register", json={"redirect_uris": ["claude.ai.verified-app://cb"]}
    ).json()["client_id"]
    response = http.get(
        "/authorize",
        params={
            "response_type": "code",
            "client_id": client_id,
            "code_challenge": CHALLENGE,
        },
    )
    assert response.status_code == 200
    assert '<span class="dest">claude.ai.verified-app://cb</span>' in response.text
    assert "not a web address" in response.text


def test_client_supplied_text_is_cleaned_before_display(http):
    name = "Claude\x1b[2K\n\u202egnp.exe"
    client_id = register(http, client_name=name)["client_id"]
    page = http.get("/oauth/login", params={"txn": start_authorization(http, client_id)})
    assert "<strong>Claude[2Kgnp.exe</strong>" in page.text
    assert printable("a" * 500, 80).endswith("…") and len(printable("a" * 500, 80)) == 80


def test_oversized_client_metadata_is_refused(http):
    too_long_name = http.post(
        "/register",
        json={"redirect_uris": [REDIRECT], "client_name": "n" * (MAX_CLIENT_NAME_LENGTH + 1)},
    )
    assert too_long_name.status_code == 400
    long_uri = http.post("/register", json={"redirect_uris": [f"https://ok.example/{'a' * 3000}"]})
    assert long_uri.status_code == 400
    bulky = http.post(
        "/register",
        json={"redirect_uris": [REDIRECT], "contacts": ["c" * 1000 for _ in range(30)]},
    )
    assert bulky.status_code == 400
    assert "larger than" in bulky.json()["error_description"]


def test_oversized_state_and_bad_challenge_are_refused(http):
    client_id = register(http)["client_id"]
    for params in (
        {"state": "s" * (MAX_STATE_LENGTH + 1)},
        {"code_challenge": "short"},
        {"code_challenge": ""},
        {"resource": "http://[::1/mcp"},
        {"resource": "http://localhost:99999999/mcp"},
    ):
        response = http.get(
            "/authorize",
            params={
                "response_type": "code",
                "client_id": client_id,
                "redirect_uri": REDIRECT,
                "code_challenge": CHALLENGE,
                **params,
            },
            follow_redirects=False,
        )
        assert response.status_code == 302, params
        error = parse_qs(urlsplit(response.headers["location"]).query)["error"][0]
        assert error in ("invalid_request", "invalid_target"), params


def test_loopback_redirect_matches_on_any_port(http):
    # Native apps listen on a new ephemeral port each time (RFC 8252 7.3).
    client_id = register(http, redirect_uris=["http://127.0.0.1:1111/callback"])["client_id"]

    def authorize(redirect_uri):
        return http.get(
            "/authorize",
            params={
                "response_type": "code",
                "client_id": client_id,
                "redirect_uri": redirect_uri,
                "code_challenge": CHALLENGE,
            },
            follow_redirects=False,
        )

    assert authorize("http://127.0.0.1:2222/callback").status_code == 302
    assert authorize("http://127.0.0.1:2222/other").status_code == 400
    assert authorize("http://127.0.0.1.evil.example:2222/callback").status_code == 400
    assert authorize("https://127.0.0.1:2222/callback").status_code == 400
    # Non-loopback URIs still need an exact match.
    web = register(http, redirect_uris=["https://ok.example/cb"])["client_id"]
    response = http.get(
        "/authorize",
        params={
            "response_type": "code",
            "client_id": web,
            "redirect_uri": "https://ok.example:8443/cb",
            "code_challenge": CHALLENGE,
        },
        follow_redirects=False,
    )
    assert response.status_code == 400


# --- the approval form endpoint under abuse -------------------------------------------------


def test_login_form_accepts_only_a_small_urlencoded_body(http):
    client_id = register(http)["client_id"]
    txn = start_authorization(http, client_id)
    multipart = http.post(
        "/oauth/login",
        data={"txn": txn, "action": "approve"},
        files={"password": ("p.txt", b"x" * 100_000)},
    )
    assert multipart.status_code == 400
    huge = http.post("/oauth/login", data={"txn": txn, "password": "x" * 100_000})
    assert huge.status_code == 400
    many = http.post("/oauth/login", data={f"f{i}": "x" for i in range(50)})
    assert many.status_code == 400
    as_json = http.post("/oauth/login", json={"txn": txn, "password": PASSWORD})
    assert as_json.status_code == 400
    code_from(approve(http, txn))  # none of that consumed the transaction


def test_cross_site_form_posts_are_refused(http):
    client_id = register(http)["client_id"]
    txn = start_authorization(http, client_id)
    form = {"txn": txn, "password": "guess", "action": "approve"}
    for headers in (
        {"Origin": "https://evil.example"},
        {"Origin": "null"},
        {"Sec-Fetch-Site": "cross-site"},
    ):
        assert http.post("/oauth/login", data=form, headers=headers).status_code == 403
    # Those did not count as failed guesses against the owner's address.
    same_site = http.post(
        "/oauth/login",
        data={**form, "password": PASSWORD},
        headers={"Origin": PUBLIC, "Sec-Fetch-Site": "same-origin"},
        follow_redirects=False,
    )
    code_from(same_site)


def test_rate_limit_buckets():
    assert rate_limit_key("203.0.113.7") == "203.0.113.7"
    # One IPv6 subscriber owns a whole /64: all of it is one bucket.
    assert rate_limit_key("2001:db8:1:2::1") == rate_limit_key("2001:db8:1:2:ffff::9")
    assert rate_limit_key("2001:db8:1:2::1") != rate_limit_key("2001:db8:1:3::1")
    assert rate_limit_key("::ffff:203.0.113.7") == "203.0.113.7"
    assert rate_limit_key("unknown") == "unknown"


def test_limiter_forgets_stale_addresses_when_full(monkeypatch):
    now = [1000.0]
    monkeypatch.setattr("garmin_mcp.login.time.monotonic", lambda: now[0])
    limiter = AttemptLimiter(per_address=1, overall=1000, window=60, max_addresses=3)
    for address in ("10.0.0.1", "10.0.0.2", "10.0.0.3"):
        limiter.record_failure(address)
    now[0] += 61
    limiter.record_failure("10.0.0.4")  # table was full of stale entries
    assert limiter.retry_after("10.0.0.4") > 0


# --- token endpoint ----------------------------------------------------------------------


def test_pkce_verifier_is_checked(http):
    client_id = register(http)["client_id"]
    code = code_from(approve(http, start_authorization(http, client_id)))
    response = exchange(http, client_id, code, verifier="w" * 64)
    assert response.status_code == 400
    assert response.json()["error"] == "invalid_grant"


def test_code_is_bound_to_its_client(http):
    client_id = register(http)["client_id"]
    other = register(http)["client_id"]
    code = code_from(approve(http, start_authorization(http, client_id)))
    assert exchange(http, other, code).json()["error"] == "invalid_grant"


def test_replayed_code_revokes_what_it_issued(http):
    client_id = register(http)["client_id"]
    code = code_from(approve(http, start_authorization(http, client_id)))
    tokens = exchange(http, client_id, code).json()
    assert list_tools(http, tokens["access_token"]).status_code == 200

    replay = exchange(http, client_id, code)
    assert replay.status_code == 400
    assert replay.json()["error"] == "invalid_grant"
    assert list_tools(http, tokens["access_token"]).status_code == 401
    assert refresh(http, client_id, tokens["refresh_token"]).status_code == 400


def test_refresh_rotates_tokens(http, store):
    client_id, tokens = sign_in(http)
    response = refresh(http, client_id, tokens["refresh_token"])
    assert response.status_code == 200
    renewed = response.json()
    assert renewed["access_token"] != tokens["access_token"]
    assert renewed["refresh_token"] != tokens["refresh_token"]
    assert list_tools(http, renewed["access_token"]).status_code == 200
    assert refresh(http, client_id, renewed["refresh_token"]).status_code == 200


def age_rotation(store, refresh_token: str, seconds: int) -> None:
    """Pretend a refresh token was rotated ``seconds`` ago."""
    store._run(
        "UPDATE tokens SET rotated_at = rotated_at - ? WHERE token_hash = ?",
        (seconds, hashlib.sha256(refresh_token.encode()).hexdigest()),
    )


def test_rotated_refresh_token_has_a_short_bounded_grace(http):
    client_id, tokens = sign_in(http)
    # A client retrying, or refreshing from two places at once, still succeeds ...
    for _ in range(REFRESH_GRACE_USES):
        assert refresh(http, client_id, tokens["refresh_token"]).status_code == 200
    # ... but one token cannot be turned into an unlimited number of sessions.
    assert refresh(http, client_id, tokens["refresh_token"]).json()["error"] == "invalid_grant"


def test_replayed_refresh_token_revokes_the_grant(http, store):
    client_id, tokens = sign_in(http)
    thief = refresh(http, client_id, tokens["refresh_token"]).json()  # stolen token used first
    assert list_tools(http, thief["access_token"]).status_code == 200

    age_rotation(store, tokens["refresh_token"], REFRESH_GRACE + 1)
    # The legitimate client now presents the token it still holds: that is a
    # replay of a rotated token, so everything from this grant dies.
    assert refresh(http, client_id, tokens["refresh_token"]).json()["error"] == "invalid_grant"
    assert list_tools(http, thief["access_token"]).status_code == 401
    assert refresh(http, client_id, thief["refresh_token"]).status_code == 400
    assert list_tools(http, tokens["access_token"]).status_code == 401


def test_approving_a_client_again_replaces_its_previous_session(http, store):
    client_id, old = sign_in(http)
    code = code_from(approve(http, start_authorization(http, client_id)))
    new = exchange(http, client_id, code).json()
    assert list_tools(http, new["access_token"]).status_code == 200
    assert list_tools(http, old["access_token"]).status_code == 401
    assert refresh(http, client_id, old["refresh_token"]).status_code == 400
    assert len(store.list_clients()) == 1


def test_refresh_token_is_bound_to_its_client(http):
    _, tokens = sign_in(http)
    other = register(http)["client_id"]
    assert refresh(http, other, tokens["refresh_token"]).json()["error"] == "invalid_grant"


def test_expired_access_token_is_refused(http, store):
    _, tokens = sign_in(http)
    store.expire_token(tokens["access_token"], int(time.time()) - 1)
    assert list_tools(http, tokens["access_token"]).status_code == 401


def test_a_refresh_token_is_not_an_access_token(http):
    _, tokens = sign_in(http)
    assert list_tools(http, tokens["refresh_token"]).status_code == 401


def test_revocation_endpoint_revokes_the_whole_grant(http):
    client_id, tokens = sign_in(http)
    # A public client sends no client_secret (RFC 7009); that must work.
    response = http.post("/revoke", data={"client_id": client_id, "token": tokens["refresh_token"]})
    assert response.status_code == 200
    assert list_tools(http, tokens["access_token"]).status_code == 401
    assert refresh(http, client_id, tokens["refresh_token"]).status_code == 400


def test_revocation_cannot_touch_another_clients_tokens(http):
    _, tokens = sign_in(http)
    other = register(http)["client_id"]
    response = http.post("/revoke", data={"client_id": other, "token": tokens["access_token"]})
    assert response.status_code == 200  # RFC 7009: no hint either way
    assert list_tools(http, tokens["access_token"]).status_code == 200


def test_confidential_client_must_present_its_secret(http):
    registered = http.post("/register", json={"redirect_uris": [REDIRECT]}).json()
    client_id, secret = registered["client_id"], registered["client_secret"]
    assert registered["token_endpoint_auth_method"] == "client_secret_post"

    def token_request(code, **extra):
        return http.post(
            "/token",
            data={
                "grant_type": "authorization_code",
                "client_id": client_id,
                "code": code,
                "code_verifier": VERIFIER,
                "redirect_uri": REDIRECT,
                **extra,
            },
        )

    code = code_from(approve(http, start_authorization(http, client_id)))
    assert token_request(code).status_code == 401
    assert token_request(code, client_secret="wrong").status_code == 401
    tokens = token_request(code, client_secret=secret).json()
    assert list_tools(http, tokens["access_token"]).status_code == 200

    revoked = http.post(
        "/revoke",
        data={"client_id": client_id, "client_secret": secret, "token": tokens["access_token"]},
    )
    assert revoked.status_code == 200
    assert list_tools(http, tokens["access_token"]).status_code == 401


# --- owner-side management -----------------------------------------------------------------


def test_revoking_a_client_takes_effect_immediately(http, store):
    client_id, tokens = sign_in(http)
    assert store.revoke_client(client_id) is True
    assert list_tools(http, tokens["access_token"]).status_code == 401
    assert store.list_clients() == []
    assert store.revoke_client(client_id) is False


def test_revoke_all(http, store):
    _, first = sign_in(http)
    _, second = sign_in(http)
    assert store.revoke_all() == 2
    assert list_tools(http, first["access_token"]).status_code == 401
    assert list_tools(http, second["access_token"]).status_code == 401
    assert store.password_hash() is not None  # the password is kept


def test_unapproved_registrations_are_capped(http, store, monkeypatch):
    monkeypatch.setattr("garmin_mcp.oauth.MAX_UNAUTHORIZED_CLIENTS", 10)
    approved, _ = sign_in(http)
    in_flight = register(http)["client_id"]
    for _ in range(9):
        register(http)

    # Full of fresh registrations: refuse the newcomer, evict nobody -- the
    # owner of an existing one may be on the approval page right now.
    refused = http.post("/register", json={"redirect_uris": [REDIRECT]})
    assert refused.status_code == 400
    assert "too many pending registrations" in refused.json()["error_description"]
    code_from(approve(http, start_authorization(http, in_flight)))

    # Once they are no longer fresh, the oldest make room.
    store._run("UPDATE clients SET created_at = created_at - 3600 WHERE authorized_at IS NULL")
    register(http)
    ids = {row["client_id"] for row in store._all("SELECT client_id FROM clients")}
    assert len(ids) == 11
    assert approved in ids  # approved clients are never evicted


def test_database_file_is_private(oauth_settings, store):
    assert oauth_settings.oauth_db.stat().st_mode & 0o777 == 0o600


# --- building blocks -----------------------------------------------------------------------


def test_password_hashing():
    encoded = hash_password(PASSWORD)
    assert encoded.startswith("scrypt$") and PASSWORD not in encoded
    assert hash_password(PASSWORD) != encoded  # salted
    assert verify_password(PASSWORD, encoded)
    assert not verify_password(PASSWORD + "x", encoded)
    assert not verify_password(PASSWORD, "garbage")
    assert not verify_password(PASSWORD, "scrypt$x$8$1$AAAA$AAAA")


def test_short_passwords_are_rejected(oauth_settings):
    store = OAuthStore(oauth_settings.oauth_db)
    with pytest.raises(ValueError, match="at least 12"):
        store.set_password("short")
    assert store.password_hash() is None


def test_attempt_limiter_windows(monkeypatch):
    now = [1000.0]
    monkeypatch.setattr("garmin_mcp.login.time.monotonic", lambda: now[0])
    limiter = AttemptLimiter(per_address=2, overall=3, window=60)
    assert limiter.retry_after("a") == 0
    limiter.record_failure("a")
    limiter.record_failure("a")
    assert limiter.retry_after("a") > 0
    assert limiter.retry_after("b") == 0  # other addresses are unaffected ...
    limiter.record_failure("b")
    assert limiter.retry_after("c") > 0  # ... until the overall limit is hit
    now[0] += 61
    assert limiter.retry_after("a") == 0
    limiter.record_failure("a")
    limiter.record_success("a")
    limiter.record_failure("a")
    assert limiter.retry_after("a") == 0  # success cleared the earlier failure


def test_normalize_url():
    assert normalize_url("HTTPS://Garmin.Example.com:443/mcp/") == "https://garmin.example.com/mcp"
    assert normalize_url("http://localhost:8765") == "http://localhost:8765"


@pytest.mark.parametrize(
    "url",
    [
        "garmin.example.com",
        "http://garmin.example.com",
        "https://garmin.example.com/mcp",
        "https://user:pw@garmin.example.com",
        "ftp://garmin.example.com",
    ],
)
def test_bad_public_url_is_a_configuration_error(url):
    with pytest.raises(ValueError, match="GARMIN_MCP_PUBLIC_URL"):
        Settings(public_url=url)


def test_public_url_is_normalised():
    settings = Settings(public_url="https://garmin.example.com/", path="/mcp")
    assert settings.public_url == "https://garmin.example.com"
    assert settings.resource_url == "https://garmin.example.com/mcp"
    assert Settings().oauth_enabled is False


# --- admin commands --------------------------------------------------------------------------


@pytest.fixture
def cli_env(monkeypatch, tmp_path):
    db = tmp_path / "cli-oauth.db"
    monkeypatch.setenv("GARMIN_MCP_OAUTH_DB", str(db))
    monkeypatch.setenv("GARMIN_MCP_PUBLIC_URL", PUBLIC)
    return db


def test_cli_set_password_from_stdin(cli_env, monkeypatch, capsys):
    monkeypatch.setattr("sys.stdin", io.StringIO(PASSWORD + "\n"))
    assert cli.main(["set-password"]) == 0
    assert verify_password(PASSWORD, OAuthStore(cli_env).password_hash())

    monkeypatch.setattr("sys.stdin", io.StringIO("short\n"))
    assert cli.main(["set-password"]) == 1
    assert verify_password(PASSWORD, OAuthStore(cli_env).password_hash())  # unchanged
    assert "at least 12" in capsys.readouterr().err


def test_cli_clients_and_revoke(cli_env, garmin, settings, capsys):
    OAuthStore(cli_env).set_password(PASSWORD)
    app = build_app(replace(settings, public_url=PUBLIC, oauth_db=cli_env), garmin)
    with TestClient(app, base_url=PUBLIC) as http:
        client_id, tokens = sign_in(http)

        assert cli.main(["clients"]) == 0
        out = capsys.readouterr().out
        assert client_id in out and "Test Client" in out and REDIRECT in out

        assert cli.main(["revoke"]) == 2
        assert cli.main(["revoke", "no-such-client"]) == 1
        assert cli.main(["revoke", client_id]) == 0
        # A separate process changed the database; the running server sees it.
        assert list_tools(http, tokens["access_token"]).status_code == 401

        sign_in(http)
        assert cli.main(["revoke", "--all"]) == 0
        capsys.readouterr()
        assert cli.main(["clients"]) == 0
        assert "No OAuth clients" in capsys.readouterr().out


def test_cli_refuses_to_invent_a_database(cli_env, capsys):
    assert cli.main(["clients"]) == 1
    assert cli.main(["revoke", "--all"]) == 1
    assert "No OAuth database" in capsys.readouterr().err
    assert not cli_env.exists()


def test_cli_does_not_print_client_text_raw(cli_env, garmin, settings, capsys):
    OAuthStore(cli_env).set_password(PASSWORD)
    app = build_app(replace(settings, public_url=PUBLIC, oauth_db=cli_env), garmin)
    with TestClient(app, base_url=PUBLIC) as http:
        client_id = register(http, client_name="evil\x1b[2J\nfake-id  Trusted")["client_id"]
        code = code_from(approve(http, start_authorization(http, client_id)))
        assert exchange(http, client_id, code).status_code == 200
    assert cli.main(["clients"]) == 0
    out = capsys.readouterr().out
    assert "\x1b" not in out
    assert "evil[2Jfake-id  Trusted" in out


def test_cli_reports_bad_configuration(monkeypatch, capsys):
    monkeypatch.setenv("GARMIN_MCP_PUBLIC_URL", "http://garmin.example.com")
    assert cli.main(["clients"]) == 2
    assert re.search(r"configuration error: .*https", capsys.readouterr().err)
