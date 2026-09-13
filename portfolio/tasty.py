"""Thin synchronous client for the tastytrade REST API.

Authenticates with OAuth2: a long-lived refresh token is exchanged for a
15-minute access token via POST /oauth/token. Because a first run can spend
several minutes looking up sector data, the access token is re-minted
automatically whenever it is close to expiring, rather than assuming a whole
report fits inside one token's lifetime.

Only about eight endpoints are needed, so this avoids depending on the
`tastytrade` PyPI SDK altogether.

HTTP goes through the standard library rather than `requests`. On some Windows
machines urllib3 (which `requests` sits on) fails its TLS handshake against
api.tastytrade.com with SSLEOFError while raw sockets and `http.client` connect
fine -- typically a TLS-inspecting security product. stdlib `urllib.request`
avoids that path entirely, and costs a dependency less.

All responses are returned as plain dicts with keys normalised from the API's
kebab-case to snake_case.
"""

from __future__ import annotations

import json
import logging
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from decimal import Decimal, InvalidOperation
from typing import Any

log = logging.getLogger(__name__)

# api.tastyworks.com is the legacy hostname and is still what the official SDK
# ships. OAuth tokens are issued by api.tastytrade.com (it is the `iss` claim in
# the refresh token JWT), so that is the host used here.
API_URL = "https://api.tastytrade.com"
CERT_URL = "https://api.cert.tastyworks.com"
# tastytrade requires a User-Agent of the form <product>/<version>.
USER_AGENT = "portfolio-statistics/1.0"

# Refresh this many seconds before the token actually expires, so a request
# never races the expiry.
TOKEN_REFRESH_MARGIN = 120


class TastytradeError(RuntimeError):
    pass


def _snake(key: str) -> str:
    return key.replace("-", "_")


def normalize(obj: Any) -> Any:
    """Recursively rewrite kebab-case keys to snake_case."""
    if isinstance(obj, dict):
        return {_snake(k): normalize(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [normalize(v) for v in obj]
    return obj


def dec(value: Any, default: Decimal = Decimal(0)) -> Decimal:
    """Parse an API numeric field into a Decimal, tolerating None/''/garbage."""
    if value is None or value == "":
        return default
    try:
        return Decimal(str(value))
    except (InvalidOperation, ValueError):
        return default


class TastyClient:
    def __init__(
        self,
        client_id: str,
        client_secret: str,
        refresh_token: str,
        sandbox: bool = False,
    ):
        missing = [
            name
            for name, value in (
                ("TASTYTRADE_CLIENT_ID", client_id),
                ("TASTYTRADE_CLIENT_SECRET", client_secret),
                ("TASTYTRADE_REFRESH_TOKEN", refresh_token),
            )
            if not value
        ]
        if missing:
            raise TastytradeError(
                "Missing OAuth credentials in .env: "
                + ", ".join(missing)
                + ". Create an OAuth application at https://my.tastytrade.com"
                "/app.html#/manage/api-access/oauth-applications, then use "
                "Manage > Create Grant to get a refresh token."
            )
        self.client_id = client_id
        self.client_secret = client_secret
        self.refresh_token = refresh_token
        self.base_url = CERT_URL if sandbox else API_URL
        self.access_token: str | None = None
        self._expires_at: float = 0.0
        # Requests are issued concurrently, so the token must be minted once
        # rather than by every thread that notices it has expired.
        self._token_lock = threading.Lock()

    # ---------------------------------------------------------------- auth

    def login(self) -> None:
        """Exchange the refresh token for a short-lived access token."""
        status, body = self._request(
            "POST",
            "/oauth/token",
            body={
                "grant_type": "refresh_token",
                "refresh_token": self.refresh_token,
                "client_id": self.client_id,
                "client_secret": self.client_secret,
            },
            authenticated=False,
        )
        if status in (400, 401, 403):
            raise TastytradeError(
                f"tastytrade rejected the OAuth credentials (HTTP {status}). "
                "Check TASTYTRADE_CLIENT_ID, TASTYTRADE_CLIENT_SECRET and "
                "TASTYTRADE_REFRESH_TOKEN in .env. If you regenerated the client "
                "secret, the old grant is invalid -- create a new one."
            )
        self._raise_for_status(status, body, "POST /oauth/token")

        # The token fields sit at the top level, but tolerate a data wrapper.
        payload = body.get("data") if isinstance(body.get("data"), dict) else body
        token = payload.get("access_token")
        if not token:
            raise TastytradeError(
                "OAuth exchange succeeded but returned no access_token."
            )
        expires_in = float(dec(payload.get("expires_in"), Decimal(900)))
        self.access_token = token
        self._expires_at = time.monotonic() + expires_in
        log.info("OAuth access token obtained, valid for %ss.", int(expires_in))

    def _valid_token(self) -> bool:
        return bool(self.access_token) and (
            time.monotonic() < self._expires_at - TOKEN_REFRESH_MARGIN
        )

    def _ensure_token(self) -> None:
        """Re-mint the access token if it is missing or about to expire.

        Double-checked under a lock: with concurrent requests, many threads can
        see an expiring token at once, and without this each would start its own
        OAuth exchange.
        """
        if self._valid_token():
            return
        with self._token_lock:
            if self._valid_token():
                return  # another thread refreshed it while we waited
            if self.access_token:
                log.info("Access token near expiry; refreshing.")
            self.login()

    def logout(self) -> None:
        # OAuth access tokens are short-lived and have no revoke endpoint here;
        # dropping the token is enough. The refresh token stays valid.
        self.access_token = None
        self._expires_at = 0.0

    def __enter__(self) -> "TastyClient":
        self.login()
        return self

    def __exit__(self, *exc: Any) -> None:
        self.logout()

    # ------------------------------------------------------------ plumbing

    def _request(
        self,
        method: str,
        path: str,
        params: dict | None = None,
        body: dict | None = None,
        authenticated: bool = True,
        timeout: int = 60,
    ) -> tuple[int, dict]:
        """Issue one request. Returns (status_code, parsed_body).

        HTTP errors come back as a status code rather than raising, so callers
        can distinguish an expired token (401) from a real failure.
        """
        url = f"{self.base_url}{path}"
        if params:
            # doseq handles repeated keys such as symbol[]=A&symbol[]=B.
            url += "?" + urllib.parse.urlencode(params, doseq=True)

        headers = {
            "Accept": "application/json",
            "Content-Type": "application/json",
            "User-Agent": USER_AGENT,
        }
        if authenticated and self.access_token:
            headers["Authorization"] = f"Bearer {self.access_token}"

        data = json.dumps(body).encode("utf-8") if body is not None else None
        req = urllib.request.Request(url, data=data, headers=headers, method=method)

        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                raw = resp.read()
                status = resp.status
        except urllib.error.HTTPError as exc:
            raw = exc.read()
            status = exc.code
        except urllib.error.URLError as exc:
            raise TastytradeError(
                f"{method} {path} could not reach {self.base_url}: {exc.reason}"
            ) from exc

        try:
            parsed = normalize(json.loads(raw.decode("utf-8")))
        except (ValueError, UnicodeDecodeError):
            parsed = {"_raw": raw[:200].decode("utf-8", "replace")}
        return status, parsed

    @staticmethod
    def _raise_for_status(status: int, body: dict, what: str) -> None:
        if 200 <= status < 300:
            return
        error = body.get("error") if isinstance(body.get("error"), dict) else {}
        detail = f"{error.get('code', '')} {error.get('message', '')}".strip()
        if not detail:
            detail = str(body.get("_raw", ""))[:200]
        raise TastytradeError(
            f"{what} failed with HTTP {status}{': ' + detail if detail else ''}"
        )

    def _get(self, path: str, params: dict | None = None) -> Any:
        self._ensure_token()
        status, body = self._request("GET", path, params=params)
        # A token can still lapse between the check and the call; re-auth once.
        if status == 401:
            log.info("Got 401 on %s; re-authenticating and retrying once.", path)
            with self._token_lock:
                if not self._valid_token():
                    self.login()
            status, body = self._request("GET", path, params=params)
        self._raise_for_status(status, body, f"GET {path}")
        return body.get("data", {})

    def _get_items(self, path: str, params: dict | None = None) -> list[dict]:
        data = self._get(path, params)
        if isinstance(data, dict) and "items" in data:
            return data["items"]
        return data if isinstance(data, list) else []

    # ----------------------------------------------------------- endpoints

    def accounts(self, include_closed: bool = False) -> list[dict]:
        """GET /customers/me/accounts -- each item wraps the account in 'account'."""
        items = self._get_items("/customers/me/accounts")
        out = []
        for item in items:
            acct = item.get("account", item)
            if acct.get("is_closed") and not include_closed:
                continue
            out.append(acct)
        return out

    def balances(self, account_number: str) -> dict:
        return self._get(f"/accounts/{account_number}/balances")

    def positions(self, account_number: str) -> list[dict]:
        return self._get_items(
            f"/accounts/{account_number}/positions", {"include-marks": "true"}
        )

    def margin_requirements(self, account_number: str) -> dict:
        """Per-position margin breakdown. Requires a margin account; may 404."""
        try:
            return self._get(f"/margin/accounts/{account_number}/requirements")
        except TastytradeError as exc:
            log.warning("Margin requirements unavailable for %s: %s", account_number, exc)
            return {}

    def market_metrics(self, symbols: list[str]) -> list[dict]:
        """Beta, SPY correlation, IV rank, market cap, dividend yield, earnings."""
        out: list[dict] = []
        unique = sorted({s for s in symbols if s})
        for i in range(0, len(unique), 100):  # endpoint caps the symbol list
            batch = unique[i : i + 100]
            try:
                out.extend(self._get_items("/market-metrics", {"symbols": ",".join(batch)}))
            except TastytradeError as exc:
                log.warning("market-metrics failed for %s: %s", batch, exc)
        return out

    def future(self, symbol: str) -> dict:
        """GET /instruments/futures/{symbol} -- the leading slash must be stripped."""
        return self._get(f"/instruments/futures/{symbol.lstrip('/')}")

    def future_option(self, symbol: str) -> dict:
        return self._get(f"/instruments/future-options/{symbol.lstrip('/')}")

    def equities(self, symbols: list[str]) -> list[dict]:
        out: list[dict] = []
        unique = sorted({s for s in symbols if s})
        for i in range(0, len(unique), 100):
            batch = unique[i : i + 100]
            try:
                out.extend(self._get_items("/instruments/equities", {"symbol[]": batch}))
            except TastytradeError as exc:
                log.warning("equities lookup failed for %s: %s", batch, exc)
        return out

    def market_data(
        self,
        equities: list[str] | None = None,
        options: list[str] | None = None,
        futures: list[str] | None = None,
        future_options: list[str] | None = None,
        indices: list[str] | None = None,
        cryptocurrencies: list[str] | None = None,
    ) -> list[dict]:
        """GET /market-data/by-type. Combined limit is 100 symbols per call, so
        requests are chunked; returns quotes with mark/last/close and beta."""
        buckets = {
            "equity": list(dict.fromkeys(equities or [])),
            "equity-option": list(dict.fromkeys(options or [])),
            "future": list(dict.fromkeys(futures or [])),
            "future-option": list(dict.fromkeys(future_options or [])),
            "index": list(dict.fromkeys(indices or [])),
            "cryptocurrency": list(dict.fromkeys(cryptocurrencies or [])),
        }
        # Flatten to (bucket, symbol) pairs so chunks can span buckets.
        pairs = [(k, s) for k, syms in buckets.items() for s in syms if s]
        out: list[dict] = []
        for i in range(0, len(pairs), 90):
            chunk = pairs[i : i + 90]
            params: dict[str, str] = {}
            for key, sym in chunk:
                params[key] = f"{params[key]},{sym}" if key in params else sym
            try:
                out.extend(self._get_items("/market-data/by-type", params))
            except TastytradeError as exc:
                log.warning("market-data failed for chunk %s: %s", chunk, exc)
        return out

    def net_liq_history(self, account_number: str, time_back: str = "1y") -> list[dict]:
        """Daily net liquidating value history (OHLC per day)."""
        try:
            return self._get_items(
                f"/accounts/{account_number}/net-liq/history", {"time-back": time_back}
            )
        except TastytradeError as exc:
            log.warning("Net liq history unavailable for %s: %s", account_number, exc)
            return []
