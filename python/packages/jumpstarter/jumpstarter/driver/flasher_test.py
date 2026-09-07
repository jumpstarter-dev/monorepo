import pytest

from jumpstarter.driver.flasher import FlasherInterface, StreamingFlasherInterface


class TestFlasherInterfaceABC:
    def test_cannot_instantiate_directly(self):
        with pytest.raises(TypeError, match="abstract method"):
            FlasherInterface()

    def test_subclass_without_flash_raises(self):
        class Incomplete(FlasherInterface):
            def dump(self, target, partition=None):
                pass

        with pytest.raises(TypeError, match="abstract method"):
            Incomplete()

    def test_subclass_without_dump_raises(self):
        class Incomplete(FlasherInterface):
            def flash(self, source, target=None):
                pass

        with pytest.raises(TypeError, match="abstract method"):
            Incomplete()

    def test_complete_subclass_can_be_instantiated(self):
        class Complete(FlasherInterface):
            def flash(self, source, target=None):
                pass

            def dump(self, target, partition=None):
                pass

        instance = Complete()
        assert instance is not None

    def test_client_returns_expected_path(self):
        assert FlasherInterface.client() == "jumpstarter.client.flasher.FlasherClient"


class TestStreamingFlasherInterfaceABC:
    def test_cannot_instantiate_directly(self):
        with pytest.raises(TypeError, match="abstract method"):
            StreamingFlasherInterface()

    def test_subclass_without_flash_raises(self):
        class Incomplete(StreamingFlasherInterface):
            pass

        with pytest.raises(TypeError, match="abstract method"):
            Incomplete()

    def test_complete_subclass_can_be_instantiated(self):
        class Complete(StreamingFlasherInterface):
            async def flash(self, source, manifest=None):
                yield  # pragma: no cover

        instance = Complete()
        assert instance is not None

    def test_client_returns_expected_path(self):
        assert StreamingFlasherInterface.client() == "jumpstarter.client.flasher.StreamingFlasherClient"

    def test_driver_type_is_storage(self):
        assert StreamingFlasherInterface.driver_type == "storage"
