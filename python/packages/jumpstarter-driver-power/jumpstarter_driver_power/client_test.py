import logging
import time

from .driver import MockPower
from jumpstarter.common.utils import serve


def test_log_stream(caplog):
    """Test that driver logs are properly streamed to the client."""
    with serve(MockPower()) as client:
        # Set log level to capture INFO messages from exporter:driver logger
        with caplog.at_level(logging.INFO, logger="exporter:driver"):
            with client.log_stream():
                client.on()
                time.sleep(1)  # to ensure log is flushed
                assert "power on" in caplog.text

                client.off()
                time.sleep(1)
                assert "power off" in caplog.text
