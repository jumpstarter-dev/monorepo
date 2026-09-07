import json
import logging
import os
import subprocess
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
import requests

from .driver import Cuttlefish, CuttlefishError, CuttlefishTimeout, ImageDirGeneration

BASE = "http://localhost:2080"

_ADB_PATCHES = [
    patch("jumpstarter_driver_adb.driver.shutil.which", return_value="/usr/bin/adb"),
    patch("jumpstarter_driver_adb.driver.subprocess.run"),
]


@pytest.fixture
def drv():
    for p in _ADB_PATCHES:
        p.start()
    try:
        yield Cuttlefish(group="cvd_1", name="dev1")
    finally:
        for p in _ADB_PATCHES:
            p.stop()


def test_status_ok(requests_mock, drv):
    requests_mock.get(f"{BASE}/_debug/statusz", text="ok")
    assert drv.status() == "OK"


def test_status_connection_error(requests_mock, drv):
    requests_mock.get(f"{BASE}/_debug/statusz", exc=requests.ConnectionError)
    with pytest.raises(CuttlefishError, match="not connected"):
        drv.status()


def test_list_cvds(requests_mock, drv):
    body = {"cvds": [{"name": "dev1", "group": "cvd_1", "status": "Running"}]}
    requests_mock.get(f"{BASE}/cvds", json=body)
    result = json.loads(drv.list_cvds())
    assert result["cvds"][0]["name"] == "dev1"


def test_get_cvd(requests_mock, drv):
    body = {"cvds": [{"name": "dev1", "group": "cvd_1", "adb_port": 6520}]}
    requests_mock.get(f"{BASE}/cvds/cvd_1/dev1", json=body)
    result = json.loads(drv.get_cvd())
    assert result["cvds"][0]["adb_port"] == 6520


def test_get_cvd_http_error(requests_mock, drv):
    requests_mock.get(f"{BASE}/cvds/cvd_1/dev1", status_code=404, json={"error": "not found"})
    with pytest.raises(CuttlefishError, match="failed"):
        drv.get_cvd()


def test_create_cvd_ok(requests_mock, drv):
    """Operation returns done=false, then completes on poll."""
    requests_mock.post(f"{BASE}/cvds", json={"name": "op-1", "done": False})
    requests_mock.post(f"{BASE}/operations/op-1/:wait", json={"name": "op-1", "done": True})
    config = {"env_config": {}}
    result = json.loads(drv.create_cvd(json.dumps(config)))
    assert result["done"] is True
    history = [r for r in requests_mock.request_history if r.path == "/cvds"]
    assert history[0].json() == config


def test_create_cvd_invalid_json(drv):
    with pytest.raises(CuttlefishError, match="invalid JSON"):
        drv.create_cvd("not json {{{")


def test_wait_503_retry(requests_mock, drv):
    """503 should retry, then succeed."""
    requests_mock.post(f"{BASE}/cvds", json={"name": "op-1", "done": False})
    responses = [
        {"status_code": 503, "text": "unavailable"},
        {"status_code": 200, "json": {"name": "op-1", "done": True}},
    ]
    requests_mock.post(f"{BASE}/operations/op-1/:wait", responses)
    result = json.loads(drv.create_cvd("{}"))
    assert result["done"] is True


def test_wait_504_retry(requests_mock, drv):
    """504 should retry, then succeed."""
    requests_mock.post(f"{BASE}/cvds", json={"name": "op-1", "done": False})
    responses = [
        {"status_code": 504, "text": "timeout"},
        {"status_code": 200, "json": {"name": "op-1", "done": True}},
    ]
    requests_mock.post(f"{BASE}/operations/op-1/:wait", responses)
    result = json.loads(drv.create_cvd("{}"))
    assert result["done"] is True


def test_wait_500_error_with_body(requests_mock, drv):
    """500 with JSON error body should raise with message."""
    requests_mock.post(f"{BASE}/cvds", json={"name": "op-1", "done": False})
    requests_mock.post(
        f"{BASE}/operations/op-1/:wait",
        status_code=500,
        json={"error": "disk full", "details": "no space left"},
    )
    with pytest.raises(CuttlefishError, match="disk full"):
        drv.create_cvd("{}")


def test_wait_500_error_plain_text(requests_mock, drv):
    """500 with non-JSON body should still raise."""
    requests_mock.post(f"{BASE}/cvds", json={"name": "op-1", "done": False})
    requests_mock.post(f"{BASE}/operations/op-1/:wait", status_code=500, text="internal error")
    with pytest.raises(CuttlefishError, match="500"):
        drv.create_cvd("{}")


@patch("jumpstarter_driver_cuttlefish.driver.time.sleep")
def test_wait_timeout(mock_sleep, requests_mock, drv):
    """Operation that never completes should raise CuttlefishTimeout."""
    requests_mock.post(f"{BASE}/operations/op-1/:wait", exc=requests.Timeout)
    with pytest.raises(CuttlefishTimeout, match="timed out"):
        drv._wait_for_operation("op-1", timeout=0.1)


def test_wait_connection_lost(requests_mock, drv):
    """Connection drop during polling should raise."""
    requests_mock.post(f"{BASE}/operations/op-1/:wait", exc=requests.ConnectionError)
    with pytest.raises(CuttlefishError, match="lost connection"):
        drv._wait_for_operation("op-1")


def test_wait_unexpected_http_error(requests_mock, drv):
    """Non-500/503/504 error should raise."""
    requests_mock.post(f"{BASE}/operations/op-1/:wait", status_code=403, text="forbidden")
    with pytest.raises(CuttlefishError, match="failed"):
        drv._wait_for_operation("op-1")


def test_operation_wait_accepts_empty_success(requests_mock, drv):
    requests_mock.delete(f"{BASE}/cvd_imgs_dirs/dir-1", json={"name": "op-1", "done": False})
    requests_mock.post(f"{BASE}/operations/op-1/:wait", status_code=200, text="")

    assert drv._do_operation("DELETE", "/cvd_imgs_dirs/dir-1") is None


def _mock_op(requests_mock, method, path, op_name="op-1"):
    """Register mocks for an operation endpoint and its wait endpoint."""
    getattr(requests_mock, method)(f"{BASE}{path}", json={"name": op_name, "done": False})
    requests_mock.post(f"{BASE}/operations/{op_name}/:wait", json={"name": op_name, "done": True})


def test_start_cvd(requests_mock, drv):
    _mock_op(requests_mock, "post", "/cvds/cvd_1/dev1/:start")
    result = json.loads(drv.start_cvd())
    assert result["done"] is True


def test_stop_cvd(requests_mock, drv):
    _mock_op(requests_mock, "post", "/cvds/cvd_1/dev1/:stop")
    result = json.loads(drv.stop_cvd())
    assert result["done"] is True


def test_restart_cvd(requests_mock, drv):
    _mock_op(requests_mock, "post", "/cvds/cvd_1/dev1/:restart")
    result = json.loads(drv.restart_cvd())
    assert result["done"] is True


def test_delete_cvd(requests_mock, drv):
    _mock_op(requests_mock, "delete", "/cvds/cvd_1/dev1")
    result = json.loads(drv.delete_cvd())
    assert result["done"] is True


def test_powerwash_cvd(requests_mock, drv):
    _mock_op(requests_mock, "post", "/cvds/cvd_1/dev1/:powerwash")
    result = json.loads(drv.powerwash_cvd())
    assert result["done"] is True


def test_powerbtn_cvd(requests_mock, drv):
    _mock_op(requests_mock, "post", "/cvds/cvd_1/dev1/:powerbtn")
    result = json.loads(drv.powerbtn_cvd())
    assert result["done"] is True


def test_list_operations(requests_mock, drv):
    body = {"operations": [{"name": "op-1", "done": False}]}
    requests_mock.get(f"{BASE}/operations", json=body)
    result = json.loads(drv.list_operations())
    assert len(result["operations"]) == 1


def test_get_adb_port_ok(requests_mock, drv):
    body = {"cvds": [{"name": "dev1", "group": "cvd_1", "adb_port": 6520}]}
    requests_mock.get(f"{BASE}/cvds/cvd_1/dev1", json=body)
    assert drv.get_adb_port() == "6520"


def test_get_adb_port_no_cvds(requests_mock, drv):
    requests_mock.get(f"{BASE}/cvds/cvd_1/dev1", json={"cvds": []})
    with pytest.raises(CuttlefishError, match="no ADB port"):
        drv.get_adb_port()


def test_get_adb_port_missing_field(requests_mock, drv):
    requests_mock.get(f"{BASE}/cvds/cvd_1/dev1", json={"cvds": [{"name": "dev1"}]})
    with pytest.raises(CuttlefishError, match="no ADB port"):
        drv.get_adb_port()


def test_request_timeout(requests_mock, drv):
    requests_mock.get(f"{BASE}/_debug/statusz", exc=requests.Timeout)
    with pytest.raises(CuttlefishError, match="timed out"):
        drv.status()


def test_request_non_json_response(requests_mock, drv):
    requests_mock.get(f"{BASE}/_debug/statusz", text="ok", headers={"Content-Type": "text/plain"})
    assert drv.status() == "OK"


def test_request_custom_port():
    for p in _ADB_PATCHES:
        p.start()
    try:
        drv = Cuttlefish(host="10.0.0.1", port=9090)
        assert drv._base_url == "http://10.0.0.1:9090"
    finally:
        for p in _ADB_PATCHES:
            p.stop()


def test_scheme_https():
    for p in _ADB_PATCHES:
        p.start()
    try:
        drv = Cuttlefish(scheme="https", host="10.0.0.1", port=443)
        assert drv._base_url == "https://10.0.0.1:443"
    finally:
        for p in _ADB_PATCHES:
            p.stop()


def test_expected_adb_port(drv):
    assert drv._expected_adb_port == 6520


def test_expected_adb_port_instance_2():
    for p in _ADB_PATCHES:
        p.start()
    try:
        drv = Cuttlefish(instance_num=3)
        assert drv._expected_adb_port == 6522
    finally:
        for p in _ADB_PATCHES:
            p.stop()


def test_cvd_device(drv):
    assert drv._cvd_device == "localhost:6520"


def test_get_host(drv):
    assert drv.get_host() == "localhost"


def test_get_existing_cvds_ok(requests_mock, drv):
    body = {"cvds": [{"name": "dev1", "group": "cvd_1"}]}
    requests_mock.get(f"{BASE}/cvds", json=body)
    assert len(drv._get_existing_cvds()) == 1


def test_get_existing_cvds_unreachable(requests_mock, drv):
    requests_mock.get(f"{BASE}/cvds", exc=requests.ConnectionError)
    with pytest.raises(CuttlefishError, match="not connected"):
        drv._get_existing_cvds()


def test_get_existing_cvds_non_dict(requests_mock, drv):
    requests_mock.get(f"{BASE}/cvds", text="not json")
    with pytest.raises(CuttlefishError, match="unexpected response"):
        drv._get_existing_cvds()


def test_auto_connect_adb(drv):
    drv.children["adb"] = MagicMock()
    result = drv._auto_connect_adb()
    assert result == "localhost:6520"
    drv.children["adb"].connect_device.assert_called_once_with("localhost:6520")


def test_auto_connect_adb_failure(drv):
    mock_adb = MagicMock()
    mock_adb.connect_device.side_effect = RuntimeError("fail")
    drv.children["adb"] = mock_adb
    result = drv._auto_connect_adb()
    assert result == "localhost:6520"


def test_auto_connect_adb_no_child(drv):
    drv.children.pop("adb", None)
    result = drv._auto_connect_adb()
    assert result == "localhost:6520"


def test_auto_disconnect_adb(drv):
    drv.children["adb"] = MagicMock()
    drv._auto_disconnect_adb()
    drv.children["adb"].disconnect_device.assert_called_once_with("localhost:6520")


def test_auto_disconnect_adb_failure(drv):
    mock_adb = MagicMock()
    mock_adb.disconnect_device.side_effect = RuntimeError("fail")
    drv.children["adb"] = mock_adb
    drv._auto_disconnect_adb()


def test_auto_disconnect_adb_no_child(drv):
    drv.children.pop("adb", None)
    drv._auto_disconnect_adb()


def test_wait_boot_no_adb_child(drv):
    drv.children.pop("adb", None)
    drv._wait_boot(timeout=1)


def test_wait_boot_wrapper_zero_timeout(drv):
    drv.boot_timeout = 0
    assert drv.wait_boot(timeout=0) == "OK"


@patch("jumpstarter_driver_cuttlefish.driver.subprocess.run")
def test_wait_boot_success(mock_run, drv):
    mock_adb = MagicMock()
    mock_adb.adb_path = "/usr/bin/adb"
    mock_adb.adb_env.return_value = {}
    drv.children["adb"] = mock_adb

    call_count = [0]

    def fake_run(cmd, **kwargs):
        call_count[0] += 1
        if "devices" in cmd:
            return subprocess.CompletedProcess(cmd, 0, stdout="localhost:6520\tdevice\n")
        if "getprop" in cmd:
            return subprocess.CompletedProcess(cmd, 0, stdout="1\n")
        return subprocess.CompletedProcess(cmd, 0, stdout="connected\n")

    mock_run.side_effect = fake_run
    drv._wait_boot(timeout=30)


@patch("jumpstarter_driver_cuttlefish.driver.time.sleep")
@patch("jumpstarter_driver_cuttlefish.driver.subprocess.run")
def test_wait_boot_timeout(mock_run, mock_sleep, drv):
    mock_adb = MagicMock()
    mock_adb.adb_path = "/usr/bin/adb"
    mock_adb.adb_env.return_value = {}
    drv.children["adb"] = mock_adb

    mock_run.return_value = subprocess.CompletedProcess([], 0, stdout="")
    with pytest.raises(CuttlefishTimeout, match="did not come online"):
        drv._wait_boot(timeout=0.1)


def test_cvd_power_off_stop(requests_mock, drv):
    power = drv.children["power"]
    requests_mock.post(f"{BASE}/cvds/cvd_1/dev1/:stop", json={"name": "op-1", "done": False})
    requests_mock.post(f"{BASE}/operations/op-1/:wait", json={"name": "op-1", "done": True})
    power.off()


def test_cvd_power_off_destroy(requests_mock, drv):
    drv.children["adb"] = MagicMock()
    power = drv.children["power"]
    requests_mock.delete(f"{BASE}/cvds/cvd_1/dev1", json={"name": "op-1", "done": False})
    requests_mock.post(f"{BASE}/operations/op-1/:wait", json={"name": "op-1", "done": True})
    power.off(destroy=True)
    assert drv._cvd_group is None
    assert drv._cvd_name is None


def test_cvd_power_off_destroy_releases_active_generation(requests_mock, drv):
    drv.children["adb"] = MagicMock()
    drv._active = ImageDirGeneration("dir-active", {"images": "images-cs"})
    power = drv.children["power"]
    requests_mock.delete(f"{BASE}/cvds/cvd_1/dev1", json={"name": "op-cvd", "done": False})
    requests_mock.post(f"{BASE}/operations/op-cvd/:wait", json={"name": "op-cvd", "done": True})
    requests_mock.delete(f"{BASE}/cvd_imgs_dirs/dir-active", json={"name": "op-dir", "done": False})
    requests_mock.post(f"{BASE}/operations/op-dir/:wait", json={"name": "op-dir", "done": True})

    power.off(destroy=True)

    assert drv._active is None
    assert any(r.method == "DELETE" and r.path == "/cvd_imgs_dirs/dir-active" for r in requests_mock.request_history)


def test_cvd_power_off_destroy_retains_active_generation_on_cleanup_failure(requests_mock, drv):
    drv.children["adb"] = MagicMock()
    drv._active = ImageDirGeneration("dir-active", {"images": "images-cs"})
    power = drv.children["power"]
    requests_mock.delete(f"{BASE}/cvds/cvd_1/dev1", json={"name": "op-cvd", "done": False})
    requests_mock.post(f"{BASE}/operations/op-cvd/:wait", json={"name": "op-cvd", "done": True})
    requests_mock.delete(f"{BASE}/cvd_imgs_dirs/dir-active", status_code=500, json={"error": "temporary"})

    power.off(destroy=True)

    assert drv._active == ImageDirGeneration("dir-active", {"images": "images-cs"})


def test_cvd_power_on_existing_running(requests_mock, drv):
    drv.children["adb"] = MagicMock()
    drv.boot_timeout = 0
    power = drv.children["power"]
    requests_mock.get(
        f"{BASE}/cvds",
        json={"cvds": [{"name": "dev1", "group": "cvd_1", "status": "Running"}]},
    )
    power.on()
    assert drv._cvd_group == "cvd_1"
    assert drv._cvd_name == "dev1"


def test_cvd_power_on_existing_stopped(requests_mock, drv):
    drv.children["adb"] = MagicMock()
    drv.boot_timeout = 0
    power = drv.children["power"]
    requests_mock.get(
        f"{BASE}/cvds",
        json={"cvds": [{"name": "dev1", "group": "cvd_1", "status": "Stopped"}]},
    )
    requests_mock.post(f"{BASE}/cvds/cvd_1/dev1/:start", json={"name": "op-1", "done": False})
    requests_mock.post(f"{BASE}/operations/op-1/:wait", json={"name": "op-1", "done": True})
    power.on()


def test_cvd_power_on_create_new(requests_mock, drv):
    drv.children["adb"] = MagicMock()
    drv.boot_timeout = 0
    power = drv.children["power"]
    requests_mock.get(f"{BASE}/cvds", json={"cvds": []})
    requests_mock.post(f"{BASE}/cvds", json={"name": "op-1", "done": False})
    requests_mock.post(
        f"{BASE}/operations/op-1/:wait",
        json={"name": "op-1", "done": True, "cvds": [{"group": "cvd_1", "name": "dev1", "adb_port": 6520}]},
    )
    power.on()
    assert drv._cvd_group == "cvd_1"
    assert drv._cvd_name == "dev1"


def test_cvd_power_on_stale_cleanup(requests_mock, drv):
    drv.children["adb"] = MagicMock()
    drv.boot_timeout = 0
    power = drv.children["power"]
    requests_mock.get(
        f"{BASE}/cvds",
        json={
            "cvds": [
                {"name": "d1", "group": "cvd_1"},
                {"name": "d2", "group": "cvd_1"},
            ]
        },
    )
    requests_mock.delete(f"{BASE}/cvds/cvd_1/d1", json={"name": "op-d1", "done": False})
    requests_mock.delete(f"{BASE}/cvds/cvd_1/d2", json={"name": "op-d2", "done": False})
    requests_mock.post(f"{BASE}/operations/op-d1/:wait", json={"name": "op-d1", "done": True})
    requests_mock.post(f"{BASE}/operations/op-d2/:wait", json={"name": "op-d2", "done": True})
    requests_mock.post(f"{BASE}/cvds", json={"name": "op-c", "done": False})
    requests_mock.post(f"{BASE}/operations/op-c/:wait", json={"name": "op-c", "done": True})
    power.on()


def test_cvd_power_on_stale_cleanup_failure(requests_mock, drv):
    """Failed stale CVD deletion aborts instead of creating duplicate."""
    drv.children["adb"] = MagicMock()
    drv.boot_timeout = 0
    power = drv.children["power"]
    requests_mock.get(
        f"{BASE}/cvds",
        json={
            "cvds": [
                {"name": "d1", "group": "cvd_1"},
                {"name": "d2", "group": "cvd_1"},
            ]
        },
    )
    requests_mock.delete(f"{BASE}/cvds/cvd_1/d1", status_code=500, json={"error": "busy"})
    requests_mock.delete(f"{BASE}/cvds/cvd_1/d2", json={"name": "op-d2", "done": False})
    requests_mock.post(f"{BASE}/operations/op-d2/:wait", json={"name": "op-d2", "done": True})
    with pytest.raises(CuttlefishError, match="failed to delete stale CVDs"):
        power.on()


def test_cvd_power_on_port_mismatch(requests_mock, drv):
    drv.children["adb"] = MagicMock()
    drv.boot_timeout = 0
    power = drv.children["power"]
    requests_mock.get(f"{BASE}/cvds", json={"cvds": []})
    requests_mock.post(f"{BASE}/cvds", json={"name": "op-1", "done": False})
    requests_mock.post(
        f"{BASE}/operations/op-1/:wait",
        json={"name": "op-1", "done": True, "cvds": [{"group": "cvd_1", "name": "dev1", "adb_port": 9999}]},
    )
    requests_mock.delete(f"{BASE}/cvds/cvd_1/dev1", json={"name": "op-del", "done": False})
    requests_mock.post(f"{BASE}/operations/op-del/:wait", json={"name": "op-del", "done": True})
    with pytest.raises(CuttlefishError, match="adb_port 9999"):
        power.on()
    assert any(r.method == "DELETE" for r in requests_mock.request_history)


def test_cvd_power_read_not_implemented(drv):
    power = drv.children["power"]
    with pytest.raises(NotImplementedError):
        list(power.read())


def test_cvd_flasher_dump_not_implemented(drv):
    flasher = drv.children["storage"]
    with pytest.raises(NotImplementedError):
        flasher.dump("target")


def test_env_config_for_create_passthrough(drv):
    """With no staged image dirs, the configured env_config is returned as a copy."""
    drv.env_config = {"instances": [{"disk": {"default_build": "/home/vsoc-01/fetch"}}]}
    result = drv._env_config_for_create()
    assert result == drv.env_config
    assert result is not drv.env_config  # deep-copied, caller can't mutate config
    result["instances"][0]["disk"]["default_build"] = "mutated"
    assert drv.env_config["instances"][0]["disk"]["default_build"] == "/home/vsoc-01/fetch"


def test_env_config_for_create_injects_staged_dir(drv):
    """A staged dir is referenced by both host_package and every default_build."""
    drv.env_config = {"instances": [{"disk": {}}, {"disk": {}}], "common": {}}
    drv._staged = ImageDirGeneration("dir-7", {"images": "images-cs", "host_package": "host-cs"})
    result = drv._env_config_for_create()
    assert result["common"]["host_package"] == "@image_dirs/dir-7"
    assert all(inst["disk"]["default_build"] == "@image_dirs/dir-7" for inst in result["instances"])
    # source config is not mutated
    assert drv.env_config["common"] == {}


def test_env_config_for_create_synthesizes_instance_when_empty(drv):
    """With no configured instances, one is created so images are referenced."""
    drv.env_config = {}
    drv._staged = ImageDirGeneration("dir-9", {"images": "images-cs", "host_package": "host-cs"})
    result = drv._env_config_for_create()
    assert result["common"]["host_package"] == "@image_dirs/dir-9"
    assert result["instances"] == [{"disk": {"default_build": "@image_dirs/dir-9"}}]


def test_env_config_for_create_falls_back_to_active_dir(drv):
    """With nothing staged, the active dir is reused so recreate needs no reflash."""
    drv.env_config = {}
    drv._active = ImageDirGeneration("dir-active", {"images": "images-cs", "host_package": "host-cs"})
    result = drv._env_config_for_create()
    assert result["common"]["host_package"] == "@image_dirs/dir-active"


def test_env_config_for_create_prefers_staged_over_active(drv):
    """A pending flash (staged) wins over the previous generation's active dir."""
    drv.env_config = {}
    drv._active = ImageDirGeneration("dir-old", {"images": "images-cs", "host_package": "host-cs"})
    drv._staged = ImageDirGeneration("dir-new", {"images": "new-images-cs", "host_package": "new-host-cs"})
    result = drv._env_config_for_create()
    assert result["common"]["host_package"] == "@image_dirs/dir-new"


class _FakeResource:
    """Stand-in for Driver.resource(): async CM yielding an async byte iterator."""

    def __init__(self, chunks):
        self._chunks = chunks

    async def __aenter__(self):
        async def gen():
            for c in self._chunks:
                yield c

        return gen()

    async def __aexit__(self, *exc):
        return False


class _FailingResource:
    """Stand-in for a resource that fails while yielding its content."""

    async def __aenter__(self):
        async def gen():
            yield b"partial archive"
            raise RuntimeError("stream failed")

        return gen()

    async def __aexit__(self, *exc):
        return False


@pytest.mark.asyncio
async def test_flash_stages_image_dir(requests_mock, drv, monkeypatch):
    """Happy path: existing artifact → extract → create + populate image dir → staged."""
    import hashlib

    payload = b"PK\x03\x04fake-image-zip"
    checksum = hashlib.sha256(payload).hexdigest()
    flasher = drv.children["storage"]
    monkeypatch.setattr(flasher, "resource", lambda source: _FakeResource([payload]))

    # Artifact already present -> upload skipped (no httpx needed).
    requests_mock.get(f"{BASE}/v1/userartifacts/{checksum}", status_code=200, json={})
    requests_mock.post(f"{BASE}/v1/userartifacts/{checksum}/:extract", json={"name": "op-x", "done": False})
    requests_mock.post(f"{BASE}/operations/op-x/:wait", json={"name": "op-x", "done": True})
    requests_mock.post(f"{BASE}/cvd_imgs_dirs", json={"name": "op-c", "done": False})
    requests_mock.post(f"{BASE}/operations/op-c/:wait", json={"name": "op-c", "done": True, "id": "dir-7"})
    requests_mock.put(f"{BASE}/cvd_imgs_dirs/dir-7", json={"name": "op-p", "done": False})
    requests_mock.post(f"{BASE}/operations/op-p/:wait", json={"name": "op-p", "done": True})

    await flasher.flash("handle")

    # Staged, not yet active — the live dir only flips over on power.on().
    assert drv._staged == ImageDirGeneration("dir-7", {"images": checksum, "host_package": checksum})
    assert drv._active is None


@pytest.mark.asyncio
async def test_flash_uploads_when_artifact_absent(requests_mock, drv, monkeypatch):
    """A 404 stat triggers the upload path with the streamed content's SHA-256."""
    import hashlib

    payload = b"PK\x03\x04another-image"
    checksum = hashlib.sha256(payload).hexdigest()
    flasher = drv.children["storage"]
    monkeypatch.setattr(flasher, "resource", lambda source: _FakeResource([payload]))

    uploaded = {}

    async def fake_upload(cs, path, size, suffix):
        uploaded["checksum"] = cs
        uploaded["size"] = size
        uploaded["suffix"] = suffix

    monkeypatch.setattr(flasher, "_upload", fake_upload)

    requests_mock.get(f"{BASE}/v1/userartifacts/{checksum}", status_code=404, json={"error": "not found"})
    requests_mock.post(f"{BASE}/v1/userartifacts/{checksum}/:extract", json={"name": "op-x", "done": False})
    requests_mock.post(f"{BASE}/operations/op-x/:wait", json={"name": "op-x", "done": True})
    requests_mock.post(f"{BASE}/cvd_imgs_dirs", json={"name": "op-c", "done": False})
    requests_mock.post(f"{BASE}/operations/op-c/:wait", json={"name": "op-c", "done": True, "id": "dir-9"})
    requests_mock.put(f"{BASE}/cvd_imgs_dirs/dir-9", json={"name": "op-p", "done": False})
    requests_mock.post(f"{BASE}/operations/op-p/:wait", json={"name": "op-p", "done": True})

    await flasher.flash("handle")

    # The zip magic drives the stored suffix HO's extractor dispatches on.
    assert uploaded == {"checksum": checksum, "size": len(payload), "suffix": ".zip"}
    assert drv._staged == ImageDirGeneration("dir-9", {"images": checksum, "host_package": checksum})


def test_create_image_dir_missing_id(requests_mock, drv):
    """A create response with no id is a hard error, not a silent None."""
    requests_mock.post(f"{BASE}/cvd_imgs_dirs", json={"name": "op-c", "done": False})
    requests_mock.post(f"{BASE}/operations/op-c/:wait", json={"name": "op-c", "done": True})
    with pytest.raises(CuttlefishError, match="no 'id'"):
        drv._create_image_dir()


def _mock_flash_endpoints(requests_mock, checksum, dir_id):
    """Register the HO calls a single flash() makes (artifact already present)."""
    requests_mock.get(f"{BASE}/v1/userartifacts/{checksum}", status_code=200, json={})
    requests_mock.post(f"{BASE}/v1/userartifacts/{checksum}/:extract", json={"name": "op-x", "done": False})
    requests_mock.post(f"{BASE}/operations/op-x/:wait", json={"name": "op-x", "done": True})
    requests_mock.post(f"{BASE}/cvd_imgs_dirs", json={"name": "op-c", "done": False})
    requests_mock.post(f"{BASE}/operations/op-c/:wait", json={"name": "op-c", "done": True, "id": dir_id})
    requests_mock.put(f"{BASE}/cvd_imgs_dirs/{dir_id}", json={"name": "op-p", "done": False})
    requests_mock.post(f"{BASE}/operations/op-p/:wait", json={"name": "op-p", "done": True})


@pytest.mark.asyncio
async def test_flash_then_power_on_creates_from_staged_images(requests_mock, drv, monkeypatch):
    """The full sequence: flash stages a dir, power.on() builds the CVD from it.

    This is the hop that a happy-path flash test alone would miss — power.on()
    calls _env_config_for_create() unconditionally, so a broken injection only
    surfaces here.
    """
    import hashlib

    payload = b"PK\x03\x04complete-bundle"
    checksum = hashlib.sha256(payload).hexdigest()
    drv.env_config = {"instances": [{"disk": {}}], "common": {}}
    drv.boot_timeout = 0
    drv.children["adb"] = MagicMock()
    flasher = drv.children["storage"]
    power = drv.children["power"]
    monkeypatch.setattr(flasher, "resource", lambda source: _FakeResource([payload]))

    _mock_flash_endpoints(requests_mock, checksum, "dir-7")
    await flasher.flash("handle")

    requests_mock.get(f"{BASE}/cvds", json={"cvds": []})
    requests_mock.post(f"{BASE}/cvds", json={"name": "op-cr", "done": False})
    requests_mock.post(
        f"{BASE}/operations/op-cr/:wait",
        json={"name": "op-cr", "done": True, "cvds": [{"group": "cvd_1", "name": "dev1", "adb_port": 6520}]},
    )
    power.on()

    create = [r for r in requests_mock.request_history if r.method == "POST" and r.path == "/cvds"][-1]
    env = create.json()["env_config"]
    assert env["common"]["host_package"] == "@image_dirs/dir-7"
    assert env["instances"][0]["disk"]["default_build"] == "@image_dirs/dir-7"

    # Creating the CVD consumes the staged dir: it becomes the active (live) dir.
    assert drv._staged is None
    assert drv._active == ImageDirGeneration("dir-7", {"images": checksum, "host_package": checksum})


@pytest.mark.asyncio
async def test_flash_two_artifacts_merge_into_one_dir(requests_mock, drv, monkeypatch):
    """Two flash() calls before a create reuse one staged dir (HO merges on populate).

    This is the images + host_package split: `client.storage.flash({...})`
    dispatches one flash() per entry, each PUT-populating the *same* dir with a
    different checksum. Only the first call creates the dir.
    """
    import hashlib

    images = b"PK\x03\x04device-images"
    host_pkg = b"\x1f\x8bhost-package-tarball"
    images_cs = hashlib.sha256(images).hexdigest()
    host_cs = hashlib.sha256(host_pkg).hexdigest()
    flasher = drv.children["storage"]

    payloads = iter([images, host_pkg])
    monkeypatch.setattr(flasher, "resource", lambda source: _FakeResource([next(payloads)]))

    # Only one dir is created (op-c → dir-1); each artifact PUT-populates it.
    _mock_flash_endpoints(requests_mock, images_cs, "dir-1")
    requests_mock.get(f"{BASE}/v1/userartifacts/{host_cs}", status_code=200, json={})
    requests_mock.post(f"{BASE}/v1/userartifacts/{host_cs}/:extract", json={"name": "op-x2", "done": False})
    requests_mock.post(f"{BASE}/operations/op-x2/:wait", json={"name": "op-x2", "done": True})

    await flasher.flash("images-handle", target="images")
    await flasher.flash("host-pkg-handle", target="host_package")

    assert drv._staged == ImageDirGeneration("dir-1", {"images": images_cs, "host_package": host_cs})
    # Exactly one dir created, but both artifacts populated into it.
    creates = [r for r in requests_mock.request_history if r.method == "POST" and r.path == "/cvd_imgs_dirs"]
    assert len(creates) == 1
    populates = [r for r in requests_mock.request_history if r.method == "PUT" and r.path == "/cvd_imgs_dirs/dir-1"]
    assert len(populates) == 2
    assert {p.json()["user_artifact_checksum"] for p in populates} == {images_cs, host_cs}
    assert drv._staged.artifacts == {"images": images_cs, "host_package": host_cs}


@pytest.mark.asyncio
async def test_partial_flash_carries_forward_active_artifacts(requests_mock, drv, monkeypatch):
    """A partial flash creates an immutable generation with active artifacts copied by checksum."""
    import hashlib

    host_pkg = b"\x1f\x8bnew-host-package"
    host_cs = hashlib.sha256(host_pkg).hexdigest()
    old_images_cs = "old-images-checksum"
    old_host_cs = "old-host-checksum"
    drv._active = ImageDirGeneration("dir-old", {"images": old_images_cs, "host_package": old_host_cs})
    flasher = drv.children["storage"]
    monkeypatch.setattr(flasher, "resource", lambda source: _FakeResource([host_pkg]))

    requests_mock.get(f"{BASE}/v1/userartifacts/{host_cs}", status_code=200, json={})
    requests_mock.post(f"{BASE}/v1/userartifacts/{host_cs}/:extract", json={"name": "op-x", "done": False})
    requests_mock.post(f"{BASE}/operations/op-x/:wait", json={"name": "op-x", "done": True})
    requests_mock.post(f"{BASE}/cvd_imgs_dirs", json={"name": "op-c", "done": False})
    requests_mock.post(f"{BASE}/operations/op-c/:wait", json={"name": "op-c", "done": True, "id": "dir-new"})
    requests_mock.put(f"{BASE}/cvd_imgs_dirs/dir-new", json={"name": "op-p", "done": False})
    requests_mock.post(f"{BASE}/operations/op-p/:wait", json={"name": "op-p", "done": True})

    await flasher.flash("host-package", target="host_package")

    populates = [r.json()["user_artifact_checksum"] for r in requests_mock.request_history if r.method == "PUT"]
    assert populates == [host_cs, old_images_cs]
    assert drv._staged == ImageDirGeneration("dir-new", {"host_package": host_cs, "images": old_images_cs})
    env = drv._env_config_for_create()
    assert env["common"]["host_package"] == "@image_dirs/dir-new"
    assert env["instances"][0]["disk"]["default_build"] == "@image_dirs/dir-new"


@pytest.mark.asyncio
async def test_put_chunk_retries_same_offset(drv):
    """A failed chunk is retried with the same offset and payload."""
    import httpx

    class FakeResponse:
        def raise_for_status(self):
            return None

    class FakeClient:
        def __init__(self):
            self.calls = []

        async def put(self, url, *, files, data):
            self.calls.append((url, files, data))
            if len(self.calls) == 1:
                raise httpx.ReadError("transient failure")
            return FakeResponse()

    client = FakeClient()
    flasher = drv.children["storage"]
    with patch("jumpstarter_driver_cuttlefish.driver.sleep", new=AsyncMock()):
        await flasher._put_chunk(client, "http://localhost/upload", "artifact.zip", b"chunk", 16, 100)

    assert len(client.calls) == 2
    assert (
        client.calls[0][2]
        == client.calls[1][2]
        == {
            "chunk_offset_bytes": "16",
            "file_size_bytes": "100",
        }
    )
    assert client.calls[0][1]["file"][1] == client.calls[1][1]["file"][1] == b"chunk"


@pytest.mark.asyncio
async def test_spool_removes_partial_file_on_stream_failure(drv, tmp_path, monkeypatch):
    """A failed source stream does not leave a partial spool file behind."""
    spool_path = tmp_path / "cvd-flash-failed"

    def fake_mkstemp(prefix):
        return os.open(spool_path, os.O_RDWR | os.O_CREAT | os.O_EXCL), str(spool_path)

    flasher = drv.children["storage"]
    monkeypatch.setattr("jumpstarter_driver_cuttlefish.driver.tempfile.mkstemp", fake_mkstemp)
    monkeypatch.setattr(flasher, "resource", lambda source: _FailingResource())

    with pytest.raises(RuntimeError, match="stream failed"):
        await flasher._spool("handle")

    assert not spool_path.exists()


@pytest.mark.asyncio
async def test_upload_uses_bounded_network_timeouts(drv, tmp_path, monkeypatch):
    """Upload connections have finite control-plane timeouts and unbounded writes."""
    import httpx

    class FakeResponse:
        def raise_for_status(self):
            return None

    class FakeClient:
        instances = []

        def __init__(self, *, timeout):
            self.timeout = timeout
            self.instances.append(self)

        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

        async def put(self, url, *, files, data):
            return FakeResponse()

    monkeypatch.setattr("jumpstarter_driver_cuttlefish.driver.httpx.AsyncClient", FakeClient)
    path = tmp_path / "artifact.zip"
    path.write_bytes(b"artifact")

    await drv.children["storage"]._upload("checksum", str(path), path.stat().st_size, ".zip")

    timeout = FakeClient.instances[0].timeout
    assert isinstance(timeout, httpx.Timeout)
    assert timeout.connect == 10.0
    assert timeout.read == 60.0
    assert timeout.write is None
    assert timeout.pool == 10.0


def test_targeted_generation_injects_only_recorded_roles(drv):
    """A generation injects only the roles it contains."""
    drv.env_config = {
        "instances": [{"disk": {"default_build": "/old/images"}}],
        "common": {"host_package": "/old/host"},
    }
    drv._staged = ImageDirGeneration("dir-images", {"images": "images-cs"})

    result = drv._env_config_for_create()

    assert result["instances"][0]["disk"]["default_build"] == "@image_dirs/dir-images"
    assert result["common"]["host_package"] == "/old/host"


@pytest.mark.asyncio
async def test_flash_rejects_unknown_target(drv, monkeypatch):
    """Cuttlefish storage exposes only the documented target roles."""
    flasher = drv.children["storage"]
    resource = MagicMock()
    monkeypatch.setattr(flasher, "resource", resource)

    with pytest.raises(CuttlefishError, match="expected 'images' or 'host_package'"):
        await flasher.flash("handle", target="system")
    resource.assert_not_called()


@pytest.mark.asyncio
async def test_flash_rejects_target_format_mismatch(drv, monkeypatch):
    """The documented target role must match the archive format."""
    flasher = drv.children["storage"]
    monkeypatch.setattr(flasher, "resource", lambda source: _FakeResource([b"\x1f\x8bhost-package"]))

    with pytest.raises(CuttlefishError, match=r"requires \.zip"):
        await flasher.flash("handle", target="images")


@pytest.mark.asyncio
async def test_flash_after_create_starts_new_generation(requests_mock, drv, monkeypatch):
    """A flash after a create begins a fresh staged dir, leaving the active one intact."""
    import hashlib

    payload = b"PK\x03\x04new-generation"
    checksum = hashlib.sha256(payload).hexdigest()
    flasher = drv.children["storage"]
    monkeypatch.setattr(flasher, "resource", lambda source: _FakeResource([payload]))
    # Simulate a prior generation already booted from dir-old.
    drv._active = ImageDirGeneration("dir-old", {"images": "old-images", "host_package": "old-host"})

    _mock_flash_endpoints(requests_mock, checksum, "dir-new")
    await flasher.flash("handle")

    # New dir staged; the live dir is untouched until the next create consumes it.
    assert drv._staged == ImageDirGeneration("dir-new", {"images": checksum, "host_package": checksum})
    assert drv._active == ImageDirGeneration("dir-old", {"images": "old-images", "host_package": "old-host"})
    assert not any(r.method == "DELETE" for r in requests_mock.request_history)


@pytest.mark.asyncio
async def test_extract_tolerates_already_extracted(requests_mock, drv, monkeypatch):
    """A 409 from :extract (artifact already extracted) is treated as success."""
    import hashlib

    payload = b"PK\x03\x04already-extracted"
    checksum = hashlib.sha256(payload).hexdigest()
    flasher = drv.children["storage"]
    monkeypatch.setattr(flasher, "resource", lambda source: _FakeResource([payload]))

    requests_mock.get(f"{BASE}/v1/userartifacts/{checksum}", status_code=200, json={})
    requests_mock.post(f"{BASE}/v1/userartifacts/{checksum}/:extract", status_code=409, json={"error": "extracted"})
    requests_mock.post(f"{BASE}/cvd_imgs_dirs", json={"name": "op-c", "done": False})
    requests_mock.post(f"{BASE}/operations/op-c/:wait", json={"name": "op-c", "done": True, "id": "dir-7"})
    requests_mock.put(f"{BASE}/cvd_imgs_dirs/dir-7", json={"name": "op-p", "done": False})
    requests_mock.post(f"{BASE}/operations/op-p/:wait", json={"name": "op-p", "done": True})

    await flasher.flash("handle")

    assert drv._staged == ImageDirGeneration("dir-7", {"images": checksum, "host_package": checksum})


def test_extract_tolerates_already_extracted_from_wait(requests_mock, drv):
    checksum = "abc123"
    requests_mock.post(
        f"{BASE}/v1/userartifacts/{checksum}/:extract",
        json={"name": "op-x", "done": False},
    )
    requests_mock.post(
        f"{BASE}/operations/op-x/:wait",
        status_code=409,
        json={"error": "already extracted"},
    )

    drv._extract_artifact(checksum)


@pytest.mark.asyncio
async def test_spool_sniffs_gzip_suffix(drv, monkeypatch):
    """A gzip-magic stream is spooled with a .tar.gz suffix (host package)."""
    flasher = drv.children["storage"]
    monkeypatch.setattr(flasher, "resource", lambda source: _FakeResource([b"\x1f\x8bhost-package"]))
    tmp_path, _checksum, _size, suffix = await flasher._spool("handle")
    try:
        assert suffix == ".tar.gz"
    finally:
        os.unlink(tmp_path)


@pytest.mark.asyncio
async def test_spool_rejects_unknown_format(drv, monkeypatch):
    """A stream with no recognized archive magic is rejected and its spool removed."""
    flasher = drv.children["storage"]
    monkeypatch.setattr(flasher, "resource", lambda source: _FakeResource([b"not-an-archive"]))
    with pytest.raises(CuttlefishError, match="unsupported archive format"):
        await flasher._spool("handle")


def test_flasher_close_reclaims_staged_but_retains_active_dir(requests_mock, drv, caplog):
    """close() cleans pending work but retains a dir a live CVD may reference."""
    flasher = drv.children["storage"]
    drv._staged = ImageDirGeneration("dir-staged", {"images": "images-cs"})
    drv._active = ImageDirGeneration("dir-active", {"images": "images-cs", "host_package": "host-cs"})
    requests_mock.delete(f"{BASE}/cvd_imgs_dirs/dir-staged", json={"name": "op-staged", "done": False})
    requests_mock.post(f"{BASE}/operations/op-staged/:wait", json={"name": "op-staged", "done": True})

    caplog.set_level(logging.WARNING)
    flasher.close()

    assert drv._staged is None
    assert drv._active is None
    deleted = {r.path for r in requests_mock.request_history if r.method == "DELETE"}
    assert deleted == {"/cvd_imgs_dirs/dir-staged"}
    assert "leaving active image dir dir-active at exporter teardown" in caplog.text


def test_flasher_close_survives_delete_failure(requests_mock, drv, caplog):
    """A failed cleanup DELETE is logged, not raised, and state is still cleared."""
    flasher = drv.children["storage"]
    drv._staged = ImageDirGeneration("dir-7", {"images": "images-cs"})
    requests_mock.delete(f"{BASE}/cvd_imgs_dirs/dir-7", status_code=500, json={"error": "in use"})

    caplog.set_level(logging.WARNING)
    flasher.close()

    assert drv._staged is None
    assert drv._active is None
    assert "failed to delete image dir dir-7; it may need manual cleanup" in caplog.text


def test_cvd_power_on_ignores_other_groups(requests_mock, drv):
    """CVDs from other groups are not touched."""
    drv.children["adb"] = MagicMock()
    drv.boot_timeout = 0
    power = drv.children["power"]
    requests_mock.get(
        f"{BASE}/cvds",
        json={
            "cvds": [
                {"name": "d1", "group": "other_group", "status": "Running"},
                {"name": "dev1", "group": "cvd_1", "status": "Running"},
            ]
        },
    )
    power.on()
    assert drv._cvd_group == "cvd_1"
    assert drv._cvd_name == "dev1"
    assert not any(r.method == "DELETE" for r in requests_mock.request_history)
