import gzip
import json
import io
import http.client
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
import ssl
import urllib.error
import urllib.parse
import urllib.request


class ApiError(Exception):
    """Sanitized error: never includes request URLs or server response bodies."""


class AuthenticationError(ApiError):
    pass


class TransientError(ApiError):
    def __init__(self, message, retry_after=None):
        super().__init__(message)
        self.retry_after = retry_after


def retry_after_seconds(value):
    try:
        seconds = float(value)
        if seconds >= 0 and seconds < float("inf"):
            return seconds
    except (TypeError, ValueError):
        pass
    try:
        result = parsedate_to_datetime(value)
        return max(0, (result - datetime.now(timezone.utc)).total_seconds())
    except (TypeError, ValueError, OverflowError):
        return None


class BafangClient:
    def __init__(self, token, device_id, host="https://msbg.bafang-e.com.cn:8096", timeout=30):
        if not token or not device_id:
            raise ValueError("Token and userDeviceId are required")
        parsed = urllib.parse.urlsplit(host)
        if parsed.scheme != "https" or parsed.username or parsed.password or not parsed.netloc or parsed.query or parsed.fragment or parsed.path not in ("", "/"):
            raise ValueError("API host must be an HTTPS origin")
        self.token, self.device_id = token, device_id
        self.host, self.timeout = host.rstrip("/"), timeout

    def _get(self, endpoint, params):
        query = urllib.parse.urlencode({**params, "token": self.token})
        request = urllib.request.Request(self.host + "/BaFangCyclingApi/v2/ride/track/" + endpoint + "?" + query,
            headers={"AppClient": "com.momasbikes.ebike", "userDeviceId": self.device_id,
                     "User-Agent": "okhttp/5.0.0-alpha.2", "Accept-Encoding": "gzip"}, method="GET")
        return request_bytes(request, self.timeout)

    def list_page(self, page, size=20):
        body = self._get("list", {"page": page, "size": size, "withOverviewImage": "false", "width": 200, "height": 200})
        try:
            result = json.loads(body)
        except (ValueError, UnicodeError):
            raise ApiError("List response is not JSON") from None
        extract_rides(result)  # Validate before persisting or interpreting.
        return result

    def download_gpx(self, ride_id):
        body = self._get("downloadTrackData", {"id": ride_id, "type": "gpx"})
        if body.lstrip().startswith((b"{", b"[")):
            try:
                extract_rides(json.loads(body))
            except (ValueError, UnicodeError):
                raise ApiError("Download returned invalid JSON") from None
            raise ApiError("Download returned JSON instead of GPX")
        from .timestamps import parse_gpx
        parse_gpx(body)
        return body


def extract_rides(payload):
    if isinstance(payload, list):
        records = payload
    elif isinstance(payload, dict):
        code = payload.get("code")
        if code in (401, 403, "401", "403"):
            raise AuthenticationError("API rejected authentication")
        if code not in (None, 0, 200, "0", "200") or payload.get("success") is False:
            raise ApiError("API reported an application error; check token validity")
        data = payload.get("data", payload)
        if isinstance(data, list):
            records = data
        elif isinstance(data, dict):
            records = next((data[k] for k in ("records", "list", "rows", "content") if isinstance(data.get(k), list)), None)
        else:
            records = None
    else:
        records = None
    if records is None or any(not isinstance(r, dict) or "id" not in r
        or isinstance(r["id"], bool) or not isinstance(r["id"], (str, int))
        or not str(r["id"]).strip() or len(str(r["id"])) > 256 for r in records):
        raise ApiError("Unknown ride list schema")
    return records


def request_bytes(request, timeout):
    # No redirects: a redirect must never forward the query token to another host.
    class NoRedirect(urllib.request.HTTPRedirectHandler):
        def redirect_request(self, req, fp, code, msg, headers, newurl):
            return None
    opener = urllib.request.build_opener(NoRedirect(), urllib.request.HTTPSHandler(context=ssl.create_default_context()))
    try:
        with opener.open(request, timeout=timeout) as response:
            body = response.read(32 * 1024 * 1024 + 1)
            if len(body) > 32 * 1024 * 1024:
                raise ApiError("Response exceeds size limit")
            if response.headers.get("Content-Encoding", "").lower() == "gzip":
                with gzip.GzipFile(fileobj=io.BytesIO(body)) as compressed:
                    body = compressed.read(32 * 1024 * 1024 + 1)
                if len(body) > 32 * 1024 * 1024:
                    raise ApiError("Decompressed response exceeds size limit")
            return body
    except urllib.error.HTTPError as exc:
        if exc.code in (401, 403):
            raise AuthenticationError("Authentication rejected") from None
        if exc.code in (408, 425, 429) or 500 <= exc.code <= 599:
            delay = retry_after_seconds(exc.headers.get("Retry-After")) if exc.headers else None
            raise TransientError(f"API returned HTTP {exc.code}", retry_after=delay) from None
        raise ApiError(f"API returned HTTP {exc.code}") from None
    except (urllib.error.URLError, TimeoutError, OSError, EOFError, http.client.HTTPException):
        raise TransientError("Network or TLS request failed") from None
