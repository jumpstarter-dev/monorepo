import json
from unittest.mock import AsyncMock, patch

from click.testing import CliRunner
from jumpstarter_kubernetes import (
    ApplyV1Alpha1Api,
    V1Alpha1AppliedResource,
    V1Alpha1AppliedResourceList,
)
from kubernetes_asyncio.client.exceptions import ApiException

from .apply import apply

CLIENT_MANIFEST = """\
apiVersion: jumpstarter.dev/v1alpha1
kind: Client
metadata:
  name: hello
"""

EXPORTER_SET_MANIFEST = """\
apiVersion: virtualtarget.jumpstarter.dev/v1alpha1
kind: ExporterSet
metadata:
  name: pool
"""


def applied(kind: str, name: str, action: str, api_version: str = "jumpstarter.dev/v1alpha1"):
    return V1Alpha1AppliedResource(
        apiVersion=api_version,
        kind=kind,
        name=name,
        namespace="default",
        action=action,
        resource={"apiVersion": api_version, "kind": kind, "metadata": {"name": name}},
    )


def run(args, files: dict[str, str], apply_all: AsyncMock, input: str | None = None):
    runner = CliRunner()
    with runner.isolated_filesystem():
        for filename, contents in files.items():
            with open(filename, "w") as f:
                f.write(contents)
        with (
            patch.object(ApplyV1Alpha1Api, "_load_kube_config", AsyncMock()),
            patch.object(ApplyV1Alpha1Api, "apply_all", apply_all),
        ):
            return runner.invoke(apply, args, input=input)


def test_apply_reports_what_happened_to_each_resource():
    apply_all = AsyncMock(
        return_value=V1Alpha1AppliedResourceList(
            items=[
                applied("Client", "hello", "created"),
                applied("ExporterSet", "pool", "configured", "virtualtarget.jumpstarter.dev/v1alpha1"),
            ]
        )
    )

    result = run(
        ["-f", "client.yaml", "-f", "set.yaml"],
        {"client.yaml": CLIENT_MANIFEST, "set.yaml": EXPORTER_SET_MANIFEST},
        apply_all,
    )

    assert result.exit_code == 0
    assert "client.jumpstarter.dev/hello created" in result.output
    assert "exporterset.virtualtarget.jumpstarter.dev/pool configured" in result.output
    # Both files reach the cluster in the order they were given.
    assert [m["kind"] for m in apply_all.call_args.args[0]] == ["Client", "ExporterSet"]
    assert apply_all.call_args.kwargs["dry_run"] is False


def test_apply_reads_a_multi_document_manifest():
    apply_all = AsyncMock(
        return_value=V1Alpha1AppliedResourceList(
            items=[
                applied("Client", "hello", "created"),
                applied("ExporterSet", "pool", "created", "virtualtarget.jumpstarter.dev/v1alpha1"),
            ]
        )
    )

    result = run(["-f", "both.yaml"], {"both.yaml": f"{CLIENT_MANIFEST}---\n{EXPORTER_SET_MANIFEST}"}, apply_all)

    assert result.exit_code == 0
    assert [m["kind"] for m in apply_all.call_args.args[0]] == ["Client", "ExporterSet"]


def test_apply_says_when_nothing_was_persisted():
    apply_all = AsyncMock(return_value=V1Alpha1AppliedResourceList(items=[applied("Client", "hello", "created")]))

    result = run(["-f", "client.yaml", "--dry-run"], {"client.yaml": CLIENT_MANIFEST}, apply_all)

    assert result.exit_code == 0
    assert "client.jumpstarter.dev/hello created (dry run)" in result.output
    assert apply_all.call_args.kwargs["dry_run"] is True


def test_apply_prints_names_only():
    apply_all = AsyncMock(return_value=V1Alpha1AppliedResourceList(items=[applied("Client", "hello", "created")]))

    result = run(["-f", "client.yaml", "-o", "name"], {"client.yaml": CLIENT_MANIFEST}, apply_all)

    assert result.exit_code == 0
    assert result.output.strip() == "client.jumpstarter.dev/hello"


def test_apply_prints_the_stored_resources_as_json():
    apply_all = AsyncMock(return_value=V1Alpha1AppliedResourceList(items=[applied("Client", "hello", "created")]))

    result = run(["-f", "client.yaml", "-o", "json"], {"client.yaml": CLIENT_MANIFEST}, apply_all)

    assert result.exit_code == 0
    parsed = json.loads(result.output)
    assert parsed["items"][0]["action"] == "created"
    assert parsed["items"][0]["resource"]["metadata"]["name"] == "hello"


def test_apply_reads_a_manifest_from_stdin():
    apply_all = AsyncMock(return_value=V1Alpha1AppliedResourceList(items=[applied("Client", "hello", "created")]))

    result = run(["-f", "-"], {}, apply_all, input=CLIENT_MANIFEST)

    assert result.exit_code == 0
    assert [m["kind"] for m in apply_all.call_args.args[0]] == ["Client"]


def test_apply_rejects_a_manifest_that_is_not_jumpstarters():
    apply_all = AsyncMock()

    result = run(
        ["-f", "secret.yaml"],
        {"secret.yaml": "apiVersion: v1\nkind: Secret\nmetadata:\n  name: creds\n"},
        apply_all,
    )

    assert result.exit_code != 0
    assert "core Kubernetes resource" in result.output
    # Nothing reaches the cluster when the manifest is refused.
    apply_all.assert_not_awaited()


def test_apply_requires_a_manifest():
    result = run([], {}, AsyncMock())

    assert result.exit_code != 0
    assert "Missing option" in result.output


CONFLICT_BODY = json.dumps(
    {
        "kind": "Status",
        "reason": "Conflict",
        "message": 'Apply failed with 1 conflict: conflict with "other-controller": .metadata.labels.owner',
    }
)


def conflict() -> ApiException:
    error = ApiException(status=409, reason="Conflict")
    error.body = CONFLICT_BODY
    return error


def test_apply_says_how_to_resolve_a_field_ownership_conflict():
    apply_all = AsyncMock(side_effect=conflict())

    result = run(["-f", "client.yaml"], {"client.yaml": CLIENT_MANIFEST}, apply_all)

    assert result.exit_code != 0
    assert ".metadata.labels.owner" in result.output
    assert "--force-conflicts" in result.output


def test_apply_does_not_suggest_a_flag_that_is_already_set():
    apply_all = AsyncMock(side_effect=conflict())

    result = run(["--force-conflicts", "-f", "client.yaml"], {"client.yaml": CLIENT_MANIFEST}, apply_all)

    assert result.exit_code != 0
    assert "--force-conflicts" not in result.output


def test_apply_asks_the_cluster_to_take_ownership_when_told_to():
    apply_all = AsyncMock(return_value=V1Alpha1AppliedResourceList(items=[applied("Client", "hello", "configured")]))

    result = run(["--force-conflicts", "-f", "client.yaml"], {"client.yaml": CLIENT_MANIFEST}, apply_all)

    assert result.exit_code == 0
    assert apply_all.call_args.kwargs["force_conflicts"] is True


def test_apply_leaves_conflicting_fields_alone_by_default():
    apply_all = AsyncMock(return_value=V1Alpha1AppliedResourceList(items=[applied("Client", "hello", "configured")]))

    result = run(["-f", "client.yaml"], {"client.yaml": CLIENT_MANIFEST}, apply_all)

    assert result.exit_code == 0
    assert apply_all.call_args.kwargs["force_conflicts"] is False
