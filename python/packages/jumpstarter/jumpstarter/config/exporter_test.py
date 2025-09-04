from pathlib import Path

import pytest

from .common import ObjectMeta
from .exporter import ExporterConfigV1Alpha1, ExporterConfigV1Alpha1DriverInstance
from .tls import TLSConfigV1Alpha1

pytestmark = pytest.mark.anyio


def test_exporter_config(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    monkeypatch.setattr(ExporterConfigV1Alpha1, "BASE_PATH", tmp_path)

    path = tmp_path / "test.yaml"

    text = """apiVersion: jumpstarter.dev/v1alpha1
kind: ExporterConfig
metadata:
  namespace: default
  name: test
tls:
  ca: "cacertificatedata"
  insecure: true
endpoint: "jumpstarter.my-lab.com:1443"
token: "dGhpc2lzYXRva2VuLTEyMzQxMjM0MTIzNEyMzQtc2Rxd3Jxd2VycXdlcnF3ZXJxd2VyLTEyMzQxMjM0MTIz"

export:
  power:
    type: "jumpstarter_driver_power.driver.PduPower"
    config:
      host: "192.168.1.111"
      port: 1234
      auth:
          username: "admin"
          password: "secret"
  serial:
    type: "jumpstarter_driver_pyserial.driver.Pyserial"
    config:
      port: "/dev/ttyUSB0"
      baudrate: 115200
  nested:
    children:
      custom:
        type: "vendorpackage.CustomDriver"
        config:
          hello: "world"
"""
    path.write_text(
        text,
        encoding="utf-8",
    )

    config = ExporterConfigV1Alpha1.load("test")

    assert config == ExporterConfigV1Alpha1(
        alias="test",
        apiVersion="jumpstarter.dev/v1alpha1",
        kind="ExporterConfig",
        metadata=ObjectMeta(namespace="default", name="test"),
        endpoint="jumpstarter.my-lab.com:1443",
        token="dGhpc2lzYXRva2VuLTEyMzQxMjM0MTIzNEyMzQtc2Rxd3Jxd2VycXdlcnF3ZXJxd2VyLTEyMzQxMjM0MTIz",
        tls=TLSConfigV1Alpha1(ca="cacertificatedata", insecure=True),
        export={
            "power": ExporterConfigV1Alpha1DriverInstance(
                type="jumpstarter_driver_power.driver.PduPower",
                config={
                    "host": "192.168.1.111",
                    "port": 1234,
                    "auth": {
                        "username": "admin",
                        "password": "secret",
                    },
                },
            ),
            "serial": ExporterConfigV1Alpha1DriverInstance(
                type="jumpstarter_driver_pyserial.driver.Pyserial",
                config={
                    "port": "/dev/ttyUSB0",
                    "baudrate": 115200,
                },
            ),
            "nested": ExporterConfigV1Alpha1DriverInstance(
                children={
                    "custom": ExporterConfigV1Alpha1DriverInstance(
                        type="vendorpackage.CustomDriver",
                        children={},
                        config={
                            "hello": "world",
                        },
                    )
                },
            ),
        },
        config={},
        path=path,
    )

    path.unlink()

    ExporterConfigV1Alpha1.save(config)

    assert config == ExporterConfigV1Alpha1.load("test")


def test_exporter_config_with_hooks(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    monkeypatch.setattr(ExporterConfigV1Alpha1, "BASE_PATH", tmp_path)

    path = tmp_path / "test-hooks.yaml"

    text = """apiVersion: jumpstarter.dev/v1alpha1
kind: ExporterConfig
metadata:
  namespace: default
  name: test-hooks
endpoint: "jumpstarter.my-lab.com:1443"
token: "test-token"
hooks:
  preLease: |
    echo "Pre-lease hook for $LEASE_NAME"
    j power on
  postLease: |
    echo "Post-lease hook for $LEASE_NAME"
    j power off
  timeout: 600
export:
  power:
    type: "jumpstarter_driver_power.driver.PduPower"
"""
    path.write_text(
        text,
        encoding="utf-8",
    )

    config = ExporterConfigV1Alpha1.load("test-hooks")

    assert config.hooks.pre_lease == 'echo "Pre-lease hook for $LEASE_NAME"\nj power on\n'
    assert config.hooks.post_lease == 'echo "Post-lease hook for $LEASE_NAME"\nj power off\n'
    assert config.hooks.timeout == 600

    # Test that it round-trips correctly
    path.unlink()
    ExporterConfigV1Alpha1.save(config)
    reloaded_config = ExporterConfigV1Alpha1.load("test-hooks")

    assert reloaded_config.hooks.pre_lease == config.hooks.pre_lease
    assert reloaded_config.hooks.post_lease == config.hooks.post_lease
    assert reloaded_config.hooks.timeout == config.hooks.timeout

    # Test that the YAML uses camelCase
    yaml_output = ExporterConfigV1Alpha1.dump_yaml(config)
    assert "preLease:" in yaml_output
    assert "postLease:" in yaml_output
    assert "pre_lease:" not in yaml_output
    assert "post_lease:" not in yaml_output
