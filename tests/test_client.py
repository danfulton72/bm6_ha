"""BLE client regression tests for history completion and idle connection cleanup."""

from __future__ import annotations

from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock, patch

from bleak.backends.device import BLEDevice
import pytest

from custom_components.bm6_ha.client import BM6Client, BM6Error


@pytest.fixture
def client() -> BM6Client:
    return BM6Client(BLEDevice('A4:C1:38:01:02:03', 'BM6', {}), 'BM6')


async def test_history_completion_marker_spanning_notifications(client: BM6Client) -> None:
    # Trailing FF of a valid history record's payload is followed by FEFE
    # in another GATT notification. Split marker detection must not time out.
    chunks = iter(('4E250140FF', 'FEFE'))
    client._request = AsyncMock(return_value='D155050001')
    client._next_frame = AsyncMock(side_effect=lambda timeout: next(chunks))
    client._write = AsyncMock()
    with patch('custom_components.bm6_ha.client.p.utcnow', return_value=datetime(2026, 1, 1, tzinfo=UTC)):
        records, reference = await client._read_history(MagicMock(), 1)
    assert len(records) == 1
    assert records[0].voltage == 12.5
    assert records[0].soc == 80
    assert records[0].temperature == 20


async def test_incomplete_history_raises_instead_of_advancing_cursor(client: BM6Client) -> None:
    client._request = AsyncMock(return_value='D155050001')
    client._next_frame = AsyncMock(side_effect=['4E250140', BM6Error('timeout')])
    client._write = AsyncMock()
    with pytest.raises(BM6Error, match='interrupted'):
        await client._read_history(MagicMock(), 1)
    client._write.assert_awaited_once()


async def test_disconnect_timer_rearmed_when_poll_busy(client: BM6Client) -> None:
    with patch.object(client, '_schedule_disconnect') as schedule:
        async with client._lock:
            await client._timed_disconnect()
    schedule.assert_called_once_with(10.0)
