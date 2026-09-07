# Jumpstarter Driver CLI

## Driver configuration schemas

`jmp driver schema` describes the configuration accepted by drivers installed
in the local Python environment. Editors can use it for exporter YAML
completion and validation without maintaining a hard-coded driver catalog.
It is distinct from inspecting driver clients in a running lease.

```console
jmp driver schema
jmp driver schema -o json
jmp driver schema TcpNetwork -o yaml
jmp driver schema jumpstarter_driver_network.driver.TcpNetwork -o json
```

Names filter by entry-point name or full dotted driver type. An unknown name
fails before any driver is loaded, including when other names match. Output
supports tables (default), JSON, YAML, and `-o name`.

JSON and YAML return a `drivers` list. Each entry contains:

- `name`, `type`, `package`, and `version`: installed driver identity.
- `client`: the client class path used by client-config `drivers.allow` patterns,
  when it can be discovered.
- `description`: the first line of the driver class docstring.
- `properties` and `required`: JSON Schema fragments for the exporter's
  driver-specific `config:` block; common Driver fields and non-constructor
  fields are excluded.
- `defs`: definitions referenced as `#/$defs/...` inside those fragments.
  Consumers combining driver schemas must preserve and namespace these references.
- `error`: an import/schema-generation error, or `null` on success. Schema
  generation failures retain best-effort dataclass field names and required
  keys, without claiming complete type information.

A broken driver does not hide the other results. Inspect each entry's `error`;
exit status zero does not mean every driver produced a complete schema.
An empty environment produces `{"drivers": []}` in JSON/YAML output.

Discovery imports driver packages and invokes their metadata/schema hooks.
It does not instantiate drivers or open a lease itself, but imports/hooks run
arbitrary local Python code: only use it with trusted installed packages.
Python-level discovery output is redirected to stderr to keep structured stdout
parseable. This command is not a sandbox or live hardware validation.
