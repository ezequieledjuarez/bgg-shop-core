import asyncio
import importlib.util
from pathlib import Path
from unittest.mock import AsyncMock, patch


def test_asgi_forwards_request_without_logging_authorization(capsys):
    application = AsyncMock()
    path = Path(__file__).resolve().parents[1] / "saleor" / "asgi.py"
    spec = importlib.util.spec_from_file_location("credit_test_asgi", path)
    module = importlib.util.module_from_spec(spec)
    with patch("django.core.asgi.get_asgi_application", return_value=application):
        spec.loader.exec_module(module)
    scope = {"type": "http", "headers": [(b"authorization", b"Bearer synthetic-secret")]}
    receive, send = AsyncMock(), AsyncMock()
    asyncio.run(module.application(scope, receive, send))
    application.assert_awaited_once_with(scope, receive, send)
    captured = capsys.readouterr()
    assert "synthetic-secret" not in captured.out + captured.err