import os
from datetime import datetime, timedelta, timezone
from io import StringIO
from pathlib import Path
import sys
from unittest.mock import patch

import django
import pytest
from django.core.management import call_command
from django.core.management.base import CommandError

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "saleor.settings")
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
django.setup()


def test_cleanup_defaults_to_dry_run():
    now = datetime(2030, 1, 1, tzinfo=timezone.utc)
    output = StringIO()
    with patch("opkeys.models.OpSession.objects.filter") as sessions, patch("django.utils.timezone.now", return_value=now):
        sessions.return_value.count.return_value = 3
        call_command("purge_expired_opkeys", stdout=output)
    sessions.assert_called_once_with(expires_at__lt=now - timedelta(days=30))
    sessions.return_value.delete.assert_not_called()
    assert "3" in output.getvalue()


def test_cleanup_requires_explicit_apply():
    with patch("opkeys.models.OpSession.objects.filter") as sessions:
        sessions.return_value.delete.return_value = (2, {"opkeys.OpSession": 2})
        call_command("purge_expired_opkeys", apply=True, stdout=StringIO())
    sessions.return_value.delete.assert_called_once()


def test_cleanup_rejects_negative_retention():
    with pytest.raises(CommandError):
        call_command("purge_expired_opkeys", retention_days=-1, stdout=StringIO())