import pytest


@pytest.fixture(autouse=True)
def auto_enable_custom_integrations(enable_custom_integrations):
    yield


@pytest.fixture(autouse=True)
def allow_loopback(socket_enabled):
    """The tests talk to a fake PLCComS server on 127.0.0.1 (tests/fake_plccoms.py)."""
    yield
