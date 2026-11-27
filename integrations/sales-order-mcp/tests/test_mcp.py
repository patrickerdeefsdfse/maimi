import pytest
from order_mcp.cloud import create_app

def test_private_service_requires_dedicated_authentication(monkeypatch):
    monkeypatch.delenv("MAIMI_ORDER_MCP_SECRET",raising=False)
    with pytest.raises(ValueError,match="认证"):
        create_app()
