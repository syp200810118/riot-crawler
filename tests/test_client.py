"""RiotClient against a local aiohttp server standing in for the Riot API."""

import asyncio

import aiohttp
import pytest
from aiohttp import web

import riot_crawler.client as client_mod
from riot_crawler.client import AuthError, RiotClient


def run_with_server(handler, body):
    async def go():
        app = web.Application()
        app.router.add_get("/{tail:.*}", handler)
        runner = web.AppRunner(app)
        await runner.setup()
        site = web.TCPSite(runner, "127.0.0.1", 0)
        await site.start()
        port = site._server.sockets[0].getsockname()[1]
        orig = client_mod.host
        client_mod.host = lambda routing: f"http://127.0.0.1:{port}"
        try:
            async with aiohttp.ClientSession() as session:
                return await body(RiotClient("test-key", session, max_retries=2))
        finally:
            client_mod.host = orig
            await runner.cleanup()

    return asyncio.run(go())


def test_429_retry_after_then_success_and_header_limits():
    calls = []

    async def handler(request):
        calls.append(request.headers["X-Riot-Token"])
        hdrs = {"X-App-Rate-Limit": "500:10,30000:600", "X-Method-Rate-Limit": "2000:10"}
        if len(calls) == 1:
            return web.Response(status=429, headers={**hdrs, "Retry-After": "1", "X-Rate-Limit-Type": "application"})
        return web.json_response(["NA1_1"], headers=hdrs)

    async def body(c):
        ids = await c.match_ids("americas", "puuid", 0, 1)
        return ids, c._app_limiters["americas"].limits

    ids, limits = run_with_server(handler, body)
    assert ids == ["NA1_1"]
    assert calls == ["test-key", "test-key"]
    assert limits == [(500, 10.0), (30000, 600.0)]


def test_404_returns_none_and_401_raises():
    async def handler(request):
        return web.Response(status=404 if "matches/NA1_404" in request.path else 401)

    async def body(c):
        assert await c.match("americas", "NA1_404") is None
        with pytest.raises(AuthError):
            await c.match("americas", "NA1_1")

    run_with_server(handler, body)
