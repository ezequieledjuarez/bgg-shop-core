import json
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
