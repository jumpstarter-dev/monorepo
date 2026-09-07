# Distributed Mode

This guide walks you through the process of creating an {term}`exporter` using the
{term}`controller` {term}`service`, configuring drivers, and running the exporter.

```{warning}
The jumpstarter-controller endpoints are secured by TLS. However, in release 0.7.x,
the certificates are self-signed and rotated on every restart. This means the client
will not be able to verify the server certificate. To bypass this, you should use the
`--insecure-tls` flag when creating clients and {term}`exporter`s.
Alternatively, you can configure the ingress/route in reencrypt mode with your own key and certificate.
```

## Prerequisites

Install [the following packages](../../installation/packages.md) in your Python
environment:

- `jumpstarter-cli` - The core Jumpstarter CLI
- `jumpstarter-driver-opendal` - The OpenDAL storage driver
- `jumpstarter-driver-power` - The base power driver

These driver packages include mock implementations, enabling you to test the
connection between an {term}`exporter` and client without physical hardware.

You need the [service](../../../introduction/service.md) running in a Kubernetes
cluster with admin access. For installation instructions, refer to the
[installation guide](../../installation/service/index.md).

## Instructions

### Create an Exporter Configuration

Create an exporter using the controller service API. The `jmp admin` CLI
provides commands to interact with the {term}`controller` directly.

Run this command to create an {term}`exporter` named `example-distributed` and save the
configuration locally:

```console
$ jmp admin create exporter example-distributed --label foo=bar --save --insecure-tls
```

After creating the exporter, find the new exporter config file at
`/etc/jumpstarter/exporters/example-distributed.yaml`. Edit the configuration
using your default text editor with:

```console
$ jmp config exporter edit example-distributed
```

Add the `storage` and `power` drivers under the `export` field in the
configuration file. Your configuration should look like this:

```yaml
apiVersion: jumpstarter.dev/v1alpha1
kind: ExporterConfig
metadata:
  namespace: default
  name: example-distributed
endpoint: "<automatically filled by the controller>"
token: "<automatically filled by the controller>"
export:
  storage:
    type: jumpstarter_driver_opendal.driver.MockStorageMux
  power:
    type: jumpstarter_driver_power.driver.MockPower
```

### Run an Exporter

Start the {term}`exporter` locally using the {term}`jmp` CLI tool:

```console
$ jmp run --exporter example-distributed
```

The {term}`exporter` runs until you terminate the process with or close the shell.

### Create a Client

Create a client to connect to your new {term}`exporter` using the `jmp admin` CLI:

The following command creates a client named "hello", enables unsafe drivers for
development purposes, and saves the configuration locally in
`${HOME}/.config/jumpstarter/clients/`:

```console
$ jmp admin create client hello --save --unsafe --insecure-tls
```

### Inspect Exporters and Leases

List the {term}`exporter`s your client can reach, to find one to select:

```console
$ jmp get exporters
NAME                 LABELS
example-distributed  foo=bar
```

`jmp describe` shows one resource in full, including the labels you select on
and the {term}`lease` holding it, if any:

```console
$ jmp describe exporter example-distributed
Name:       example-distributed
Namespace:  jumpstarter-lab
Labels:
  foo=bar
Online:   yes
Status:   AVAILABLE
Enabled:  yes
Lease:  <none>
```

Describing a {term}`lease` reports its conditions, which is the quickest way to
see why one is still waiting:

```console
$ jmp describe lease 01a05822-e378-71cc-a98c-a216ad4a9432
Name:                  01a05822-e378-71cc-a98c-a216ad4a9432
Namespace:             jumpstarter-lab
Selector:              foo=bar
Exporter:              example-distributed
Client:                hello
Status:                In-Use
Duration:              0:05:00
Effective Begin Time:  2026-08-31 10:04:36 EDT
Effective End Time:    <none>
Tags:  <none>
Context:  <none>
Conditions:
  Type   Status  Reason  Message                                       Last Transition Time
  ----   ------  ------  -------                                       --------------------
  Ready  True    Ready   An exporter has been acquired for the client  2026-08-31 14:04:36 UTC
```

For a lease you hold, add `--drivers` to connect to its exporter and discover
its drivers and runnable `j` commands:

```console
$ jmp describe lease 01a05822-e378-71cc-a98c-a216ad4a9432 --client hello --drivers
$ jmp describe lease 01a05822-e378-71cc-a98c-a216ad4a9432 --client hello --drivers -o json
```

The human-readable description adds **Drivers** and **Commands** tables. With
`-o json` or `-o yaml`, the result is `{lease, driver_tree}`: `lease` contains the
usual lease metadata, while `driver_tree` contains a `drivers` list and the recursive
`cli_tree`, including command help and parameters. This is discovery only; it
does not execute the listed driver commands, create a lease, or release your
existing lease when it finishes. It does require a connection to the exporter
and uses the selected client's driver-access settings. Without `--drivers`,
the existing metadata-only behavior and output shape are unchanged.

Here, **drivers** means the software driver clients exposed by the lease session,
not an inventory of physical devices attached to the exporter. The exporter's
existing device report is unchanged by this command.

Describing a client reads the local configuration rather than the cluster, so it
works without a connection and reports whether the client's token is still valid:

```console
$ jmp describe client hello
Alias:      hello
Path:       /home/user/.config/jumpstarter/clients/hello.yaml
Current:    yes
Name:       hello
Namespace:  jumpstarter-lab
Endpoint:   grpc.jumpstarter.example.com:443
TLS:
  CA:        configured
  Insecure:  yes
Drivers:
  Allow:   <none>
  Unsafe:  yes
Token:
  Expiry:  2027-08-30 02:00:56 UTC
  Status:  valid (8723h 53m remaining)
Refresh Token Stored:  no
```

Add `-o json` or `-o yaml` to any of these for machine-readable output.

### Spawn an Exporter Shell

Interact with your distributed {term}`exporter` using the {term}`exporter shell` functionality
in the {term}`jmp` CLI. When you spawn a shell, the client attempts to acquire a {term}`lease`
on an {term}`exporter`. Once the {term}`lease` is acquired, you can interact with the {term}`exporter`
through your shell {term}`session`.

```console
$ jmp shell --client hello --selector example.com/board=foo
```

### Exiting the Exporter Shell

To terminate the local {term}`exporter`, simply exit the shell:

```console
$ exit
```

## Next Steps

Once you have your {term}`exporter shell` running, you can start using Jumpstarter
commands to interact with your hardware. To learn more about common workflow
patterns and implementation examples, see [Examples](../examples/index.md).
