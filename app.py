"""Portfolio Statistics -- desktop dashboard for a tastytrade account.

Runs a small FastAPI server on localhost and opens it in a native desktop window.
Start it with run.bat, or `python app.py`. Use `--browser` to open in your default
browser instead of the desktop window, and `--json` to dump the raw report and exit.
"""

from __future__ import annotations

import argparse
import json
import logging
import socket
import sys
import threading
import time
from pathlib import Path

import uvicorn
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from portfolio.analytics import build_report
from portfolio.config import load_settings
from portfolio.correlation import PriceHistory
from portfolio.tasty import TastyClient, TastytradeError

logging.basicConfig(
    level=logging.INFO, format="%(asctime)s  %(levelname)-7s %(name)s: %(message)s"
)
log = logging.getLogger("portfolio")

ROOT = Path(__file__).resolve().parent
WEB_DIR = ROOT / "web"

app = FastAPI(title="Portfolio Statistics")

_cache: dict = {"report": None, "fetched_at": 0.0}
_lock = threading.Lock()

# UI preferences are stored server-side rather than in localStorage: the app
# serves on a different port each launch, and localStorage is scoped to the
# origin *including the port*, so a browser-side preference would never survive
# a restart.
PREFS_FILE = ROOT / "data" / "ui_state.json"
DEFAULT_PREFS = {
    "account": "ALL",
    "tab": "by_symbol",
    "basis": "pct_of_gross",
    "ex_futures": False,
    "sort": "notional",
    "sort_dir": "desc",
    "collapsed": [],  # panel ids the user has folded away
}


def read_prefs() -> dict:
    try:
        stored = json.loads(PREFS_FILE.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return dict(DEFAULT_PREFS)
    return {**DEFAULT_PREFS, **{k: v for k, v in stored.items() if k in DEFAULT_PREFS}}


def write_prefs(values: dict) -> dict:
    prefs = {**read_prefs(), **{k: v for k, v in values.items() if k in DEFAULT_PREFS}}
    try:
        PREFS_FILE.parent.mkdir(parents=True, exist_ok=True)
        PREFS_FILE.write_text(json.dumps(prefs, indent=2), encoding="utf-8")
    except OSError as exc:
        log.warning("Could not save UI preferences: %s", exc)
    return prefs


DEMO = False


def fetch_report() -> dict:
    if DEMO:
        return demo_report()
    settings = load_settings()
    client = TastyClient(
        settings.client_id,
        settings.client_secret,
        settings.refresh_token,
        settings.sandbox,
    )
    with client:
        return build_report(client, settings.accounts or None)


def demo_report() -> dict:
    """A stub portfolio, for checking the UI without hitting the API."""
    sys.path.insert(0, str(ROOT / "tests"))
    from test_analytics import FakeClient, StubClassifier

    from portfolio.analytics import PortfolioBuilder

    report = PortfolioBuilder(
        FakeClient(),
        StubClassifier(
            {
                "AAPL": ("Technology", "Consumer Electronics"),
                "XOM": ("Energy", "Oil & Gas Integrated"),
            }
        ),
    ).build()
    for view in report["views"].values():
        view["warnings"] = ["DEMO MODE - this is stub data, not your account."]
    return report


@app.get("/api/report")
def api_report(refresh: bool = False):
    with _lock:
        fresh = _cache["report"] is not None and (time.time() - _cache["fetched_at"]) < 60
        if fresh and not refresh:
            return _cache["report"]
        try:
            report = fetch_report()
        except TastytradeError as exc:
            raise HTTPException(status_code=502, detail=str(exc)) from exc
        except Exception as exc:  # noqa: BLE001 - surface anything else to the UI
            log.exception("Report build failed")
            raise HTTPException(status_code=500, detail=str(exc)) from exc
        _cache["report"] = report
        _cache["fetched_at"] = time.time()
        return report


@app.get("/api/whatif")
def api_whatif(symbols: str = "", account: str = "ALL", ex_futures: bool = False):
    """Correlation of prospective symbols against the current book.

    Weights come from the selected view's net notional, so the comparison is
    against the portfolio as actually held, not an equal-weighted basket.
    """
    wanted = [s.strip().upper() for s in symbols.split(",") if s.strip()]
    if not wanted:
        raise HTTPException(status_code=400, detail="Provide at least one symbol.")
    if len(wanted) > 20:
        raise HTTPException(status_code=400, detail="At most 20 symbols at a time.")

    report = _cache["report"]
    if report is None:
        raise HTTPException(
            status_code=409, detail="Load the portfolio first, then try again."
        )
    # Honour the "Equities only" toggle, so the comparison is against the same
    # book the rest of the page is showing.
    key = "views_ex_futures" if ex_futures else "views"
    view = report.get(key, {}).get(account)
    if view is None:
        raise HTTPException(status_code=404, detail=f"Unknown account {account!r}.")

    weights = {
        row["name"]: row["net_notional"]
        for row in view["allocation"]["by_symbol"]
        if row.get("net_notional")
    }
    if not weights:
        raise HTTPException(
            status_code=409, detail="This account has no positions to compare against."
        )

    try:
        return PriceHistory().what_if(weights, wanted)
    except Exception as exc:  # noqa: BLE001 - surface the reason to the UI
        log.exception("What-if failed")
        raise HTTPException(status_code=500, detail=str(exc)) from exc


@app.get("/api/prefs")
def api_get_prefs():
    return read_prefs()


@app.post("/api/prefs")
async def api_set_prefs(request: Request):
    try:
        body = await request.json()
    except Exception:  # noqa: BLE001 - malformed body is a client error, not a crash
        raise HTTPException(status_code=400, detail="Expected a JSON object.") from None
    if not isinstance(body, dict):
        raise HTTPException(status_code=400, detail="Expected a JSON object.")
    return write_prefs(body)


@app.exception_handler(HTTPException)
def http_error(_request, exc: HTTPException):
    return JSONResponse(status_code=exc.status_code, content={"detail": exc.detail})


@app.get("/")
def index():
    return FileResponse(WEB_DIR / "index.html")


app.mount("/static", StaticFiles(directory=WEB_DIR), name="static")


DEFAULT_PORT = 8765


def free_port() -> int:
    """Prefer a stable port so the URL stays consistent between launches;
    fall back to any free one if it is taken."""
    try:
        with socket.socket() as sock:
            sock.bind(("127.0.0.1", DEFAULT_PORT))
        return DEFAULT_PORT
    except OSError:
        with socket.socket() as sock:
            sock.bind(("127.0.0.1", 0))
            return sock.getsockname()[1]


def serve(port: int) -> None:
    uvicorn.run(app, host="127.0.0.1", port=port, log_level="warning")


def wait_for_server(port: int, timeout: float = 20.0) -> bool:
    """Block until uvicorn is accepting connections, so the window never opens
    onto a dead port."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=0.5):
                return True
        except OSError:
            time.sleep(0.1)
    return False


def main() -> int:
    parser = argparse.ArgumentParser(description="tastytrade portfolio dashboard")
    parser.add_argument("--browser", action="store_true",
                        help="open in the default browser instead of a desktop window")
    parser.add_argument("--json", action="store_true",
                        help="print the report as JSON and exit (no UI)")
    parser.add_argument("--port", type=int, default=0, help="port to serve on")
    parser.add_argument("--demo", action="store_true",
                        help="render stub data instead of calling the API")
    args = parser.parse_args()

    global DEMO
    DEMO = args.demo

    if args.json:
        try:
            print(json.dumps(fetch_report(), indent=2))
        except TastytradeError as exc:
            print(f"Error: {exc}", file=sys.stderr)
            return 1
        return 0

    port = args.port or free_port()
    url = f"http://127.0.0.1:{port}/"
    threading.Thread(target=serve, args=(port,), daemon=True).start()
    if not wait_for_server(port):
        print(f"Server failed to start on port {port}.", file=sys.stderr)
        return 1

    if args.browser:
        import webbrowser

        webbrowser.open(url)
        print(f"Serving at {url} -- press Ctrl+C to stop.")
        try:
            while True:
                time.sleep(1)
        except KeyboardInterrupt:
            return 0

    try:
        import webview

        webview.create_window("Portfolio Statistics", url, width=1500, height=950)
        webview.start()
    except Exception as exc:  # noqa: BLE001 - fall back rather than crash
        log.warning("Desktop window unavailable (%s); opening in browser.", exc)
        import webbrowser

        webbrowser.open(url)
        try:
            while True:
                time.sleep(1)
        except KeyboardInterrupt:
            return 0
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
