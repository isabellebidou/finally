"""Tests for the SSE streaming module."""

import json

from app.market.cache import PriceCache
from app.market.stream import _generate_events, create_stream_router


class FakeRequest:
    """Minimal Request stand-in: disconnects after `connected_checks` checks."""

    def __init__(self, connected_checks: int = 1, host: str | None = "127.0.0.1"):
        self._remaining = connected_checks
        self.client = type("C", (), {"host": host})() if host else None

    async def is_disconnected(self) -> bool:
        if self._remaining <= 0:
            return True
        self._remaining -= 1
        return False


async def _collect(cache, request, interval=0.0):
    return [chunk async for chunk in _generate_events(cache, request, interval=interval)]


class TestGenerateEvents:
    async def test_first_chunk_is_retry_directive(self):
        chunks = await _collect(PriceCache(), FakeRequest(0))
        assert chunks == ["retry: 1000\n\n"]

    async def test_empty_cache_sends_no_data(self):
        chunks = await _collect(PriceCache(), FakeRequest(2))
        assert chunks == ["retry: 1000\n\n"]

    async def test_emits_all_prices_as_json(self):
        cache = PriceCache()
        cache.update("AAPL", 190.0)
        cache.update("GOOGL", 175.0)
        chunks = await _collect(cache, FakeRequest(1))
        assert len(chunks) == 2
        assert chunks[1].startswith("data: ") and chunks[1].endswith("\n\n")
        payload = json.loads(chunks[1][len("data: "):])
        assert set(payload) == {"AAPL", "GOOGL"}
        for key in ("ticker", "price", "previous_price", "timestamp",
                    "change", "change_percent", "direction"):
            assert key in payload["AAPL"]

    async def test_only_sends_when_cache_changed(self):
        cache = PriceCache()
        cache.update("AAPL", 190.0)
        chunks = await _collect(cache, FakeRequest(3))
        assert len([c for c in chunks if c.startswith("data:")]) == 1

    async def test_sends_again_after_update(self):
        cache = PriceCache()
        cache.update("AAPL", 190.0)

        class UpdatingRequest(FakeRequest):
            async def is_disconnected(self):
                cache.update("AAPL", cache.get_price("AAPL") + 1)
                return await super().is_disconnected()

        chunks = await _collect(cache, UpdatingRequest(3))
        assert len([c for c in chunks if c.startswith("data:")]) == 3

    async def test_unknown_client_host(self):
        chunks = await _collect(PriceCache(), FakeRequest(0, host=None))
        assert chunks == ["retry: 1000\n\n"]


class TestStreamRouter:
    def test_router_registers_prices_route(self):
        router = create_stream_router(PriceCache())
        paths = [r.path for r in router.routes]
        assert "/api/stream/prices" in paths
