# JEPs

This directory contains the Jumpstarter Enhancement Proposals - design documents
that describe significant changes to the Jumpstarter project.

## What is a JEP?

A JEP is a design document that proposes a new feature, process change, or
architectural decision for the Jumpstarter hardware-in-the-loop testing
framework. JEPs provide a transparent, structured process for the community to
propose, discuss, and decide on substantial changes.

For the full process definition, see [JEP-0000](JEP-0000-jep-process.md).

## Quick Start

1. Read [JEP-0000](JEP-0000-jep-process.md) to understand when a JEP is needed.
2. Socialize your idea in [Matrix](https://matrix.to/#/#jumpstarter:matrix.org)
   or at the [weekly meeting](https://meet.google.com/gzd-hhbd-hpu).
3. Create a branch and add your JEP markdown file to the `docs/source/contributing/jeps/` directory
   using the [JEP-NNNN-template.md](JEP-NNNN-template.md) as a starting point.
4. Open a pull request. The PR serves as the primary venue for discussion,
   allowing inline review comments on the JEP text.

## JEP Index

### Process JEPs

| JEP  | Title                                  | Status | Author(s)               |
| ---- | -------------------------------------- | ------ | ----------------------- |
| 0000 | [JEP Process](JEP-0000-jep-process.md) | Active | Jumpstarter Maintainers |

### Standards Track JEPs

| JEP  | Title                                                                                                                  | Status      | Author(s)                            |
| ---- | ---------------------------------------------------------------------------------------------------------------------- | ----------- | ------------------------------------ |
| 0010 | [Renode Integration](JEP-0010-renode-integration.md)                                                                   | Implemented | @vtz (Vinicius Zein)                 |
| 0011 | [Protobuf Introspection and Interface Generation](JEP-0011-protobuf-introspection-interface-generation.md)             | Accepted    | @kirkbrauer (Kirk Brauer)            |
| 0013 | [Metrics, Tracing, and Log Observability](JEP-0013-observability-telemetry-logs.md)                                    | Accepted    | @mangelajo (Miguel Angel Ajo Pelayo) |
| 0014 | [Virtual Scalable Exporters](JEP-0014-virtual-scalable-exporters.md)                                                   | Approved    | @mangelajo (Miguel Angel Ajo Pelayo) |
| 0015 | [Multi-Exporter Leases and Inter-Exporter Port Forwarding](JEP-0015-multi-exporter-leases-port-forwarding.md) | Discussion  | @kirkbrauer (Kirk Brauer)            |

### Informational JEPs

| JEP        | Title | Status | Author(s) |
| ---------- | ----- | ------ | --------- |
| *none yet* |       |        |           |

## Status Key

> **Note:** [JEP-0000](JEP-0000-jep-process.md) is the canonical source for
> lifecycle states and their definitions.

| Status       | Meaning                                          |
| ------------ | ------------------------------------------------ |
| Draft        | Author is still writing; not yet open for review |
| Discussion   | PR is open and under community discussion        |
| Accepted     | Design approved; implementation may begin        |
| Implementing | Implementation in progress                       |
| Implemented  | Reference implementation merged                  |
| Final        | Complete and authoritative                       |
| Rejected     | Declined (record preserved)                      |
| Deferred     | Sound but not a current priority                 |
| Withdrawn    | Author voluntarily withdrew                      |
| Active       | Living document, actively maintained (Process JEPs only) |
| Superseded   | Replaced by a newer JEP                          |

```{toctree}
:hidden:

JEP-0000-jep-process.md
JEP-0010-renode-integration.md
JEP-0011-protobuf-introspection-interface-generation.md
JEP-0013-observability-telemetry-logs.md
JEP-0014-virtual-scalable-exporters.md
JEP-0015-multi-exporter-leases-port-forwarding.md
```
