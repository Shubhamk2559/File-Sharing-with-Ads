"""Regression tests for the HTTP 422 on POST /api/smart/start.

Root cause: ads/routes.py had `from __future__ import annotations` while FastAPI's Request was imported *inside*
register(). FastAPI then could not resolve the string annotation "Request" and treated `request` as a required query
parameter -> 422 {"loc": ["query","request"], "type": "missing"}. These tests fail if that ever comes back.
"""
import ast
import inspect
import sys
import types
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def _install_stub_fastapi():
    """Tiny stand-in so the wiring can be inspected where FastAPI isn't installed. Not used if the real one imports."""
    fa = types.ModuleType("fastapi")
    class Request: ...
    class HTTPException(Exception): ...
    fa.Request, fa.HTTPException, fa.Header = Request, HTTPException, lambda default=None: default
    exh = types.ModuleType("fastapi.exception_handlers")
    async def _default(request, exc): return None
    exh.request_validation_exception_handler = _default
    exc = types.ModuleType("fastapi.exceptions")
    class RequestValidationError(Exception): ...
    exc.RequestValidationError = RequestValidationError
    resp = types.ModuleType("fastapi.responses")
    for n in ("HTMLResponse", "JSONResponse", "PlainTextResponse", "Response"):
        setattr(resp, n, type(n, (), {"__init__": lambda self, *a, **k: None}))
    sys.modules.update({"fastapi": fa, "fastapi.exception_handlers": exh, "fastapi.exceptions": exc, "fastapi.responses": resp})


try:
    import fastapi  # noqa: F401
    REAL_FASTAPI = True
except ImportError:
    _install_stub_fastapi()
    REAL_FASTAPI = False


class FakeApp:
    def __init__(self): self.routes = {}; self.handlers = {}
    def _reg(self, method):
        def deco(path, **_):
            def wrap(fn): self.routes[(method, path)] = fn; return fn
            return wrap
        return deco
    get = property(lambda s: s._reg("GET")); post = property(lambda s: s._reg("POST"))
    def exception_handler(self, exc):
        def wrap(fn): self.handlers[exc] = fn; return fn
        return wrap


class WiringTests(unittest.TestCase):
    def setUp(self):
        from ads import routes, settings
        self.routes, self.settings = routes, settings
        self.old = settings.ADS_ADMIN_TOKEN
        settings.ADS_ADMIN_TOKEN = "admintoken"
        from ads.verify import Policy
        self.policy = Policy(); self.policy.monetag_postback = False
        self.app = FakeApp()
        routes.register(self.app, auth=None, identify=None, send_file=None, db=object(), token_re=None,
                        telegram_errors=(), forbidden=(), policy=self.policy, manager=None, ads_required=1)

    def tearDown(self):
        self.settings.ADS_ADMIN_TOKEN = self.old

    def test_no_postponed_annotations_in_routes_module(self):
        tree = ast.parse((ROOT / "ads" / "routes.py").read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and node.module == "__future__":
                self.fail("ads/routes.py must not use `from __future__ import annotations` (breaks FastAPI's Request injection)")

    def test_smart_routes_registered(self):
        for p in ("/api/smart/start", "/api/smart/report", "/api/smart/done"):
            self.assertIn(("POST", p), self.app.routes)

    def test_every_handler_request_param_is_a_real_class_not_a_string(self):
        import fastapi
        checked = 0
        for (m, path), fn in self.app.routes.items():
            params = inspect.signature(fn).parameters
            if "request" in params:
                ann = params["request"].annotation
                self.assertIs(ann, fastapi.Request, f"{m} {path}: `request` annotation is {ann!r}; FastAPI would treat it as a query param (422)")
                checked += 1
        self.assertGreaterEqual(checked, 6)

    def test_validation_handler_registered(self):
        import fastapi.exceptions as fe
        self.assertIn(fe.RequestValidationError, self.app.handlers)

    @unittest.skipUnless(REAL_FASTAPI, "needs real fastapi + httpx (pip install -r requirements.txt httpx)")
    def test_real_fastapi_post_smart_start_is_not_422(self):
        from fastapi import FastAPI
        from fastapi.testclient import TestClient
        from ads import routes
        app = FastAPI()
        async def auth(body): return None, None, "Session expired. Please open the link again."
        routes.register(app, auth=auth, identify=lambda d: (None, None), send_file=None, db=object(), token_re=None,
                        telegram_errors=(), forbidden=(), policy=self.policy, manager=None, ads_required=1)
        c = TestClient(app)
        r = c.post("/api/smart/start", json={"sid": "abc", "init_data": "x"})
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(r.json()["code"], "session")
        r = c.post("/api/smart/start", content="not json", headers={"content-type": "application/json"})
        self.assertEqual((r.status_code, r.json()["code"]), (400, "invalid_json"))
        r = c.post("/api/smart/start", content="sid=1", headers={"content-type": "application/x-www-form-urlencoded"})
        self.assertEqual((r.status_code, r.json()["code"]), (400, "bad_content_type"))


if __name__ == "__main__":
    unittest.main()
