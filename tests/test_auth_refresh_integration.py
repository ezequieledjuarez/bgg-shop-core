import json

from saleor import auth_refresh

# If Django is installed in the environment, ensure minimal settings so JsonResponse works
try:
    from django.conf import settings
    if not settings.configured:
        settings.configure(DEFAULT_CHARSET='utf-8')
except Exception:
    pass


def _make_request(body: dict):
    # Create a minimal request-like object so tests work whether or not Django is installed
    class _Req:
        pass

    req = _Req()
    req.method = 'POST'
    req.body = json.dumps(body).encode('utf-8')
    return req


def _unwrap_response(resp):
    # Handle fallback JsonResponse stub or Django HttpResponse
    if isinstance(resp, dict) and 'status' in resp and 'data' in resp:
        return resp['status'], resp['data']
    # Django HttpResponse-like
    status = getattr(resp, 'status_code', None)
    try:
        data = resp.json()
    except Exception:
        try:
            data = json.loads(getattr(resp, 'content', b'{}'))
        except Exception:
            data = {}
    return status, data


def test_refresh_rotates_and_accepts_one_use():
    auth_refresh.clear_store()
    tokens = auth_refresh.issue_for_user('user-1')
    old = tokens['refresh_token']

    # first consumption succeeds and returns new refresh
    req = _make_request({'refresh_token': old})
    status, data = _unwrap_response(auth_refresh.refresh_view(req))
    assert status == 200
    assert 'access_token' in data and 'refresh_token' in data
    new = data['refresh_token']
    assert new != old

    # reusing old should fail (replay protection)
    req2 = _make_request({'refresh_token': old})
    status2, data2 = _unwrap_response(auth_refresh.refresh_view(req2))
    assert status2 == 401


def test_refresh_invalid_token_returns_401():
    auth_refresh.clear_store()
    req = _make_request({'refresh_token': 'bogus-token'})
    status, data = _unwrap_response(auth_refresh.refresh_view(req))
    assert status == 401


def test_refresh_missing_token_returns_400():
    auth_refresh.clear_store()
    req = _make_request({})
    status, data = _unwrap_response(auth_refresh.refresh_view(req))
    assert status == 400
