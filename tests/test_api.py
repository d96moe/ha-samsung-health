from pathlib import Path

import httpx
import pytest
from samsung_health_cloud.exceptions import AuthenticationError, SamsungHealthCloudError

from custom_components.ha_samsung_health import api, compat


@pytest.mark.parametrize(
    ("exc", "expected"),
    [
        (SamsungHealthCloudError("authorize failed with HTTP 401"), api.SamsungHealthAuthError),
        (SamsungHealthCloudError("token failed with HTTP 400"), api.SamsungHealthAuthError),
        (SamsungHealthCloudError("document GET failed with HTTP 403"), api.SamsungHealthTransientError),
        (SamsungHealthCloudError("document GET failed with HTTP 429"), api.SamsungHealthRateLimited),
        (SamsungHealthCloudError("authorize failed with HTTP 503"), api.SamsungHealthTransientError),
        (SamsungHealthCloudError("document GET network request failed"), api.SamsungHealthTransientError),
        (AuthenticationError("Pending Samsung authentication has expired"), api.SamsungHealthAuthError),
    ],
)
def test_classify(exc, expected):
    assert type(api.classify(exc)) is expected


def test_eu_host_patch_accepts_bare_host_and_keeps_allowlist():
    compat.apply_patches()
    compat.apply_patches()  # idempotent
    from samsung_health_cloud import account_auth

    assert account_auth._trusted_auth_server_url("eu-auth2.samsungosp.com") == "https://eu-auth2.samsungosp.com"
    assert account_auth._trusted_auth_server_url("https://account.samsung.com") == "https://account.samsung.com"
    with pytest.raises(AuthenticationError):
        account_auth._trusted_auth_server_url("evil.example.com")


def _client(handler):
    return httpx.Client(transport=httpx.MockTransport(handler))


def test_server_revision_parses_400_body():
    def handler(request):
        assert request.url.params["limit"] == "1"
        return httpx.Response(400, json={
            "rcode": 4001001,
            "rmsg": "The schemaRevision is invalid, you should update to the latest schema information (server revision: 5)",
        })

    with _client(handler) as http:
        assert compat.server_revision(http, "Bearer x", "com.samsung.shealth.step_daily_trend", 14) == 5


def test_server_revision_unknown_body_returns_none():
    with _client(lambda r: httpx.Response(404, text="nope")) as http:
        assert compat.server_revision(http, "Bearer x", "com.samsung.x", 1) is None


@pytest.fixture
def client(tmp_path: Path):
    paths = api.EntryPaths(tmp_path / "entry")
    paths.ensure()
    c = api.SamsungHealthApi(paths, http=_client(lambda r: httpx.Response(500)))
    yield c
    c.close()


def test_fetch_heals_stale_revision(client, monkeypatch):
    calls = []

    def iterate(manifest, revision):
        calls.append(revision)
        if revision != 5:
            raise SamsungHealthCloudError("document GET failed with HTTP 400")
        return [{"data": {"datauuid": "x"}}]

    monkeypatch.setattr(client, "_iterate", iterate)
    monkeypatch.setattr(api, "server_revision", lambda *a: 5)
    result = client.fetch("com.samsung.shealth.step_daily_trend", 14)
    assert result.revision == 5 and calls == [14, 5] and len(result.documents) == 1


def test_fetch_retries_once_on_401(client, monkeypatch):
    calls = []

    def iterate(manifest, revision):
        calls.append(revision)
        if len(calls) == 1:
            raise SamsungHealthCloudError("document GET failed with HTTP 401")
        return []

    monkeypatch.setattr(client, "_iterate", iterate)
    monkeypatch.setattr(client, "_invalidate_cloud_token", lambda: calls.append("invalidated"))
    monkeypatch.setattr(client, "initialize", lambda: calls.append("init"))
    client.fetch("m.x", 1)
    assert calls == [1, "invalidated", "init", 1]


def test_fetch_gives_up_after_second_401(client, monkeypatch):
    def iterate(manifest, revision):
        raise SamsungHealthCloudError("document GET failed with HTTP 401")

    monkeypatch.setattr(client, "_iterate", iterate)
    monkeypatch.setattr(client, "_invalidate_cloud_token", lambda: None)
    monkeypatch.setattr(client, "initialize", lambda: None)
    with pytest.raises(api.SamsungHealthTransientError):
        client.fetch("m.x", 1)


def test_fetch_404_means_type_unavailable(client, monkeypatch):
    def iterate(manifest, revision):
        raise SamsungHealthCloudError("document GET failed with HTTP 404")

    monkeypatch.setattr(client, "_iterate", iterate)
    assert client.fetch("m.x", 1).documents == []


def test_fetch_retries_server_errors_then_succeeds(client, monkeypatch):
    attempts, pauses = [], []

    def iterate(manifest, revision):
        attempts.append(1)
        if len(attempts) < 3:
            raise SamsungHealthCloudError("document GET failed with HTTP 500")
        return [{"datauuid": "a"}]

    monkeypatch.setattr(client, "_iterate", iterate)
    monkeypatch.setattr(api, "_sleep", pauses.append)
    assert client.fetch("m.x", 1).documents == [{"datauuid": "a"}]
    assert pauses == list(api.SERVER_ERROR_RETRY_DELAYS)


def test_fetch_gives_up_after_repeated_server_errors(client, monkeypatch):
    def iterate(manifest, revision):
        raise SamsungHealthCloudError("document GET failed with HTTP 503")

    monkeypatch.setattr(client, "_iterate", iterate)
    monkeypatch.setattr(api, "_sleep", lambda s: None)
    with pytest.raises(api.SamsungHealthTransientError):
        client.fetch("m.x", 1)


def test_initialize_without_login_is_auth_error(client):
    with pytest.raises(api.SamsungHealthAuthError):
        client.initialize()
