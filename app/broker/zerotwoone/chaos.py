"""A test tool: makes ONE chosen order call misbehave the way a bad network or a busy broker would.

It wraps the HTTP transport under the adapter. Everything passes straight through, except that after
`arm(mode)` the next `POST /orders` is sabotaged:

  lost-request   the request never reaches 021 (connection times out)          -> the order does NOT exist
  lost-reply     021 gets and processes the order, but the reply is lost       -> the order DOES exist
  http-500       021 processes the order, then we are told 500 'Internal Server Error'
  http-503       021 processes the order, then we are told 503

These are exactly the "did my order go through?" situations the problem statement warns about. Used by
`scripts/chaos_live.py` against the real sandbox and by the tests against the fake one. It is never switched on
by itself, and nothing in the running app imports it.
"""

import httpx

MODES = ("lost-request", "lost-reply", "http-500", "http-503")


class ChaosTransport(httpx.AsyncBaseTransport):
    def __init__(self, inner: httpx.AsyncBaseTransport | None = None):
        self._inner = inner or httpx.AsyncHTTPTransport()
        self._mode: str | None = None
        self.sabotaged = 0

    def arm(self, mode: str) -> None:
        if mode not in MODES:
            raise ValueError(f"unknown mode {mode!r}; use one of {MODES}")
        self._mode = mode

    @property
    def armed(self) -> bool:
        return self._mode is not None

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        if self._mode is None or request.method != "POST" or not request.url.path.endswith("/orders"):
            return await self._inner.handle_async_request(request)
        mode, self._mode = self._mode, None  # one shot
        self.sabotaged += 1
        if mode == "lost-request":
            raise httpx.ConnectTimeout("chaos: the request never left", request=request)
        response = await self._inner.handle_async_request(request)  # 021 really receives and processes it
        await response.aread()
        if mode == "lost-reply":
            raise httpx.ReadTimeout("chaos: the reply was lost", request=request)
        status, text = (500, "Internal Server Error") if mode == "http-500" else (503, "Service temporarily unavailable")
        return httpx.Response(status, json={"data": None, "success": False, "error": text}, request=request)

    async def aclose(self) -> None:
        await self._inner.aclose()
