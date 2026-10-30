import json
import os
import subprocess
import sys

import pytest

from saleor.auth_refresh import issue_for_user, clear_store, _hash_token, _consume_refresh


def test_issue_and_rotate_refresh_token():
    clear_store()
    tokens = issue_for_user('user-1')
    assert 'access_token' in tokens and 'refresh_token' in tokens
    old_refresh = tokens['refresh_token']
    # consume once -> should return new refresh
    new_refresh = _consume_refresh(old_refresh)
    assert new_refresh is not None and new_refresh != old_refresh
    # consuming old_refresh again should fail (replay protection)
    again = _consume_refresh(old_refresh)
    assert again is None


def test_invalid_refresh_token_fails():
    clear_store()
    bad = 'refresh-badtoken'
    res = _consume_refresh(bad)
    assert res is None


@pytest.mark.parametrize("secret", ["django-insecure-super-secret-key", "super-secret-key", "a" * 50])
def test_non_debug_settings_reject_insecure_signing_keys(secret):
    result = subprocess.run(
        [sys.executable, "-c", "import saleor.settings"],
        env={**os.environ, "DEBUG": "false", "SECRET_KEY": secret},
        capture_output=True,
        text=True,
    )
    assert result.returncode != 0
    assert secret not in result.stdout + result.stderr


@pytest.mark.parametrize("debug,secret", [
    ("true", "django-insecure-super-secret-key"),
    ("false", "test-0123456789-ABCDEFGHIJKLMNOPQRSTUVWXYZ-abcdefghijklmnop"),
])
def test_settings_accept_explicit_supported_key_configuration(debug, secret):
    result = subprocess.run(
        [sys.executable, "-c", "import saleor.settings"],
        env={**os.environ, "DEBUG": debug, "SECRET_KEY": secret},
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0


def test_settings_load_previous_keys_from_json():
    result = subprocess.run(
        [sys.executable, "-c", "import saleor.settings as settings; assert settings.SECRET_KEY_FALLBACKS == ['old,key', 'older-key']"],
        env={**os.environ, "DEBUG": "true", "SECRET_KEY_FALLBACKS": '["old,key", "older-key"]'},
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0


@pytest.mark.parametrize("fallbacks", ['not-json-secret', '"not-a-list-secret"', '["", "test-secret"]', '[123]'])
def test_settings_reject_invalid_previous_keys_without_leaking_values(fallbacks):
    result = subprocess.run(
        [sys.executable, "-c", "import saleor.settings"],
        env={**os.environ, "DEBUG": "true", "SECRET_KEY_FALLBACKS": fallbacks},
        capture_output=True,
        text=True,
    )
    assert result.returncode != 0
    assert fallbacks not in result.stdout + result.stderr
