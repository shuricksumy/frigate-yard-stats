"""Tests for the web UI's sub-path safety (static/app.js, static/admin.js).

The UI is served from /ui/ but calls an API that lives one level above it. Every URL it builds
therefore has to be resolved relative to the page's own location -- a leading-"/" URL resolves
against the ORIGIN, which escapes the sub-path entirely when the app is behind a reverse proxy
(Home Assistant ingress serves it under /api/hassio_ingress/<token>/ui/, so "/events" would hit
Home Assistant itself and 404).

These tests run the real JS through node rather than reading it as text, because the failure mode
that actually happened here was semantic, not syntactic: an automated rewrite turned apiUrl's own
body into `return apiUrl(...)` -- infinite recursion that `node --check` passes happily. Only
calling the function catches that.

Skipped (not failed) when node isn't installed, so a Python-only dev environment still runs the
rest of the suite. GitHub's ubuntu-latest runners ship node, so this does execute in CI.
"""
import json
import os
import re
import shutil
import subprocess

import pytest

STATIC_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "static")

# (name, document.baseURI, expected API_BASE) -- the three deployment shapes this has to support.
DEPLOYMENTS = [
    ("standalone", "http://host:8080/ui/", "http://host:8080"),
    ("ha_addon_ingress", "https://ha/api/hassio_ingress/TOK/ui/", "https://ha/api/hassio_ingress/TOK"),
    ("hass_ingress", "https://ha/api/ingress/yard/ui/", "https://ha/api/ingress/yard"),
]

# Evaluates one of the UI scripts with a stubbed document, then reports what its own API_BASE and
# apiUrl() actually produce. Deliberately CALLS apiUrl instead of just reading API_BASE.
_DRIVER = """
const fs = require("fs");
const vm = require("vm");
const [file, baseURI] = process.argv.slice(2);
const context = {
  document: { baseURI, cookie: "" },
  window: {},
  console,
  URL,
  setInterval: () => 0,
  clearInterval: () => {},
  fetch: () => Promise.reject(new Error("no network in tests")),
};
vm.createContext(context);
// The script's own values are read by appending an expression to it rather than via
// context.API_BASE: `const` creates a lexical binding, not a property of the context object, so
// it is invisible from outside. apiUrl is CALLED here, not just referenced -- that is what makes
// a self-recursive rewrite fail loudly instead of passing.
const source = fs.readFileSync(file, "utf8") +
  '\\n;({ api_base: API_BASE, events: apiUrl("/events"), nested: apiUrl("/events/12/thumbnail") })';
const out = vm.runInContext(source, context, { filename: file, timeout: 5000 });
process.stdout.write(JSON.stringify(out));
"""


def _node():
    exe = shutil.which("node")
    if not exe:
        pytest.skip("node not installed -- skipping web UI URL tests")
    return exe


@pytest.fixture(scope="module")
def driver(tmp_path_factory):
    path = tmp_path_factory.mktemp("jsdriver") / "driver.js"
    path.write_text(_DRIVER)
    return str(path)


def _run(driver, script_name, base_uri):
    result = subprocess.run(
        [_node(), driver, os.path.join(STATIC_DIR, script_name), base_uri],
        capture_output=True, text=True, timeout=30,
    )
    assert result.returncode == 0, f"node failed for {script_name}: {result.stderr}"
    return json.loads(result.stdout)


@pytest.mark.parametrize("script", ["app.js", "admin.js"])
@pytest.mark.parametrize("name,base_uri,expected_base", DEPLOYMENTS)
def test_api_base_resolves_for_every_deployment(driver, script, name, base_uri, expected_base):
    out = _run(driver, script, base_uri)
    assert out["api_base"] == expected_base, f"{script} under {name}"


@pytest.mark.parametrize("script", ["app.js", "admin.js"])
@pytest.mark.parametrize("name,base_uri,expected_base", DEPLOYMENTS)
def test_apiurl_builds_urls_under_the_subpath(driver, script, name, base_uri, expected_base):
    # Calling apiUrl (rather than only reading API_BASE) is the point: a rewrite that made this
    # function call itself passed every syntax check and would fail only here.
    out = _run(driver, script, base_uri)
    assert out["events"] == f"{expected_base}/events"
    assert out["nested"] == f"{expected_base}/events/12/thumbnail"


# Any string/template literal that looks like an API path ("/events", "/admin/overview", ...).
# Deliberately NOT restricted to `fetch("/` -- the first version of this test only looked for that
# and missed a real one: admin.js's generateReport built `let url = \`/reports/generate?...\`` on
# its own line and then called fetch(url), which is just as broken under a sub-path but matches no
# fetch("/ pattern. Matching the literal itself catches the URL wherever it is built.
_ABSOLUTE_PATH_LITERAL = re.compile(r"""["'`]/[a-z][a-z0-9/_-]*""", re.IGNORECASE)
# A literal is fine if the line routes it through one of these.
_SAFE_WRAPPERS = ("apiUrl(", "this._get(", "this._post(")


@pytest.mark.parametrize("script", ["app.js", "admin.js"])
def test_no_absolute_path_url_literals_remain(script):
    # Acceptance criterion from the task brief: nothing may build a URL starting with "/", since
    # that resolves against the origin and escapes the sub-path. Catches a call site added by hand
    # without going through apiUrl().
    source = open(os.path.join(STATIC_DIR, script)).read()
    offenders = []
    for lineno, line in enumerate(source.split("\n"), start=1):
        stripped = line.strip()
        if stripped.startswith("//") or stripped.startswith("*"):
            continue
        if not _ABSOLUTE_PATH_LITERAL.search(line):
            continue
        if any(wrapper in line for wrapper in _SAFE_WRAPPERS):
            continue
        offenders.append(f"{script}:{lineno}: {stripped}")
    assert not offenders, (
        "absolute-path URL literal(s) not routed through apiUrl()/_get()/_post():\n"
        + "\n".join(offenders)
    )


@pytest.mark.parametrize("script", ["app.js", "admin.js"])
def test_apiurl_is_not_self_recursive(script):
    # Guards the exact regression above at the source level too, so the reason is obvious to anyone
    # reading this file rather than only showing up as a node timeout/stack overflow.
    source = open(os.path.join(STATIC_DIR, script)).read()
    start = source.index("function apiUrl(")
    body = source[start:source.index("}", start)]
    assert "apiUrl(" not in body[len("function apiUrl("):], f"{script}: apiUrl() calls itself"


# ---- server-side: the /ui -> /ui/ redirect ----

# Driven through the ASGI interface directly rather than fastapi.testclient.TestClient, which
# needs httpx -- not a dependency of this project (see requirements.txt). Adding one just for a
# test would break the "no new runtime dependency" requirement, and it is not needed: a FastAPI app
# IS an ASGI callable, so a scope plus a send/receive pair exercises the real routing, the real
# mount, and the real response headers with nothing extra installed.
def _asgi_get(path: str) -> tuple[int, dict, bytes]:
    import asyncio

    import api

    scope = {
        "type": "http", "asgi": {"version": "3.0", "spec_version": "2.3"},
        "http_version": "1.1", "method": "GET", "scheme": "http",
        "path": path, "raw_path": path.encode(), "query_string": b"",
        "root_path": "", "headers": [(b"host", b"testserver")],
        "client": ("127.0.0.1", 12345), "server": ("testserver", 80),
    }
    messages: list[dict] = []

    async def receive():
        return {"type": "http.request", "body": b"", "more_body": False}

    async def send(message):
        messages.append(message)

    asyncio.run(api.app(scope, receive, send))
    start = next(m for m in messages if m["type"] == "http.response.start")
    headers = {k.decode().lower(): v.decode() for k, v in start["headers"]}
    body = b"".join(m.get("body", b"") for m in messages if m["type"] == "http.response.body")
    return start["status"], headers, body


def test_ui_redirect_location_is_relative_not_absolute():
    # StaticFiles(html=True) would emit an ABSOLUTE Location built from the backend's own address
    # (confirmed: "http://127.0.0.1:8899/ui/"). Behind a proxy that does no response rewriting --
    # which the Home Assistant add-on deliberately doesn't -- that sends the browser to the
    # backend's internal address. A relative Location resolves against whatever the browser
    # actually requested, so it works under any prefix without the server knowing about it.
    status, headers, _ = _asgi_get("/ui")
    assert status in (301, 302, 307, 308)
    location = headers["location"]
    assert not location.startswith("/"), f"absolute-path Location escapes the sub-path: {location}"
    assert not location.startswith("http"), f"absolute Location leaks the backend address: {location}"
    assert location == "ui/"


@pytest.mark.parametrize("requested,expected", [
    ("http://host:8080/ui", "http://host:8080/ui/"),
    ("https://ha/api/hassio_ingress/TOK/ui", "https://ha/api/hassio_ingress/TOK/ui/"),
    ("https://ha/api/ingress/yard/ui", "https://ha/api/ingress/yard/ui/"),
])
def test_ui_redirect_resolves_to_the_right_url_in_every_deployment(requested, expected):
    # What the browser will actually do with that Location header.
    from urllib.parse import urljoin
    _, headers, _ = _asgi_get("/ui")
    assert urljoin(requested, headers["location"]) == expected


@pytest.mark.parametrize("path", ["/ui/", "/ui/admin", "/ui/app.js"])
def test_ui_and_admin_pages_still_serve(path):
    status, _, _ = _asgi_get(path)
    assert status == 200, f"{path} returned {status}"


# ---- static asset caching ----

@pytest.mark.parametrize("path", ["/ui/", "/ui/admin", "/ui/admin.js", "/ui/app.js", "/ui/style.css"])
def test_ui_assets_must_be_revalidated(path):
    # Plain StaticFiles sends etag/last-modified but no Cache-Control, so browsers fall back to
    # HEURISTIC caching and may reuse a stored copy without asking. That shipped a real breakage:
    # after an upgrade the browser loaded the new admin.html but reused the OLD admin.js, so the
    # markup called handlers the cached script didn't define and the page died with
    # "delOpenPreview is not defined". HTML and its scripts must never be able to drift apart.
    _, headers, _ = _asgi_get(path)
    assert headers.get("cache-control") == "no-cache", f"{path} may be served stale from cache"


def test_revalidation_is_cheap_not_a_redownload():
    # no-cache means "revalidate", not "never store" -- an unchanged asset comes back as a bodyless
    # 304, so the cost is a round trip rather than re-downloading every asset on every page load.
    _, headers, body = _asgi_get("/ui/admin.js")
    assert headers.get("etag"), "an ETag is what makes revalidation cheap"
    assert len(body) > 0
