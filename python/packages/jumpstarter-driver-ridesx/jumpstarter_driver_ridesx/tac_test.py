from unittest.mock import AsyncMock, MagicMock

import pytest

from jumpstarter_driver_ridesx.tac import send_tac_command


@pytest.mark.asyncio
async def test_send_tac_command_times_out():
    tac = MagicMock()
    stream = AsyncMock()
    stream.__aenter__ = AsyncMock(return_value=stream)
    stream.__aexit__ = AsyncMock(return_value=None)
    stream.receive = AsyncMock(return_value=b"waiting")
    tac.connect.return_value = stream

    with pytest.raises(TimeoutError, match="devicePower"):
        await send_tac_command(tac, "devicePower 0", timeout=0.01)
