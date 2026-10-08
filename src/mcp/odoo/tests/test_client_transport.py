import pytest

from src.core.credentials import OdooCredentials
from src.mcp.odoo.connection import client as client_module
from src.mcp.odoo.connection import json2_transport, xmlrpc_transport
from src.mcp.odoo.connection.client import OdooClient


class FakeResponse:
    def __init__(self, payload):
        self._payload = payload

    def raise_for_status(self):
        return None

    def json(self):
        return self._payload


class FakeAsyncClient:
    instances = []

    def __init__(self, **kwargs):
        self.kwargs = kwargs
        self.calls = []
        FakeAsyncClient.instances.append(self)

    async def get(self, path):
        self.calls.append(("GET", path, None))
        if path == "/web/version":
            return FakeResponse({"version": "19.0"})
        raise AssertionError(f"Unexpected GET {path}")

    async def post(self, path, json):
        self.calls.append(("POST", path, json))
        if path == "/json/2/res.users/context_get":
            return FakeResponse({"uid": 7, "lang": "en_US"})
        if path == "/json/2/crm.lead/search_read":
            return FakeResponse([{"id": 1, "name": "Deal"}])
        if path == "/json/2/res.users/read":
            return FakeResponse(
                [
                    {
                        "id": 7,
                        "name": "Demo User",
                        "email": "demo@example.com",
                        "company_id": [1, "My Company"],
                    }
                ]
            )
        if path == "/json/2/crm.lead/message_post":
            return FakeResponse(501)
        raise AssertionError(f"Unexpected POST {path}")

    async def aclose(self):
        self.calls.append(("CLOSE", None, None))


class FakeAsyncClientV18(FakeAsyncClient):
    async def get(self, path):
        self.calls.append(("GET", path, None))
        if path == "/web/version":
            return FakeResponse({"version": "18.0"})
        raise AssertionError(f"Unexpected GET {path}")


class FakeXmlRpcCommonProxy:
    def authenticate(self, db, username, api_key, context):
        return 11

    def version(self):
        return {"server_version": "19.0"}


class FakeXmlRpcObjectProxy:
    def execute_kw(self, *args, **kwargs):
        return True


def fake_server_proxy_for_v19(url, context=None):
    if url.endswith("/xmlrpc/2/common"):
        return FakeXmlRpcCommonProxy()
    if url.endswith("/xmlrpc/2/object"):
        return FakeXmlRpcObjectProxy()
    raise AssertionError(f"Unexpected ServerProxy URL {url}")


@pytest.fixture
def creds():
    return OdooCredentials(
        url="https://example.odoo.com",
        db="demo-db",
        username="demo@example.com",
        api_key="stored-api-key",
    )


class TestTransportSelection:
    def test_explicit_transport_pins_client_backend(self, creds):
        client = OdooClient(credentials=creds, transport="xmlrpc")

        assert client.transport_name == "xmlrpc"

    def test_legacy_no_argument_local_path_reads_the_configured_transport(self, creds, monkeypatch):
        monkeypatch.setenv("ODOO_TRANSPORT", "json2")
        monkeypatch.setattr(client_module, "get_odoo_credentials", lambda: creds)
        monkeypatch.setattr(json2_transport.httpx, "AsyncClient", FakeAsyncClient)

        client = OdooClient()

        assert client.transport_name == "json2"

    def test_explicit_credentials_require_a_profile_selected_transport(self, creds):
        with pytest.raises(ValueError, match="require an explicit transport"):
            OdooClient(credentials=creds)


class TestJson2Transport:
    @pytest.mark.asyncio
    async def test_json2_auth_and_core_read_mapping(self, creds, monkeypatch):
        FakeAsyncClient.instances = []
        monkeypatch.setattr(json2_transport.httpx, "AsyncClient", FakeAsyncClient)
        monkeypatch.setenv("ODOO_API_KEY", "wrong-profile-api-key")

        client = OdooClient(credentials=creds, transport="json2")
        uid = await client.authenticate()
        records = await client.search_read(
            "crm.lead",
            [["name", "ilike", "Deal"]],
            ["name"],
            limit=5,
            offset=2,
            order="id desc",
        )
        user = await client.get_current_user()
        await client.close()

        assert uid == 7
        assert records == [{"id": 1, "name": "Deal"}]
        assert user["id"] == 7

        http = FakeAsyncClient.instances[0]
        assert http.kwargs["headers"]["Authorization"] == "bearer stored-api-key"
        assert http.kwargs["headers"]["X-Odoo-Database"] == "demo-db"
        assert ("GET", "/web/version", None) in http.calls
        assert (
            "POST",
            "/json/2/crm.lead/search_read",
            {
                "domain": [["name", "ilike", "Deal"]],
                "fields": ["name"],
                "limit": 5,
                "offset": 2,
                "order": "id desc",
            },
        ) in http.calls
        assert (
            "POST",
            "/json/2/res.users/read",
            {
                "ids": [7],
                "fields": ["name", "email", "company_id"],
            },
        ) in http.calls

    @pytest.mark.asyncio
    async def test_json2_direct_note_uses_message_post_mapping(self, creds, monkeypatch):
        FakeAsyncClient.instances = []
        monkeypatch.setattr(json2_transport.httpx, "AsyncClient", FakeAsyncClient)

        client = OdooClient(credentials=creds, transport="json2")
        await client.authenticate()
        message_id = await client.log_note("crm.lead", 42, "hello")
        await client.close()

        assert message_id == 501
        http = FakeAsyncClient.instances[0]
        assert (
            "POST",
            "/json/2/crm.lead/message_post",
            {
                "ids": [42],
                "body": "hello",
                "message_type": "comment",
                "subtype_id": 2,
            },
        ) in http.calls

    @pytest.mark.asyncio
    async def test_json2_rejected_for_odoo_18_or_below(self, creds, monkeypatch):
        FakeAsyncClient.instances = []
        monkeypatch.setattr(json2_transport.httpx, "AsyncClient", FakeAsyncClientV18)

        client = OdooClient(credentials=creds, transport="json2")

        with pytest.raises(ConnectionError, match="Odoo 19 and above"):
            await client.authenticate()

        await client.close()


class TestXmlRpcTransportCompatibility:
    @pytest.mark.asyncio
    async def test_xmlrpc_rejected_for_odoo_19_or_above(self, creds, monkeypatch):
        monkeypatch.setattr(xmlrpc_transport.xmlrpc.client, "ServerProxy", fake_server_proxy_for_v19)

        client = OdooClient(credentials=creds, transport="xmlrpc")

        with pytest.raises(ConnectionError, match="Odoo 18 and below"):
            await client.authenticate()

        await client.close()


class TestModelIdCacheIsolation:
    @pytest.mark.asyncio
    async def test_model_ids_are_not_shared_between_client_instances(self, creds):
        first = OdooClient(credentials=creds, transport="xmlrpc")
        second = OdooClient(credentials=creds, transport="xmlrpc")
        first_calls = 0
        second_calls = 0

        async def first_search_read(*args, **kwargs):
            nonlocal first_calls
            first_calls += 1
            return [{"id": 41}]

        async def second_search_read(*args, **kwargs):
            nonlocal second_calls
            second_calls += 1
            return [{"id": 72}]

        first.search_read = first_search_read
        second.search_read = second_search_read

        assert await first._get_model_id("crm.lead") == 41
        assert await first._get_model_id("crm.lead") == 41
        assert await second._get_model_id("crm.lead") == 72
        assert await second._get_model_id("crm.lead") == 72
        assert first_calls == 1
        assert second_calls == 1
