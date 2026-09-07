import warnings
from unittest.mock import MagicMock, patch

import click.testing
import pytest

from jumpstarter.client.flasher import FlashPhase, FlashStatus, StreamingFlasherClient, _parse_path


class TestParsePath:
    """Tests for _parse_path which routes local files vs HTTP URLs."""

    def test_http_url(self):
        local, url = _parse_path("http://example.com/image.qcow2")
        assert local is None
        assert url == "http://example.com/image.qcow2"

    def test_https_url(self):
        local, url = _parse_path("https://download.fedoraproject.org/pub/fedora/image.qcow2")
        assert local is None
        assert url == "https://download.fedoraproject.org/pub/fedora/image.qcow2"

    def test_local_path_string(self, tmp_path):
        test_file = tmp_path / "image.qcow2"
        test_file.touch()
        local, url = _parse_path(str(test_file))
        assert url is None
        assert local == test_file.resolve()

    def test_local_path_object(self, tmp_path):
        test_file = tmp_path / "image.qcow2"
        test_file.touch()
        local, url = _parse_path(test_file)
        assert url is None
        assert local == test_file.resolve()

    def test_relative_path(self):
        local, url = _parse_path("relative/path/image.qcow2")
        assert url is None
        assert local is not None
        assert local.is_absolute()

    def test_url_with_query_params(self):
        test_url = "https://example.com/image.qcow2?token=abc&expires=123"
        local, url = _parse_path(test_url)
        assert local is None
        assert url == test_url


class TestHttpUrlAdapter:
    """Tests for _http_url_adapter which creates PresignedRequestResource for HTTP URLs."""

    @pytest.mark.anyio
    async def test_read_mode_produces_get_request(self):
        from jumpstarter.client.flasher import _http_url_adapter

        # _http_url_adapter is decorated with @blocking, but the underlying
        # async generator can be tested directly via its __wrapped__ attribute
        gen = _http_url_adapter.__wrapped__(
            client=None,
            url="https://example.com/firmware.bin",
            mode="rb",
        )
        result = await gen.__aenter__()

        # Should produce a serialized PresignedRequestResource with GET method
        assert result["url"] == "https://example.com/firmware.bin"
        assert result["method"] == "GET"
        assert result["headers"] == {}

        await gen.__aexit__(None, None, None)

    @pytest.mark.anyio
    async def test_write_mode_produces_put_request(self):
        from jumpstarter.client.flasher import _http_url_adapter

        gen = _http_url_adapter.__wrapped__(
            client=None,
            url="https://example.com/dump.bin",
            mode="wb",
        )
        result = await gen.__aenter__()

        assert result["url"] == "https://example.com/dump.bin"
        assert result["method"] == "PUT"
        assert result["headers"] == {}

        await gen.__aexit__(None, None, None)


class TestAsyncIteratorStream:
    """Tests for _AsyncIteratorStream receive/send/aclose lifecycle."""

    @pytest.mark.anyio
    async def test_receive_yields_chunks(self):
        from anyio import EndOfStream

        from jumpstarter.client.flasher import _AsyncIteratorStream

        async def gen():
            yield b"chunk1"
            yield b"chunk2"

        stream = _AsyncIteratorStream(iterator=gen(), total=12)
        assert await stream.receive() == b"chunk1"
        assert await stream.receive() == b"chunk2"
        with pytest.raises(EndOfStream):
            await stream.receive()

    @pytest.mark.anyio
    async def test_send_raises_broken_resource(self):
        from anyio import BrokenResourceError

        from jumpstarter.client.flasher import _AsyncIteratorStream

        async def gen():
            yield b"data"

        stream = _AsyncIteratorStream(iterator=gen())
        with pytest.raises(BrokenResourceError):
            await stream.send(b"data")

    @pytest.mark.anyio
    async def test_aclose_propagates_to_generator(self):
        from jumpstarter.client.flasher import _AsyncIteratorStream

        closed = False

        async def gen():
            nonlocal closed
            try:
                yield b"data"
                yield b"more"
            finally:
                closed = True

        stream = _AsyncIteratorStream(iterator=gen())
        await stream.receive()
        await stream.aclose()
        assert closed

    @pytest.mark.anyio
    async def test_extra_attributes_with_total(self):
        from jumpstarter.client.flasher import _AsyncIteratorStream
        from jumpstarter.streams.progress import ProgressAttribute

        async def gen():
            yield b"data"

        stream = _AsyncIteratorStream(iterator=gen(), total=100)
        attrs = stream.extra_attributes
        assert ProgressAttribute.total in attrs
        assert attrs[ProgressAttribute.total]() == 100.0

    @pytest.mark.anyio
    async def test_extra_attributes_without_total(self):
        from jumpstarter.client.flasher import _AsyncIteratorStream

        async def gen():
            yield b"data"

        stream = _AsyncIteratorStream(iterator=gen(), total=None)
        assert stream.extra_attributes == {}

    @pytest.mark.anyio
    async def test_receive_on_empty_iterator(self):
        from anyio import EndOfStream

        from jumpstarter.client.flasher import _AsyncIteratorStream

        async def gen():
            return
            yield  # noqa: RET504

        stream = _AsyncIteratorStream(iterator=gen())
        with pytest.raises(EndOfStream):
            await stream.receive()


class TestFileWriteObjectStream:
    """Tests for _FileWriteObjectStream send/aclose lifecycle."""

    @pytest.mark.anyio
    async def test_write_and_read_back(self, tmp_path):
        from jumpstarter.client.flasher import _FileWriteObjectStream

        out = tmp_path / "output.bin"
        stream = _FileWriteObjectStream(path=out)
        await stream.send(b"hello ")
        await stream.send(b"world")
        await stream.send_eof()
        assert out.read_bytes() == b"hello world"

    @pytest.mark.anyio
    async def test_receive_raises_end_of_stream(self, tmp_path):
        from anyio import EndOfStream

        from jumpstarter.client.flasher import _FileWriteObjectStream

        stream = _FileWriteObjectStream(path=tmp_path / "out.bin")
        with pytest.raises(EndOfStream):
            await stream.receive()

    @pytest.mark.anyio
    async def test_aclose_without_open(self, tmp_path):
        from jumpstarter.client.flasher import _FileWriteObjectStream

        stream = _FileWriteObjectStream(path=tmp_path / "out.bin")
        await stream.aclose()

    @pytest.mark.anyio
    async def test_aclose_closes_file(self, tmp_path):
        from jumpstarter.client.flasher import _FileWriteObjectStream

        out = tmp_path / "output.bin"
        stream = _FileWriteObjectStream(path=out)
        await stream.send(b"data")
        await stream.aclose()
        assert out.read_bytes() == b"data"
        assert stream._file is None


class TestFlasherClientRouting:
    """Tests that FlasherClient routes HTTP URLs vs local paths correctly."""

    def test_flash_single_routes_http_url(self):
        """Verify that an HTTP URL goes through _http_url_adapter, not _local_file_adapter."""
        from unittest.mock import MagicMock

        from jumpstarter.client.flasher import FlasherClient

        client = object.__new__(FlasherClient)

        mock_http = MagicMock()
        mock_http.__enter__ = MagicMock(return_value="http_handle")
        mock_http.__exit__ = MagicMock(return_value=False)

        mock_local = MagicMock()

        with (
            patch("jumpstarter.client.flasher._http_url_adapter", return_value=mock_http) as http_patch,
            patch("jumpstarter.client.flasher._local_file_adapter", return_value=mock_local) as local_patch,
            patch.object(client, "call", return_value=None) as call_mock,
        ):
            client._flash_single("https://example.com/image.bin", target=None, compression=None)

            http_patch.assert_called_once_with(client=client, url="https://example.com/image.bin", mode="rb")
            local_patch.assert_not_called()
            call_mock.assert_called_once_with("flash", "http_handle", None)

    def test_flash_single_routes_local_path(self, tmp_path):
        """Verify that a local path goes through _local_file_adapter, not _http_url_adapter."""
        from unittest.mock import MagicMock

        from jumpstarter.client.flasher import FlasherClient

        client = object.__new__(FlasherClient)
        test_file = tmp_path / "image.bin"
        test_file.touch()

        mock_local = MagicMock()
        mock_local.__enter__ = MagicMock(return_value="local_handle")
        mock_local.__exit__ = MagicMock(return_value=False)

        mock_http = MagicMock()

        with (
            patch("jumpstarter.client.flasher._http_url_adapter", return_value=mock_http) as http_patch,
            patch("jumpstarter.client.flasher._local_file_adapter", return_value=mock_local) as local_patch,
            patch.object(client, "call", return_value=None) as call_mock,
        ):
            client._flash_single(str(test_file), target=None, compression=None)

            local_patch.assert_called_once()
            http_patch.assert_not_called()
            call_mock.assert_called_once_with("flash", "local_handle", None)

    def test_dump_routes_http_url(self):
        """Verify that dump with an HTTP URL goes through _http_url_adapter."""
        from unittest.mock import MagicMock

        from jumpstarter.client.flasher import FlasherClient

        client = object.__new__(FlasherClient)

        mock_http = MagicMock()
        mock_http.__enter__ = MagicMock(return_value="http_handle")
        mock_http.__exit__ = MagicMock(return_value=False)

        with (
            patch("jumpstarter.client.flasher._http_url_adapter", return_value=mock_http) as http_patch,
            patch("jumpstarter.client.flasher._local_file_adapter") as local_patch,
            patch.object(client, "call", return_value=None) as call_mock,
        ):
            client.dump("https://example.com/dump.bin", target=None)

            http_patch.assert_called_once_with(client=client, url="https://example.com/dump.bin", mode="wb")
            local_patch.assert_not_called()
            call_mock.assert_called_once_with("dump", "http_handle", None)

    def test_dump_routes_local_path(self, tmp_path):
        """Verify that dump with a local path goes through _local_file_adapter."""
        from unittest.mock import MagicMock

        from jumpstarter.client.flasher import FlasherClient

        client = object.__new__(FlasherClient)
        test_file = tmp_path / "dump.bin"

        mock_local = MagicMock()
        mock_local.__enter__ = MagicMock(return_value="local_handle")
        mock_local.__exit__ = MagicMock(return_value=False)

        with (
            patch("jumpstarter.client.flasher._http_url_adapter") as http_patch,
            patch("jumpstarter.client.flasher._local_file_adapter", return_value=mock_local) as local_patch,
            patch.object(client, "call", return_value=None) as call_mock,
        ):
            client.dump(str(test_file), target=None)

            local_patch.assert_called_once()
            http_patch.assert_not_called()
            call_mock.assert_called_once_with("dump", "local_handle", None)


class TestFlasherClientMultiTarget:
    """Tests for dict-based multi-target flash."""

    def test_flash_dict_calls_flash_single_per_entry(self):
        from unittest.mock import MagicMock

        from jumpstarter.client.flasher import FlasherClient

        client = object.__new__(FlasherClient)

        mock_http = MagicMock()
        mock_http.__enter__ = MagicMock(return_value="handle")
        mock_http.__exit__ = MagicMock(return_value=False)

        with (
            patch("jumpstarter.client.flasher._http_url_adapter", return_value=mock_http),
            patch.object(client, "call", return_value="ok") as call_mock,
        ):
            results = client.flash(
                {"boot": "https://example.com/boot.bin", "root": "https://example.com/root.bin"},
                compression=None,
            )
            assert results == {"boot": "ok", "root": "ok"}
            assert call_mock.call_count == 2

    def test_flash_dict_with_target_raises_argument_error(self):
        from jumpstarter.client.flasher import FlasherClient
        from jumpstarter.common.exceptions import ArgumentError

        client = object.__new__(FlasherClient)

        with pytest.raises(ArgumentError, match="'target' parameter is not valid"):
            client.flash({"boot": "/tmp/boot.bin"}, target="some_target")


class TestCompressionWarning:
    """Tests that compression parameter warns when used with HTTP URLs."""

    def test_flash_http_with_compression_warns(self):
        from unittest.mock import MagicMock

        from jumpstarter.client.flasher import FlasherClient

        client = object.__new__(FlasherClient)

        mock_http = MagicMock()
        mock_http.__enter__ = MagicMock(return_value="handle")
        mock_http.__exit__ = MagicMock(return_value=False)

        with (
            patch("jumpstarter.client.flasher._http_url_adapter", return_value=mock_http),
            patch.object(client, "call", return_value=None),
        ):
            with warnings.catch_warnings(record=True) as w:
                warnings.simplefilter("always")
                client._flash_single("https://example.com/image.bin", target=None, compression="zstd")
                assert len(w) == 1
                assert "compression parameter is ignored" in str(w[0].message)

    def test_flash_local_with_compression_no_warning(self, tmp_path):
        from unittest.mock import MagicMock

        from jumpstarter.client.flasher import FlasherClient

        client = object.__new__(FlasherClient)
        test_file = tmp_path / "image.bin"
        test_file.touch()

        mock_local = MagicMock()
        mock_local.__enter__ = MagicMock(return_value="handle")
        mock_local.__exit__ = MagicMock(return_value=False)

        with (
            patch("jumpstarter.client.flasher._local_file_adapter", return_value=mock_local),
            patch.object(client, "call", return_value=None),
        ):
            with warnings.catch_warnings(record=True) as w:
                warnings.simplefilter("always")
                client._flash_single(str(test_file), target=None, compression="zstd")
                assert len(w) == 0

    def test_dump_http_with_compression_warns(self):
        from unittest.mock import MagicMock

        from jumpstarter.client.flasher import FlasherClient

        client = object.__new__(FlasherClient)

        mock_http = MagicMock()
        mock_http.__enter__ = MagicMock(return_value="handle")
        mock_http.__exit__ = MagicMock(return_value=False)

        with (
            patch("jumpstarter.client.flasher._http_url_adapter", return_value=mock_http),
            patch.object(client, "call", return_value=None),
        ):
            with warnings.catch_warnings(record=True) as w:
                warnings.simplefilter("always")
                client.dump("https://example.com/dump.bin", target=None, compression="zstd")
                assert len(w) == 1
                assert "compression parameter is ignored" in str(w[0].message)


class TestFlasherClientCli:
    """Tests for FlasherClientInterface.cli() click command group."""

    def _make_client(self):
        from jumpstarter.client.flasher import FlasherClient

        client = object.__new__(FlasherClient)
        client.flash = MagicMock()
        client.dump = MagicMock()
        client.description = None
        client.methods_description = {}
        return client

    def test_flash_with_file_argument(self):
        client = self._make_client()
        group = client.cli()
        runner = click.testing.CliRunner()
        result = runner.invoke(group, ["flash", "/tmp/image.bin"])
        assert result.exit_code == 0
        client.flash.assert_called_once_with("/tmp/image.bin", target=None, compression=None)

    def test_flash_with_target_spec(self):
        client = self._make_client()
        group = client.cli()
        runner = click.testing.CliRunner()
        result = runner.invoke(group, ["flash", "-t", "boot:/tmp/boot.bin", "-t", "root:/tmp/root.bin"])
        assert result.exit_code == 0
        client.flash.assert_called_once()
        call_args = client.flash.call_args
        mapping = call_args[0][0]
        assert mapping == {"boot": "/tmp/boot.bin", "root": "/tmp/root.bin"}

    def test_flash_with_invalid_target_spec_format(self):
        client = self._make_client()
        group = client.cli()
        runner = click.testing.CliRunner()
        result = runner.invoke(group, ["flash", "-t", "no_colon_here"])
        assert result.exit_code != 0
        assert "Invalid target spec" in result.output

    def test_flash_with_no_file_and_no_target(self):
        client = self._make_client()
        group = client.cli()
        runner = click.testing.CliRunner()
        result = runner.invoke(group, ["flash"])
        assert result.exit_code != 0
        assert "FILE argument is required" in result.output

    def test_dump_with_file_and_target(self):
        client = self._make_client()
        group = client.cli()
        runner = click.testing.CliRunner()
        result = runner.invoke(group, ["dump", "/tmp/output.bin", "--target", "rootfs"])
        assert result.exit_code == 0
        client.dump.assert_called_once_with("/tmp/output.bin", target="rootfs", compression=None)

    def test_dump_with_file_only(self):
        client = self._make_client()
        group = client.cli()
        runner = click.testing.CliRunner()
        result = runner.invoke(group, ["dump", "/tmp/output.bin"])
        assert result.exit_code == 0
        client.dump.assert_called_once_with("/tmp/output.bin", target=None, compression=None)


class TestFlashPhase:
    """Tests for FlashPhase constants."""

    def test_standard_phases(self):
        assert FlashPhase.DOWNLOAD == "download"
        assert FlashPhase.EXTRACT == "extract"
        assert FlashPhase.STEP == "step"
        assert FlashPhase.CACHE == "cache"
        assert FlashPhase.COMPLETE == "complete"
        assert FlashPhase.ERROR == "error"

    def test_phases_are_strings(self):
        """FlashPhase values should be usable directly as FlashStatus phase."""
        status = FlashStatus(phase=FlashPhase.STEP, message="test")
        assert status.phase == "step"


class TestFlashStatusFields:
    """Tests for FlashStatus stdout/stderr fields."""

    def test_stdout_stderr_default_none(self):
        status = FlashStatus(phase=FlashPhase.STEP, message="test")
        assert status.stdout is None
        assert status.stderr is None

    def test_stdout_stderr_set(self):
        status = FlashStatus(
            phase=FlashPhase.STEP,
            message="test",
            stdout="output line",
            stderr="warning line",
        )
        assert status.stdout == "output line"
        assert status.stderr == "warning line"


class TestRenderFlashStatus:
    """Tests for StreamingFlasherClient.render_flash_status."""

    def test_basic_render(self):
        status = FlashStatus(phase=FlashPhase.STEP, message="Running qdl ufs")
        result = StreamingFlasherClient.render_flash_status(status)
        assert "STEP" in result
        assert "Running qdl ufs" in result

    def test_render_with_step_info(self):
        status = FlashStatus(
            phase=FlashPhase.STEP,
            message="Running",
            step_index=2,
            total_steps=5,
            step_name="Flash UFS",
        )
        result = StreamingFlasherClient.render_flash_status(status)
        assert "step 2/5" in result
        assert "Flash UFS" in result

    def test_render_with_progress(self):
        status = FlashStatus(phase=FlashPhase.DOWNLOAD, message="Downloading", progress=0.5)
        result = StreamingFlasherClient.render_flash_status(status)
        assert "50.0%" in result

    def test_render_with_bytes(self):
        status = FlashStatus(
            phase=FlashPhase.DOWNLOAD,
            message="Receiving",
            bytes_transferred=1024,
            bytes_total=4096,
        )
        result = StreamingFlasherClient.render_flash_status(status)
        assert "1024/4096 bytes" in result

    def test_render_verbose_with_stdout(self):
        status = FlashStatus(
            phase=FlashPhase.STEP,
            message="Completed",
            stdout="OKAY [ 0.123s]\n",
        )
        result = StreamingFlasherClient.render_flash_status(status, verbose=True)
        assert "[stdout]" in result
        assert "OKAY" in result

    def test_render_verbose_with_stderr(self):
        status = FlashStatus(
            phase=FlashPhase.STEP,
            message="Completed",
            stderr="Warning: slow device\n",
        )
        result = StreamingFlasherClient.render_flash_status(status, verbose=True)
        assert "[stderr]" in result
        assert "slow device" in result

    def test_render_not_verbose_hides_stdout_stderr(self):
        status = FlashStatus(
            phase=FlashPhase.STEP,
            message="Completed",
            stdout="hidden output",
            stderr="hidden error",
        )
        result = StreamingFlasherClient.render_flash_status(status, verbose=False)
        assert "[stdout]" not in result
        assert "[stderr]" not in result

    def test_render_verbose_no_output(self):
        status = FlashStatus(phase=FlashPhase.STEP, message="Completed")
        result = StreamingFlasherClient.render_flash_status(status, verbose=True)
        assert "[stdout]" not in result
        assert "[stderr]" not in result


class TestStreamingFlasherClient:
    """Tests for StreamingFlasherClient methods using mocks."""

    def _make_client(self):
        client = object.__new__(StreamingFlasherClient)
        client.description = None
        client.methods_description = {}
        return client

    def test_iter_flash_status_yields_statuses(self):
        client = self._make_client()
        statuses_data = [
            {"phase": "step", "message": "running"},
            {"phase": "complete", "message": "done"},
        ]
        client.streamingcall = MagicMock(return_value=iter(statuses_data))
        results = list(client._iter_flash_status(handle=None, manifest=None))
        assert len(results) == 2
        assert results[0].phase == FlashPhase.STEP
        assert results[1].phase == FlashPhase.COMPLETE

    def test_iter_flash_status_coerces_float_to_int(self):
        """Protobuf struct_pb2.Value stores all numbers as doubles, so integer
        fields like bytes_transferred arrive as floats after round-tripping
        through gRPC.  model_validate must accept them without strict mode."""
        client = self._make_client()
        statuses_data = [
            {
                "phase": "download",
                "message": "Received 480285 bytes",
                "bytes_transferred": 480285.0,
                "bytes_total": 1000000.0,
            },
            {"phase": "complete", "message": "done"},
        ]
        client.streamingcall = MagicMock(return_value=iter(statuses_data))
        results = list(client._iter_flash_status(handle=None, manifest=None))
        assert len(results) == 2
        assert results[0].bytes_transferred == 480285
        assert isinstance(results[0].bytes_transferred, int)
        assert results[0].bytes_total == 1000000
        assert isinstance(results[0].bytes_total, int)

    def test_iter_flash_status_raises_on_error(self):
        client = self._make_client()
        statuses_data = [
            {"phase": "step", "message": "running"},
            {"phase": "error", "message": "something failed"},
        ]
        client.streamingcall = MagicMock(return_value=iter(statuses_data))
        with pytest.raises(RuntimeError, match="something failed"):
            list(client._iter_flash_status(handle=None, manifest=None))

    def test_flash_stream_local_file(self, tmp_path):
        client = self._make_client()
        test_file = tmp_path / "firmware.bin"
        test_file.touch()

        mock_adapter = MagicMock()
        mock_adapter.__enter__ = MagicMock(return_value="local_handle")
        mock_adapter.__exit__ = MagicMock(return_value=False)

        statuses_data = [{"phase": "complete", "message": "done"}]
        client.streamingcall = MagicMock(return_value=iter(statuses_data))

        with patch("jumpstarter.client.flasher._local_file_adapter", return_value=mock_adapter):
            results = list(client.flash_stream(str(test_file)))
            assert len(results) == 1
            assert results[0].phase == FlashPhase.COMPLETE

    def test_flash_stream_http_url(self):
        client = self._make_client()

        mock_adapter = MagicMock()
        mock_adapter.__enter__ = MagicMock(return_value="http_handle")
        mock_adapter.__exit__ = MagicMock(return_value=False)

        statuses_data = [{"phase": "complete", "message": "done"}]
        client.streamingcall = MagicMock(return_value=iter(statuses_data))

        with patch("jumpstarter.client.flasher._http_url_adapter", return_value=mock_adapter):
            results = list(client.flash_stream("https://example.com/firmware.bin"))
            assert len(results) == 1

    def test_flash_stream_http_url_compression_warns(self):
        client = self._make_client()

        mock_adapter = MagicMock()
        mock_adapter.__enter__ = MagicMock(return_value="http_handle")
        mock_adapter.__exit__ = MagicMock(return_value=False)

        statuses_data = [{"phase": "complete", "message": "done"}]
        client.streamingcall = MagicMock(return_value=iter(statuses_data))

        with patch("jumpstarter.client.flasher._http_url_adapter", return_value=mock_adapter):
            with warnings.catch_warnings(record=True) as w:
                warnings.simplefilter("always")
                list(client.flash_stream("https://example.com/fw.bin", compression="zstd"))
                assert len(w) == 1
                assert "compression parameter is ignored" in str(w[0].message)

    def test_flash_returns_last_status(self, tmp_path):
        client = self._make_client()
        test_file = tmp_path / "firmware.bin"
        test_file.touch()

        mock_adapter = MagicMock()
        mock_adapter.__enter__ = MagicMock(return_value="handle")
        mock_adapter.__exit__ = MagicMock(return_value=False)

        statuses_data = [
            {"phase": "step", "message": "running"},
            {"phase": "complete", "message": "done"},
        ]
        client.streamingcall = MagicMock(return_value=iter(statuses_data))

        with patch("jumpstarter.client.flasher._local_file_adapter", return_value=mock_adapter):
            result = client.flash(str(test_file))
            assert result.phase == FlashPhase.COMPLETE

    def test_flash_raises_on_incomplete(self, tmp_path):
        client = self._make_client()
        test_file = tmp_path / "firmware.bin"
        test_file.touch()

        mock_adapter = MagicMock()
        mock_adapter.__enter__ = MagicMock(return_value="handle")
        mock_adapter.__exit__ = MagicMock(return_value=False)

        statuses_data = [{"phase": "step", "message": ""}]
        client.streamingcall = MagicMock(return_value=iter(statuses_data))

        with patch("jumpstarter.client.flasher._local_file_adapter", return_value=mock_adapter):
            with pytest.raises(RuntimeError, match="flash did not complete"):
                client.flash(str(test_file))

    def test_flash_raises_on_no_statuses(self, tmp_path):
        client = self._make_client()
        test_file = tmp_path / "firmware.bin"
        test_file.touch()

        mock_adapter = MagicMock()
        mock_adapter.__enter__ = MagicMock(return_value="handle")
        mock_adapter.__exit__ = MagicMock(return_value=False)

        client.streamingcall = MagicMock(return_value=iter([]))

        with patch("jumpstarter.client.flasher._local_file_adapter", return_value=mock_adapter):
            with pytest.raises(RuntimeError, match="without status updates"):
                client.flash(str(test_file))

    def test_flash_dict_raises_argument_error(self):
        from jumpstarter.common.exceptions import ArgumentError

        client = self._make_client()
        with pytest.raises(ArgumentError, match="does not support multi-target"):
            client.flash({"boot": "/tmp/boot.bin"})

    def test_flash_with_target_raises_argument_error(self, tmp_path):
        from jumpstarter.common.exceptions import ArgumentError

        client = self._make_client()
        test_file = tmp_path / "firmware.bin"
        test_file.touch()
        with pytest.raises(ArgumentError, match="not supported"):
            client.flash(str(test_file), target="boot")

    def test_cli_flash_command(self, tmp_path):
        client = self._make_client()

        mock_adapter = MagicMock()
        mock_adapter.__enter__ = MagicMock(return_value="handle")
        mock_adapter.__exit__ = MagicMock(return_value=False)

        statuses_data = [{"phase": "complete", "message": "done"}]
        client.streamingcall = MagicMock(return_value=iter(statuses_data))

        test_file = tmp_path / "firmware.bin"
        test_file.touch()

        with patch("jumpstarter.client.flasher._local_file_adapter", return_value=mock_adapter):
            group = client.cli()
            runner = click.testing.CliRunner()
            result = runner.invoke(group, ["flash", str(test_file)])
            assert result.exit_code == 0
            assert "COMPLETE" in result.output
