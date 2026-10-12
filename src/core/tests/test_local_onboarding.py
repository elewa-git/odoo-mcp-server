import pytest

from src.core.local_onboarding import onboard_local_terminal, transport_for_odoo_major


class FakeClient:
    def __init__(self, *, credentials, transport, reject=False):
        self.credentials = credentials
        self.transport = transport
        self.reject = reject
        self.closed = False

    async def authenticate(self):
        if self.reject:
            # Deliberately include the API key: terminal output must redact it.
            raise RuntimeError(f"rejected {self.credentials.api_key}")
        return 7

    async def close(self):
        self.closed = True


def _input_values(*values):
    iterator = iter(values)
    return lambda _prompt: next(iterator)


@pytest.mark.parametrize(
    ("major", "expected_transport"), [(13, "xmlrpc"), (18, "xmlrpc"), (19, "json2"), (None, "json2")]
)
@pytest.mark.asyncio
async def test_local_onboarding_pins_transport_and_persists_only_after_auth(major, expected_transport):
    output = []
    persisted = []
    clients = []

    def client_factory(**kwargs):
        client = FakeClient(**kwargs)
        clients.append(client)
        return client

    result = await onboard_local_terminal(
        input_fn=_input_values("https://odoo.example/", "mydb", "admin", str(major) if major else ""),
        getpass_fn=lambda _prompt: "api-key-that-must-stay-secret",
        output_fn=output.append,
        client_factory=client_factory,
        store_credentials=lambda *args, **kwargs: persisted.append((args, kwargs)),
    )

    assert result is not None
    assert result.transport == expected_transport
    assert clients[0].transport == expected_transport
    assert clients[0].closed is True
    assert persisted == [
        (
            ("https://odoo.example", "mydb", "admin", "api-key-that-must-stay-secret"),
            {"transport": expected_transport},
        )
    ]
    assert "api-key-that-must-stay-secret" not in "\n".join(output)


@pytest.mark.asyncio
async def test_local_onboarding_does_not_persist_or_echo_secret_when_authentication_fails():
    output = []
    persisted = []
    secret = "api-key-that-must-stay-secret"

    result = await onboard_local_terminal(
        input_fn=_input_values("https://odoo.example", "mydb", "user@example.com", "19"),
        getpass_fn=lambda _prompt: secret,
        output_fn=output.append,
        client_factory=lambda **kwargs: FakeClient(**kwargs, reject=True),
        store_credentials=lambda *args: persisted.append(args),
    )

    assert result is None
    assert persisted == []
    assert secret not in "\n".join(output)
    assert output == ["Authentication failed. Check the URL, database, login, secret, and Odoo version."]


def test_transport_for_odoo_major_rejects_invalid_version():
    with pytest.raises(ValueError, match="positive integer"):
        transport_for_odoo_major(0)


@pytest.mark.asyncio
async def test_local_onboarding_rejects_insecure_remote_http_before_secret_input():
    output = []
    secret_prompted = False

    def getpass_fn(_prompt):
        nonlocal secret_prompted
        secret_prompted = True
        return "must-not-be-read"

    result = await onboard_local_terminal(
        input_fn=_input_values("http://odoo.example", "mydb", "user@example.com", "18"),
        getpass_fn=getpass_fn,
        output_fn=output.append,
    )

    assert result is None
    assert secret_prompted is False
    assert output == ["URL, database name, and login / username are required; use HTTPS or local loopback HTTP."]
