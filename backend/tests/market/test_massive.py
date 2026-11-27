"""Tests for MassiveDataSource (mocked)."""

from unittest.mock import MagicMock, patch

import pytest

from app.market.cache import PriceCache
from app.market.massive_client import MassiveDataSource


def _make_snapshot(ticker: str, price: float, timestamp_ms: int) -> MagicMock:
    """Create a mock Massive snapshot object."""
    snap = MagicMock()
    snap.ticker = ticker
    snap.last_trade = MagicMock()
    snap.last_trade.price = price
    snap.last_trade.timestamp = timestamp_ms
    return snap


@pytest.mark.asyncio
class TestMassiveDataSource:
    """Unit tests for MassiveDataSource with mocked API."""

    async def test_poll_updates_cache(self):
        """Test that polling updates the cache."""
        cache = PriceCache()
        source = MassiveDataSource(
            api_key="test-key",
            price_cache=cache,
            poll_interval=60.0,  # Long interval so the loop doesn't auto-poll
        )
        source._tickers = ["AAPL", "GOOGL"]
        source._client = MagicMock()  # Satisfy the _poll_once guard

        mock_snapshots = [
            _make_snapshot("AAPL", 190.50, 1707580800000),
            _make_snapshot("GOOGL", 175.25, 1707580800000),
        ]

        with patch.object(source, "_fetch_snapshots", return_value=mock_snapshots):
            await source._poll_once()

        assert cache.get_price("AAPL") == 190.50
        assert cache.get_price("GOOGL") == 175.25

    async def test_malformed_snapshot_skipped(self):
        """Test that malformed snapshots are skipped gracefully."""
        cache = PriceCache()
        source = MassiveDataSource(
            api_key="test-key",
            price_cache=cache,
            poll_interval=60.0,
        )
        source._tickers = ["AAPL", "BAD"]
        source._client = MagicMock()  # Satisfy the _poll_once guard

        good_snap = _make_snapshot("AAPL", 190.50, 1707580800000)
        bad_snap = MagicMock()
        bad_snap.ticker = "BAD"
        bad_snap.last_trade = None  # Will cause AttributeError

        with patch.object(source, "_fetch_snapshots", return_value=[good_snap, bad_snap]):
            await source._poll_once()

        # Good ticker processed, bad one skipped
        assert cache.get_price("AAPL") == 190.50
        assert cache.get_price("BAD") is None

    async def test_api_error_does_not_crash(self):
        """Test that API errors don't crash the poller."""
        cache = PriceCache()
        source = MassiveDataSource(
            api_key="test-key",
            price_cache=cache,
            poll_interval=60.0,
        )
        source._tickers = ["AAPL"]
        source._client = MagicMock()  # Satisfy the _poll_once guard

        with patch.object(source, "_fetch_snapshots", side_effect=Exception("network error")):
            await source._poll_once()  # Should not raise

        assert cache.get_price("AAPL") is None  # No update happened

    async def test_timestamp_conversion(self):
        """Test that timestamps are converted from milliseconds to seconds."""
        cache = PriceCache()
        source = MassiveDataSource(
            api_key="test-key",
            price_cache=cache,
            poll_interval=60.0,
        )
        source._tickers = ["AAPL"]
        source._client = MagicMock()  # Satisfy the _poll_once guard

        mock_snapshots = [_make_snapshot("AAPL", 190.50, 1707580800000)]

        with patch.object(source, "_fetch_snapshots", return_value=mock_snapshots):
            await source._poll_once()

        update = cache.get("AAPL")
        assert update is not None
        assert update.timestamp == 1707580800.0  # Converted to seconds

    async def test_add_ticker(self):
        """Test adding a ticker."""
        cache = PriceCache()
        source = MassiveDataSource(api_key="test-key", price_cache=cache)

        await source.add_ticker("AAPL")
        assert "AAPL" in source.get_tickers()

    async def test_add_ticker_uppercase_normalization(self):
        """Test that tickers are normalized to uppercase."""
        cache = PriceCache()
        source = MassiveDataSource(api_key="test-key", price_cache=cache)

        await source.add_ticker("aapl")
        assert "AAPL" in source.get_tickers()

    async def test_add_ticker_strips_whitespace(self):
        """Test that ticker whitespace is stripped."""
        cache = PriceCache()
        source = MassiveDataSource(api_key="test-key", price_cache=cache)

        await source.add_ticker("  AAPL  ")
        assert "AAPL" in source.get_tickers()

    async def test_remove_ticker(self):
        """Test removing a ticker."""
        cache = PriceCache()
        source = MassiveDataSource(api_key="test-key", price_cache=cache)
        source._tickers = ["AAPL", "GOOGL"]
        cache.update("AAPL", 190.00)

        await source.remove_ticker("AAPL")
        assert "AAPL" not in source.get_tickers()
        assert cache.get("AAPL") is None

    async def test_get_tickers(self):
        """Test getting the list of active tickers."""
        cache = PriceCache()
        source = MassiveDataSource(api_key="test-key", price_cache=cache)
        source._tickers = ["AAPL", "GOOGL"]

        tickers = source.get_tickers()
        assert tickers == ["AAPL", "GOOGL"]

    async def test_empty_tickers_skips_poll(self):
        """Test that polling is skipped when there are no tickers."""
        cache = PriceCache()
        source = MassiveDataSource(api_key="test-key", price_cache=cache)
        source._tickers = []

        # Should not call _fetch_snapshots
        with patch.object(source, "_fetch_snapshots") as mock_fetch:
            await source._poll_once()
            mock_fetch.assert_not_called()

    async def test_stop_is_idempotent(self):
        """Test that stop() can be called multiple times."""
        cache = PriceCache()
        source = MassiveDataSource(api_key="test-key", price_cache=cache)

        await source.stop()
        await source.stop()  # Should not raise

    async def test_stop_cancels_task(self):
        """Test that stop() cancels the polling task."""
        cache = PriceCache()
        source = MassiveDataSource(api_key="test-key", price_cache=cache, poll_interval=10.0)

        # Mock the client and start
        with patch("app.market.massive_client.RESTClient"):
            with patch.object(source, "_fetch_snapshots", return_value=[]):
                await source.start(["AAPL"])

        # Verify task is running
        assert source._task is not None
        assert not source._task.done()

        # Stop and verify task is cancelled
        await source.stop()
        assert source._task is None

    async def test_start_immediate_poll(self):
        """Test that start() does an immediate poll before starting the loop."""
        cache = PriceCache()
        source = MassiveDataSource(api_key="test-key", price_cache=cache, poll_interval=60.0)

        mock_snapshots = [_make_snapshot("AAPL", 190.50, 1707580800000)]

        with patch("app.market.massive_client.RESTClient"):
            with patch.object(source, "_fetch_snapshots", return_value=mock_snapshots):
                await source.start(["AAPL"])

        # Cache should have data immediately from the first poll
        assert cache.get_price("AAPL") == 190.50

        await source.stop()


@pytest.mark.asyncio
class TestMassiveLifecycle:
    """Lifecycle, loop and fetch tests with mocked REST client."""

    async def test_start_polls_immediately_and_stop_cleans_up(self):
        cache = PriceCache()
        source = MassiveDataSource("k", cache, poll_interval=60.0)
        snaps = [_make_snapshot("AAPL", 190.0, 1707580800000)]
        with patch("app.market.massive_client.RESTClient"), patch.object(
            source, "_fetch_snapshots", return_value=snaps
        ):
            await source.start(["AAPL"])
            assert cache.get_price("AAPL") == 190.0
            assert source._task is not None
            await source.stop()
        assert source._task is None
        assert source._client is None

    async def test_poll_loop_polls_repeatedly(self):
        import asyncio

        source = MassiveDataSource("k", PriceCache(), poll_interval=0.01)
        calls = 0

        async def fake_poll():
            nonlocal calls
            calls += 1

        source._poll_once = fake_poll
        task = asyncio.create_task(source._poll_loop())
        await asyncio.sleep(0.1)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert calls >= 2

    async def test_poll_failure_does_not_raise(self):
        cache = PriceCache()
        source = MassiveDataSource("k", cache)
        source._tickers = ["AAPL"]
        source._client = MagicMock()
        with patch.object(source, "_fetch_snapshots", side_effect=RuntimeError("429")):
            await source._poll_once()
        assert cache.get_price("AAPL") is None

    async def test_poll_skipped_without_tickers_or_client(self):
        source = MassiveDataSource("k", PriceCache())
        with patch.object(source, "_fetch_snapshots") as fetch:
            await source._poll_once()
            source._client = MagicMock()
            await source._poll_once()
        fetch.assert_not_called()

    async def test_timestamp_converted_from_ms(self):
        cache = PriceCache()
        source = MassiveDataSource("k", cache)
        source._tickers = ["AAPL"]
        source._client = MagicMock()
        with patch.object(
            source, "_fetch_snapshots",
            return_value=[_make_snapshot("AAPL", 190.0, 1707580800000)],
        ):
            await source._poll_once()
        assert cache.get("AAPL").timestamp == 1707580800.0

    async def test_add_remove_ticker(self):
        cache = PriceCache()
        source = MassiveDataSource("k", cache)
        await source.add_ticker(" tsla ")
        await source.add_ticker("TSLA")
        assert source.get_tickers() == ["TSLA"]
        cache.update("TSLA", 200.0)
        await source.remove_ticker("tsla")
        assert source.get_tickers() == []
        assert cache.get("TSLA") is None

    def test_fetch_snapshots_calls_client(self):
        source = MassiveDataSource("k", PriceCache())
        source._tickers = ["AAPL", "MSFT"]
        source._client = MagicMock()
        source._client.get_snapshot_all.return_value = ["x"]
        assert source._fetch_snapshots() == ["x"]
        kwargs = source._client.get_snapshot_all.call_args.kwargs
        assert kwargs["tickers"] == ["AAPL", "MSFT"]
