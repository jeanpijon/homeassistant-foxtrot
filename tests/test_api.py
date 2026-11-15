"""TecoApi client: request building, auth fallback and SetObject value encoding."""
from __future__ import annotations

from unittest.mock import MagicMock

import pytest
from aiohttp import BasicAuth, ClientResponseError

from custom_components.foxtrot_teco import make_unique_id
from custom_components.foxtrot_teco.api import TecoApiClient


class FakeResponse:
    def __init__(self, status=200, body=None):
        self.status = status
        self.headers = {}
        self._body = body if body is not None else {}

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    def raise_for_status(self):
        if self.status >= 400:
            raise ClientResponseError(MagicMock(), (), status=self.status)

    async def json(self):
        return self._body


class FakeSession:
    """Records GETs and answers from a queue of responses."""

    def __init__(self, *responses):
        self.responses = list(responses)
        self.calls = []

    def get(self, url, params=None, auth=None, ssl=None):
        self.calls.append({"url": url, "params": params, "auth": auth})
        return self.responses.pop(0)


def client(session, auth_type="basic"):
    return TecoApiClient("http://plc.example/", session, "user", "pw", auth_type, verify_ssl=False)


async def test_get_list_keeps_only_ha_prefixed_names():
    session = FakeSession(FakeResponse(body={"ha_a": 1, "other": 2, "ha_b": 3}))
    assert await client(session).get_list() == ["ha_a", "ha_b"]
    assert session.calls[0]["url"] == "http://plc.example/TecoApi/GetList"


async def test_get_object_asks_for_the_variable():
    session = FakeSession(FakeResponse(body={"ha_x": {"pv": 21.5}}))
    assert await client(session).get_object("ha_x") == {"ha_x": {"pv": 21.5}}
    assert session.calls[0]["params"] == {"ha_x": ""}


async def test_401_falls_back_to_the_other_auth():
    session = FakeSession(FakeResponse(status=401), FakeResponse(body={"ok": 1}))
    result = await client(session, auth_type="digest").get_info()
    assert result == {"ok": 1}
    assert len(session.calls) == 2
    assert isinstance(session.calls[1]["auth"], BasicAuth)


async def test_other_http_errors_are_raised():
    session = FakeSession(FakeResponse(status=500))
    with pytest.raises(ClientResponseError):
        await client(session).get_info()


@pytest.mark.parametrize(
    ("value", "expected"),
    [(True, "true"), (False, "false"), (21, "21"), (21.5, "21.5"), ("auto", '"auto"')],
)
async def test_set_object_encodes_values(value, expected):
    session = FakeSession(FakeResponse(body={}))
    await client(session).set_object("ha_room", {"sp": value})
    assert session.calls[0]["params"] == {"ha_room.sp": expected}
    assert session.calls[0]["url"] == "http://plc.example/TecoApi/SetObject"


async def test_set_object_needs_exactly_one_field():
    with pytest.raises(ValueError):
        await client(FakeSession()).set_object("ha_room", {"sp": 1, "mode": 2})


async def test_set_object_gives_up_when_all_auth_is_refused():
    session = FakeSession(FakeResponse(status=401), FakeResponse(status=401))
    with pytest.raises(RuntimeError, match="SetObject failed"):
        await client(session, auth_type="auto").set_object("ha_room", {"sp": 1})


def test_unique_id_is_stable_and_distinguishes_inputs():
    a = make_unique_id("123", 1, "room", "ha_room")
    assert a == make_unique_id("123", 1, "room", "ha_room")
    assert a != make_unique_id("124", 1, "room", "ha_room")
    assert a != make_unique_id("123", 2, "room", "ha_room")
    assert a.startswith("teco_") and len(a) == len("teco_") + 20
