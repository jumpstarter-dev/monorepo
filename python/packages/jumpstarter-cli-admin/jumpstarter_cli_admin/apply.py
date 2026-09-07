from http import HTTPStatus
from typing import IO, Optional

import click
from jumpstarter_cli_common.blocking import blocking
from jumpstarter_cli_common.opt import (
    OutputType,
    opt_context,
    opt_kubeconfig,
    opt_namespace,
    opt_output_all,
)
from jumpstarter_cli_common.print import model_print
from jumpstarter_kubernetes import ApplyV1Alpha1Api, ManifestError, load_manifests
from kubernetes_asyncio.client.exceptions import ApiException
from kubernetes_asyncio.config.config_exception import ConfigException

from .k8s import (
    handle_k8s_api_exception,
    handle_k8s_config_exception,
)


@click.command("apply")
@click.option(
    "-f",
    "--filename",
    "filenames",
    type=click.File("r"),
    multiple=True,
    required=True,
    help="Manifest to apply, or '-' to read from stdin. Can be set multiple times.",
)
@click.option(
    "--force-conflicts",
    is_flag=True,
    default=False,
    help="Take ownership of fields another manager holds, instead of reporting the conflict.",
)
@click.option(
    "--dry-run",
    is_flag=True,
    default=False,
    help="Send the manifests to the server for validation without persisting them.",
)
@opt_namespace
@opt_kubeconfig
@opt_context
@opt_output_all
@blocking
async def apply(
    filenames: tuple[IO, ...],
    force_conflicts: bool,
    dry_run: bool,
    namespace: str,
    kubeconfig: Optional[str],
    context: Optional[str],
    output: OutputType,
):
    """Apply Jumpstarter manifests to a Kubernetes cluster

    Creates the resources in a manifest, or updates them in place when they
    already exist. Only Jumpstarter resources (clients, exporters, exporter
    sets, virtual target classes) can be applied.
    """
    try:
        manifests = []
        for file in filenames:
            manifests.extend(load_manifests(file.read(), file.name))
    except ManifestError as e:
        raise click.ClickException(str(e)) from e

    try:
        async with ApplyV1Alpha1Api(namespace, kubeconfig, context) as api:
            applied = await api.apply_all(manifests, dry_run=dry_run, force_conflicts=force_conflicts)
    except ManifestError as e:
        raise click.ClickException(str(e)) from e
    except ApiException as e:
        try:
            handle_k8s_api_exception(e)
        except click.ClickException as error:
            if e.status == HTTPStatus.CONFLICT and not force_conflicts:
                # The conflict names the fields; say how to win them on purpose.
                error.message += "\nRe-run with --force-conflicts to take ownership of those fields."
            raise
    except ConfigException as e:
        handle_k8s_config_exception(e)

    if output is None:
        # Mirror kubectl: one line per resource saying what happened to it.
        suffix = " (dry run)" if dry_run else ""
        for resource in applied.items:
            click.echo(f"{resource.qualified_name} {resource.action}{suffix}")
    else:
        model_print(applied, output, namespace=namespace)
