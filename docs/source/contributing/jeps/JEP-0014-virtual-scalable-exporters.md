# JEP-0014: Virtual Scalable Exporters

| Field             | Value                                                          |
| ----------------- | -------------------------------------------------------------- |
| **JEP**           | 0014                                                           |
| **Title**         | Virtual Scalable Exporters                                     |
| **Author(s)**     | @mangelajo (Miguel Angel Ajo Pelayo)                           |
| **Status**        | Approved                                                       |
| **Type**          | Standards Track                                                |
| **Created**       | 2026-06-03                                                     |
| **Updated**       | 2026-07-28                                                     |
| **Discussion**    | https://github.com/jumpstarter-dev/jumpstarter/issues/41       |
| **Requires**      |                                                                |
| **Supersedes**    |                                                                |
| **Superseded-By** |                                                                |

---

## Abstract

This JEP proposes a Virtual Scalable Exporter subsystem for Jumpstarter that
manages pools of virtual targets with configurable autoscaling. Conceptually,
the system scales **virtual targets**; the **Exporter** is the scheduling and
leasing unit (the Pod analog). Each `ExporterSet` declares scaling bounds using
familiar Kubernetes vocabulary (`minReplicas`, `maxReplicas`,
`minAvailableReplicas`); the controller maintains a warm pool of ready exporters
to absorb the 10-60s cold-start latency of VM boot and exporter registration.
This enables low-latency lease acquisition, massive scalability, resource
efficiency, and simplified orchestration of mixed physical/virtual test
topologies — while allowing administrators to tune the trade-off between
responsiveness and resource consumption on a per-target basis.

## Motivation

Jumpstarter currently excels at managing scarce, physical hardware targets.
However, testing and development often require a mix of physical devices and
scalable, virtual resources. Today, virtual targets must be manually deployed
as static exporters with a fixed count — there is no mechanism for the system
to maintain or scale a pool of virtual instances based on demand.

This model has several limitations:

- **Artificial scarcity:** Virtual targets are treated as a fixed-size pool,
  just like physical ones, which defeats their "virtually unlimited" potential.
- **No elasticity:** The pool cannot grow when demand spikes (CI burst) or
  shrink when idle, leading to either queuing or waste.
- **Manual lifecycle:** Administrators must manually deploy, monitor, and scale
  virtual exporter instances — there is no declarative "desired state" for a
  virtual target pool.
- **Cold-start penalty vs. waste trade-off:** Users must choose between
  pre-spawning many instances (wasting resources when idle) or spawning on
  demand (high latency at lease time). There is no middle ground.

The core problem is that virtual targets lack a pool manager that can maintain a
configurable warm pool while autoscaling to meet demand.

### Fidelity / Cost Ladder

One logical target can be served by multiple backends at different fidelity and
cost tiers. Users select via labels through `jmp lease`; the same workflow
applies regardless of backend:

| class (provisioner) | fidelity | scale/cost | role |
| --- | --- | --- | --- |
| container sim (`qemu.jumpstarter.dev`) | low | cheap / CI-scale | functional checks |
| cloud virtual device (`corellium.jumpstarter.dev`) | high | metered | higher-fidelity behavior |
| real hardware (Exporter) | full | scarce | ground truth |

For example, a target that needs GPU or specialized I/O can run functional
checks cheaply on a QEMU class in CI, validate higher-fidelity behavior on a
cloud-backed virtual device, and use real hardware as ground truth. The
The `VirtualTargetClass` abstraction makes this ladder explicit
without changing the lease experience.

### User Stories

- **As a** CI pipeline author, **I want to** lease N virtual targets instantly
  from a warm pool, **so that** my pipeline doesn't block on provisioning
  latency during burst periods.

- **As a** developer, **I want to** lease a virtual target matching a known
  physical board's properties with near-zero wait time, **so that** I can
  iterate quickly without waiting for scarce hardware.

- **As a** platform engineer, **I want to** declare an `ExporterSet` with
  `minAvailableReplicas: 2, maxReplicas: 20`, **so that** there are always warm
  instances ready while the system scales up on demand and scales down when idle.

- **As a** cost-conscious operator, **I want to** set `minAvailableReplicas: 0`
  for rarely-used target types, **so that** they consume no resources until
  actually requested, accepting a cold-start delay.

## Proposal

The proposal introduces **Virtual Scalable Exporters** — a controller-managed
pool of virtual target instances with configurable autoscaling. Rather than
treating virtual targets as purely on-demand or purely static, each
`ExporterSet` declares scaling parameters that let administrators tune the
trade-off between instant availability and resource consumption.

### Resource Hierarchy

Virtual scalable exporters are modeled on familiar Kubernetes workload
primitives:

```text
VirtualTargetClass  ←── referenced by ──  ExporterSet
                                              │
                                              ▼
                                         Exporter ──► Pod
                              (exporter main + target-runtime sidecar)
```

- **`VirtualTargetClass`** — **namespaced** configuration for a backend
  (`provisioner`, nested `parameters`, credentials, scheduling, binding mode).
  Lives in the same namespace as referencing `ExporterSet` resources. Admins own
  classes; `ExporterSet` authors never touch credentials.
- **`ExporterSet`** — namespaced generic scaling resource with `selector` + inline
  `template`. References a `VirtualTargetClass` by name in the **same
  namespace**. Optional nested `parameters` deep-merge over the class defaults.
  One mental model for all backends.
- **`Exporter`** — the minimum leased unit. Exposes drivers that connect to the
  virtual target provisioned from the class.

### Core Concept: ExporterSet with Kubernetes-Native Scaling

`ExporterSet` is a generic CRD (ReplicaSet + HPA analog) with familiar scaling
vocabulary. Provider typing lives in `VirtualTargetClass`, not in the pool CRD
itself.

**Example: VirtualTargetClass (namespaced backend profile)**

```yaml
apiVersion: virtualtarget.jumpstarter.dev/v1alpha1
kind: VirtualTargetClass
metadata:
  name: qemu-rpi4
  namespace: jumpstarter
spec:
  provisioner: qemu.jumpstarter.dev
  bindingMode: Immediate              # warm pool; WaitForFirstConsumer = on-demand
  reclaimPolicy: Delete
  scheduling:                         # inherited by rendered exporter Pods
    nodeSelector:
      kubernetes.io/arch: arm64
    tolerations:
      - key: jumpstarter.dev/kvm
        operator: Exists
        effect: NoSchedule
    resources:
      limits:
        devices.kubevirt.io/kvm: "1"
  images:                            # optional; overrides default container images
    exporter:
      image: quay.io/jumpstarter-dev/jumpstarter:v0.9.0
    runtime:
      image: quay.io/jumpstarter-dev/virtual/qemu-runtime:v0.9.0
  parameters:                        # nested object; provisioner interprets
    machineType: virt
    firmware:
      url: registry.example.com/firmware/rpi4:latest
      digest: sha256:abc...
    resources:
      cpu: 4
      memory: 4Gi
    storage:
      size: 16Gi
      fsOverhead: "10%"
    # storage:
    #   storageClassName: gp3        # omit → sized emptyDir at /disk
```

```yaml
apiVersion: virtualtarget.jumpstarter.dev/v1alpha1
kind: ExporterSet
metadata:
  name: rpi4-virtual
  namespace: jumpstarter
spec:
  minReplicas: 0
  maxReplicas: 20
  minAvailableReplicas: 2            # PDB-style warm buffer (ready & unleased)
  scaleDownCooldown: 5m
  recycleStrategy: ExitAndReplace    # or InPlaceReuse
  virtualTargetClassName: qemu-rpi4  # same-namespace VirtualTargetClass name
  parameters:                        # optional; deep-merged over class parameters
    resources:
      memory: 8Gi                    # override only memory; cpu inherited
  selector:
    matchLabels:
      board: rpi4
  template:                          # embedded template (Deployment idiom)
    metadata:
      labels:
        board: rpi4
        arch: aarch64
        virtual: "true"
    spec:
      drivers:
        - name: qemu
          type: jumpstarter_driver_qemu.driver.Qemu
        - name: tcp
          type: jumpstarter_driver_network.driver.TcpNetwork
          config:
            port: 22
        - name: serial
          type: jumpstarter_driver_serial.driver.QemuSerial
status:
  replicas: 5
  readyReplicas: 3
  availableReplicas: 1             # warm (ready & unleased)
  leasedReplicas: 2
# scale subresource: specReplicasPath=.spec.maxReplicas
```

**Example: Corellium VirtualTargetClass**

```yaml
apiVersion: virtualtarget.jumpstarter.dev/v1alpha1
kind: VirtualTargetClass
metadata:
  name: corellium-kronos
  namespace: jumpstarter
spec:
  provisioner: corellium.jumpstarter.dev
  credentialsSecretRef:
    name: corellium-creds              # Secret in same namespace
  bindingMode: WaitForFirstConsumer  # provision on lease
  reclaimPolicy: Delete
  parameters:
    api:
      host: app.corellium.com
      projectId: "778f00af-5e9b-40e6-8e7f-c4f14b632e9c"
    device:
      flavor: kronos
      os: "1.1.1"
      build: "Critical Application Monitor (Baremetal)"
```

The Corellium driver (`jumpstarter_driver_corellium.driver.Corellium`) manages
the full virtual instance lifecycle through the Corellium REST API — it creates
instances on power-on and destroys them on power-off. Device parameters live in
`VirtualTargetClass.spec.parameters` and may be overridden per pool via
`ExporterSet.spec.parameters` (deep-merged). The provisioner injects API
credentials from `VirtualTargetClass.credentialsSecretRef` into the exporter
Pod; `ExporterSet` authors never see credentials.

**Example: Android ExporterSet**

```yaml
apiVersion: virtualtarget.jumpstarter.dev/v1alpha1
kind: ExporterSet
metadata:
  name: pixel7-emulator
  namespace: jumpstarter
spec:
  minReplicas: 0
  maxReplicas: 10
  minAvailableReplicas: 0            # fully on-demand
  virtualTargetClassName: android-pixel7
  selector:
    matchLabels:
      device: pixel7
  template:
    metadata:
      labels:
        device: pixel7
        os: android
        api-level: "34"
        virtual: "true"
    spec:
      drivers:
        - name: adb
          type: jumpstarter_driver_android.driver.AdbDriver
        - name: power
          type: jumpstarter_driver_power.driver.EmulatorPower
```

An `ExporterSet` with `minAvailableReplicas: 0` consumes no resources until a
lease is requested, accepting cold-start latency. An `ExporterSet` with
`minAvailableReplicas: 3` always has 3 ready-to-lease exporters — leases are
fulfilled instantly from the warm pool, and the controller scales up if more are
needed.

### Container-Backed Targets: Sidecar Pattern

For container-backed provisioners (`qemu.jumpstarter.dev`, Android emulator, etc.),
the provisioner renders each instance Pod from independently shipped artifacts.
The sketch below uses **native sidecar init containers** (`restartPolicy: Always`,
[KEP-753](https://github.com/kubernetes/enhancements/issues/753)) as the
**proposed** co-location model — **init containers vs. lifecycle hooks** is
unresolved; see *Unresolved Questions*.

```yaml
# rendered by qemu.jumpstarter.dev provisioner
spec:
  restartPolicy: Never               # exporter exit completes the Pod (ExitAndReplace)
  initContainers:
    - name: copy-jumpstarter-exec    # one-shot: stage binary onto shared volume
      image: quay.io/jumpstarter-dev/jumpstarter:latest
      volumeMounts:
        - name: shared
          mountPath: /shared
    - name: target-runtime           # native sidecar (starts before exporter)
      restartPolicy: Always
      image: quay.io/jumpstarter-dev/virtual/qemu-runtime:latest
      volumeMounts:
        - name: shared
          mountPath: /shared
        - name: disk
          mountPath: /disk
  containers:
    - name: exporter                 # main — default logs; exit tears Pod down
      image: quay.io/jumpstarter-dev/jumpstarter:latest
      volumeMounts:
        - name: shared
          mountPath: /shared
        - name: disk
          mountPath: /disk
  volumes:
    - name: shared
      emptyDir:
        sizeLimit: 100Mi             # sockets / cidata / jumpstarter-exec
    - name: disk
      emptyDir:
        sizeLimit: 16Gi              # guest disk; or ephemeral volumeClaimTemplate
```

Benefits:

- **Independent release cadence** — exporter, runtime, and OS image version
  independently.
- **Natural teardown** — exporter is the main container, so ExitAndReplace is
  simply process exit; Kubernetes terminates the runtime sidecar automatically.
- **Default logs** — `kubectl logs` shows the exporter without `-c`.
- **Socket ready first** — runtime sidecar starts before the exporter so
  `launcher.sock` exists when `jmp run` begins.
- **Standard interfaces** — drivers attach over virtio (serial/SPI/CAN/GPIO) or
  Unix sockets on shared volumes; same driver code works physical + virtual.
- **Unprivileged Pods** — virtio-backed guests avoid privileged containers when
  the host supports it.

The exporter (main container) communicates with the target-runtime sidecar via
Unix sockets on a shared `emptyDir` volume (QMP for QEMU control, serial console,
launcher socket for dynamic argv). API-backed provisioners (`corellium`, `ec2`)
and off-cluster provisioners (`qemu-baremetal.jumpstarter.dev`) skip the
in-cluster runtime container — see *External and Off-Cluster Provisioning*.

### User Experience

From the user's perspective, virtual scalable exporters appear as regular
exporters in the pool. The lease experience is unchanged:

```bash
# Lease any rpi4 target — may match physical or virtual
jmp lease -l board=rpi4

# Lease explicitly virtual targets
jmp lease -l board=rpi4,virtual=true

# Prefer ground truth when fidelity matters
jmp lease -l board=rpi4,fidelity=full
```

The guiding principle is: **"Get me a target that matches my requirements."** The
distinction between physical and virtual is an implementation detail, not a
primary concern for the user. Virtual exporters simply appear in the same pool
as physical ones, differentiated only by labels.

### End-to-End Flow (QEMU Example)

This section walks through a complete **in-cluster QEMU warm-pool** scenario:
what each actor does, which CRDs are involved, and how control passes between
components. The flow uses only **two admin-configured CRDs** — no per-instance
claim resources:

| Admin CRD | Role in this flow |
| --- | --- |
| `VirtualTargetClass` | Backend profile: provisioner, scheduling, nested `parameters` |
| `ExporterSet` | Pool scaling, labels, drivers, optional parameter overrides |

Everything else (`Exporter`, `Lease`, `Pod`) is created and managed by
controllers at runtime. Relationships use a **reference graph** (not a strict
ownership tree):

```text
VirtualTargetClass  ←── referenced by ──  ExporterSet
                                              │
                                              ▼
                                         Exporter ──► Pod
                              (exporter main + QEMU runtime sidecar)
```

Homogeneous QEMU pools configure **`VirtualTargetClass` + `ExporterSet` only**.
The provisioner deep-merges parameters, materializes Pods, and registers
`Exporter` CRs. **OS images are not pre-selected by the pool** — lessees flash
and boot what they need after leasing (see Phase 4 and DD-7).

#### Actors

| Actor | Component | Responsibility |
| --- | --- | --- |
| **Administrator** | Human / GitOps | Cluster bootstrap, class + set CRs |
| **Jumpstarter operator** | `Jumpstarter` CR | Deploys `jumpstarter-controller`, routers, exporter-set controllers |
| **Exporter-set controller** | `qemu.jumpstarter.dev` Deployment | Reconciles `ExporterSet`, creates Exporters/Pods, scales pool |
| **Jumpstarter controller** | Existing controller | Assigns `Lease` → `Exporter`, unchanged lease semantics |
| **User** | CLI / CI (`jmp lease`, drivers) | Requests leases, flashes images, runs tests |

#### Phase 0 — Cluster bootstrap (admin, one-time)

**Admin actions:**

1. Install Jumpstarter operator (if not already present).
2. Configure the `Jumpstarter` CR with `spec.exporterSets.provisioners` listing
   `qemu.jumpstarter.dev` (and any other provisioners).

**Controller actions:**

- Operator creates the exporter-set controller Deployment
  (`--provisioner=qemu.jumpstarter.dev`).
- Operator ensures `jumpstarter-controller` is running (existing behavior).

**Result:** Provisioner controller is watching for `ExporterSet` CRs whose
`virtualTargetClassName` references a class handled by that provisioner.

#### Phase 1 — Define the virtual target profile (admin, two CRs)

**Admin actions:**

1. Create a `VirtualTargetClass` describing the QEMU backend (same namespace as
   the `ExporterSet` that will reference it):

```yaml
apiVersion: virtualtarget.jumpstarter.dev/v1alpha1
kind: VirtualTargetClass
metadata:
  name: qemu-rpi4
  namespace: jumpstarter
spec:
  provisioner: qemu.jumpstarter.dev
  bindingMode: Immediate
  reclaimPolicy: Delete
  scheduling:
    nodeSelector:
      kubernetes.io/arch: arm64
    tolerations:
      - key: jumpstarter.dev/kvm
        operator: Exists
        effect: NoSchedule
    resources:
      limits:
        devices.kubevirt.io/kvm: "1"
  parameters:
    machineType: virt
    firmware:
      url: registry.example.com/firmware/rpi4:latest
      digest: sha256:abc...
    resources:
      cpu: 4
      memory: 4Gi
    storage:
      size: 16Gi
      fsOverhead: "10%"
```

2. Create an `ExporterSet` in the **same namespace** that references the class
   by name and declares scaling + lease-matching labels:

```yaml
apiVersion: virtualtarget.jumpstarter.dev/v1alpha1
kind: ExporterSet
metadata:
  name: rpi4-virtual
  namespace: jumpstarter
spec:
  minReplicas: 0
  maxReplicas: 20
  minAvailableReplicas: 2
  scaleDownCooldown: 5m
  recycleStrategy: ExitAndReplace
  virtualTargetClassName: qemu-rpi4
  parameters:
    resources:
      memory: 8Gi
  selector:
    matchLabels:
      board: rpi4
  template:
    metadata:
      labels:
        board: rpi4
        arch: aarch64
        virtual: "true"
    spec:
      drivers:
        - name: qemu
          type: jumpstarter_driver_qemu.driver.Qemu
        - name: tcp
          type: jumpstarter_driver_network.driver.TcpNetwork
          config:
            port: 22
        - name: serial
          type: jumpstarter_driver_serial.driver.QemuSerial
```

**User actions:** None.

**Controller actions:** None yet — exporter-set controller waits until
`ExporterSet` exists and resolves `virtualTargetClassName` to the class above.

#### Phase 2 — Warm pool provisioning (exporter-set controller)

**Trigger:** `ExporterSet` CR created or updated; `minAvailableReplicas: 2`.

**Exporter-set controller actions (reconcile loop):**

1. Resolve `ExporterSet.spec.virtualTargetClassName` to `VirtualTargetClass`
   `qemu-rpi4` in the same namespace; compute merged parameters (deep-merge of
   class + set overrides).
2. Count owned `Exporter` CRs: `replicas`, `readyReplicas`, `leasedReplicas`,
   `availableReplicas` (= ready − leased).
3. If `availableReplicas < minAvailableReplicas` and `replicas < maxReplicas`,
   scale up by creating new instances. For each new instance:
   - Create an `Exporter` CR with labels from `spec.template.metadata` and
     drivers from `spec.template.spec`.
   - Render a Kubernetes Pod (sidecar pattern):
     - **QEMU runtime** (native sidecar, `restartPolicy: Always`) — starts
       first so `launcher.sock` is ready; baseline virt machine from merged
       `parameters` (CPU, memory, firmware blob); **empty disk** ready for
       user flash at lease time.
     - **Exporter** (main container) — registers with `jumpstarter-controller`;
       default logs; exit drives ExitAndReplace Pod completion.
     - Exporter talks to runtime via Unix sockets on a shared `emptyDir` (QMP,
       serial, launcher).
   - Apply scheduling from `VirtualTargetClass.scheduling` to the Pod.
4. Update `ExporterSet.status` (`replicas`, `readyReplicas`, `availableReplicas`,
   `leasedReplicas`, conditions).

**Jumpstarter-controller actions:**

- Accepts exporter registrations from the sidecar processes (existing gRPC flow).
- Marks exporters as available for lease assignment when ready.

**User actions:** None.

**Result:** Two warm exporters appear in the pool, labeled `board=rpi4,
virtual=true`. `ExporterSet.status.availableReplicas: 2`.

```text
ExporterSet rpi4-virtual
├── Exporter rpi4-virtual-aaa   [Ready, unleased]  →  Pod (exporter + QEMU)
└── Exporter rpi4-virtual-bbb   [Ready, unleased]  →  Pod (exporter + QEMU)
```

#### Phase 3 — User requests a lease (user + jumpstarter-controller)

**User actions:**

```bash
jmp lease -l board=rpi4,virtual=true
```

**Jumpstarter-controller actions:**

1. Create a `Lease` CR with `spec.selector.matchLabels: {board: rpi4, virtual: "true"}`.
2. Scan available `Exporter` CRs matching the selector (enabled, no active
   `leaseRef`, ready).
3. Pick one (e.g. `rpi4-virtual-aaa`) and set `Exporter.status.leaseRef` to the
   lease name.
4. Return connection details to the user (existing flow).

**Exporter-set controller actions:**

- Observes `leasedReplicas` increased, `availableReplicas` decreased.
- If `availableReplicas < minAvailableReplicas`, begins scale-up (create another
  instance to refill the warm buffer).
- Does **not** participate in lease assignment.

**Result:** User holds an active lease on `rpi4-virtual-aaa`. Pool still
maintains warm capacity via background scale-up.

#### Phase 4 — User session: flash, boot, test (user + exporter)

The warm pool provides **instant lease assignment**; image selection happens
**after** lease — same workflow as a physical bench (DD-7). The pool does not
pre-flash an OS onto instances.

**User actions** (via leased client):

```python
with env() as client:
    client.storage.flash("/path/to/image.raw")   # write disk image
    client.power.on()                             # boot QEMU via QemuPower driver
    client.serial.read()                          # interact over serial
    # ... run tests ...
```

**Exporter actions:**

- `storage.flash` writes the image to `/disk` on the guest-disk volume (or tells
  QEMU runtime via QMP/`blockdev-add`).
- `power.on` sends QEMU start via QMP or launcher socket on shared volume.
- Serial/network drivers proxy to the QEMU runtime sidecar.

**Controller actions:** None during the session (lease is held).

#### Phase 5 — Lease release and recycle (user + controllers)

**User actions:**

```bash
jmp delete-lease <lease-id>    # or lease TTL expires
```

**Jumpstarter-controller actions:**

1. Clear `Exporter.status.leaseRef` on `rpi4-virtual-aaa`.
2. Mark lease as released.

**Exporter-set controller actions:**

1. Observe exporter is unleased; update `availableReplicas` / `leasedReplicas`.
2. Apply `recycleStrategy`:
   - **ExitAndReplace (default):** exporter (main) exits after cleanup → Pod
     reaches `Succeeded`/`Failed` → controller deletes the unleased `Exporter`
     CR (OwnerReference cascade removes the Completed Pod) → warm-buffer
     scale-up creates a fresh replacement with empty baseline storage to
     maintain `minAvailableReplicas` (next lessee flashes again). With
     `maxReplicas` tight, delete must happen before create.
   - **InPlaceReuse:** exporter resets QEMU state in place → same Pod returns
     to Ready without restart (lessee may re-flash before next session).
3. If `availableReplicas > minAvailableReplicas` for longer than
   `scaleDownCooldown`, gracefully scale down an excess replica:
   - Set `Exporter.spec.enabled: false`
   - Wait until no lease assigned
   - Delete Pod + `Exporter` CR

**Result:** Pool returns to steady state with `minAvailableReplicas` warm,
unleased exporters.

#### Phase 6 — Demand spike (scale-up under load)

**Trigger:** Three users (or CI jobs) request leases simultaneously; only one
warm exporter remains.

**User actions:** Three concurrent `jmp lease -l board=rpi4,virtual=true`.

**Jumpstarter-controller actions:**

- Assigns the one available exporter immediately.
- Sets `Pending` condition on the other two leases (existing behavior when no
  exporter is available).

**Exporter-set controller actions:**

1. Sees pending leases matching `spec.selector` with no available exporters.
2. Scales up: creates new `Exporter` + Pod instances (up to `maxReplicas`).
3. As new exporters register and become ready, jumpstarter-controller assigns
   pending leases.

**Result:** Pool grows to meet demand, then shrinks back after cooldown when
leases are released.

#### Summary: CRDs and runtime objects

**Admin-configured (2 CRDs — the full pool definition):**

| CRD | Scope | Created by | Observed by | Relationship |
| --- | --- | --- | --- | --- |
| `VirtualTargetClass` | Namespaced | Admin | Exporter-set controller | Referenced by `ExporterSet` (same namespace) |
| `ExporterSet` | Namespaced | Admin | Exporter-set controller | References class; owns runtime objects below |

**Platform and runtime (created by controllers):**

| Resource | Created by | Observed by | User-visible? |
| --- | --- | --- | --- |
| `Jumpstarter` | Admin | Operator | No |
| `Exporter` | Exporter-set controller | Jumpstarter-controller, exporter-set controller | Indirectly (via lease) |
| `Lease` | User (via CLI) | Jumpstarter-controller, exporter-set controller | Yes |
| `Pod` | Exporter-set controller | Kubernetes, exporter-set controller | No |

#### QEMU vs API-backed vs off-cluster backends

The flow above applies to **in-cluster container-backed** provisioners
(`qemu.jumpstarter.dev`). Other provisioner strings reuse the same
`ExporterSet` + `jumpstarter-controller` lease flow with different placement:

| Topology | Example provisioner | Where the target runs |
| --- | --- | --- |
| In-cluster container | `qemu.jumpstarter.dev` | Pod on Kubernetes (sidecar + runtime) |
| API-backed cloud | `corellium.jumpstarter.dev` | External SaaS API; lightweight exporter Pod |
| Off-cluster bare metal | `qemu-baremetal.jumpstarter.dev` | QEMU/emulator on lab hosts outside the cluster |

For **API-backed** backends:

- `VirtualTargetClass` holds `credentialsSecretRef` and shared backend
  `parameters`.
- Per-pool overrides are expressed via `ExporterSet.spec.parameters`
  (deep-merged over the class).
- The exporter Pod is lighter (API client only; no QEMU runtime container).

For **off-cluster** backends, see *External and Off-Cluster Provisioning*.

The `ExporterSet` + `jumpstarter-controller` lease flow is identical for all
topologies.

### Architecture Overview

```text
                        ┌─────────────────────────┐
                        │  jumpstarter-controller │
                        │  (creates Leases,       │
                        │   assigns Exporters)    │
                        └──────────┬──────────────┘
                                   │
                      creates/updates Lease & Exporter objects
                                   │
                                   ▼
                  ┌────────────────────────────────────┐
                  │          Kubernetes API            │
                  │  (Lease, Exporter, ExporterSet,   │
                  │   VirtualTargetClass)              │
                  └─┬──────────────┬──────────────┬────┘
                    │              │              │
         watches    │   watches    │   watches    │
      Leases +      │  Leases +    │  Leases +    │
      Exporters     │  Exporters   │  Exporters   │
                    │              │              │
  ┌─────────────────▼┐ ┌───────────▼──────────┐┌──▼──────────────────────┐
  │ qemu provisioner │ │ android provisioner  │ │ corellium provisioner   │
  │ (ExporterSet     │ │ (ExporterSet         │ │ (ExporterSet            │
  │  controller)     │ │  controller)         │ │  controller)            │
  └────────┬─────────┘ └──────────┬──────────┘ └────────────┬────────────┘
           │                      │                         │
           │ manages              │ manages                 │ manages
           ▼                      ▼                         ▼
  ┌──────────────────┐ ┌───────────────────────┐ ┌────────────────────────┐
  │ Warm Pool        │ │ Warm Pool             │ │ Warm Pool              │
  │ [Exporter]..     │ │ [Exporter]..          │ │ [Exporter]..           │
  └────────┬─────────┘ └───────────┬───────────┘ └────────────┬───────────┘
           │                       │                          │
           └───────────────────────┼──────────────────────────┘
                                   │ register as standard Exporter CRs
                                   ▼
                          Kubernetes API (Exporters)
```

**Scaling Inputs — Watches on Leases and Exporters:**

Each `ExporterSet` controller watches two key resources to make scaling decisions:

1. **Leases** — The controller watches for pending Leases whose label selectors
   match the set's selector. Pending leases with no available exporter signal
   demand and trigger scale-up.
2. **Exporters** — The controller watches owned Exporter objects to track which
   instances are available (no active lease) vs. occupied (leased). This
   determines the current pool utilization.

Together these inputs feed the scaling logic: if there are pending leases that
match this set and no available instances to serve them, scale up. If there are
excess idle instances beyond `minAvailableReplicas` for a sustained period, scale
down.

**Per-Provisioner Deployments (single image by default):** All provisioner
controllers are compiled into a single binary. Each Deployment in the cluster
passes a `--provisioner=<name>` flag to activate the corresponding reconciler
(e.g., `qemu.jumpstarter.dev`). This gives each provisioner isolated logs and
independent restarts while maintaining a single image to build and release.

The Jumpstarter operator deploys provisioner controllers based on the
`Jumpstarter` CR configuration. A new `exporterSets` section lists which
provisioners to enable:

```yaml
apiVersion: operator.jumpstarter.dev/v1alpha1
kind: Jumpstarter
metadata:
  name: jumpstarter
  namespace: jumpstarter
spec:
  # ... existing controller, routers, authentication config ...
  
  exporterSets:
    image: quay.io/jumpstarter-dev/exporter-set-controller:latest
    imagePullPolicy: IfNotPresent
    provisioners:
      - name: qemu.jumpstarter.dev
        enabled: true
      - name: corellium.jumpstarter.dev
        enabled: false
        image: quay.io/jumpstarter-dev/exporter-set-controller-corellium:latest
```

**Scaling Logic:** Each `ExporterSet` controller monitors its instances and scales
based on available (unleased) replicas:

- If `availableReplicas` drops below `minAvailableReplicas`, scale up.
- If `availableReplicas` exceeds demand for a cooldown period, scale down (never
  below `minAvailableReplicas`).
- Never exceed `maxReplicas` (if set; 0 or omitted means no upper bound).
- `kubectl scale exporterset/<name> --replicas=N` works via the `scale`
  subresource (`specReplicasPath=.spec.maxReplicas`).

**Instance Lifecycle:**

1. `ExporterSet` controller creates an `Exporter` from the set template
   (provisioner renders the Pod).
2. The Pod starts the virtual target (sidecar pattern for container backends, or
   API call for external backends) and runs the Jumpstarter exporter, registering
   with the controller like any other exporter.
3. The instance becomes available in the pool for lease assignment.
4. When a lease is released, the exporter handles cleanup/reset per
   `recycleStrategy`. The instance returns to the available pool or is replaced.

### API / Protocol Changes

**New CRDs**

| CRD | Scope | Role |
| --- | --- | --- |
| `VirtualTargetClass` | Namespaced | Backend profile — provisioner, credentials, scheduling, binding, nested `parameters` |
| `ExporterSet` | Namespaced | Generic scaling resource (ReplicaSet + HPA analog) |

**Reference rule:** `ExporterSet.spec.virtualTargetClassName` must name a
`VirtualTargetClass` in the **same namespace**. Cross-namespace references are
rejected at admission. `credentialsSecretRef.name` must refer to a Secret in that
same namespace.

**VirtualTargetClass (common fields):**

```yaml
spec:
  provisioner: <string>              # e.g. qemu.jumpstarter.dev
  credentialsSecretRef:              # optional; for API-backed provisioners
    name: <string>                   # Secret in same namespace as this class
  parameters:                        # nested YAML object; provisioner-specific
    <key>: <nested value>
  bindingMode: Immediate | WaitForFirstConsumer
  reclaimPolicy: Delete | Retain
  scheduling:                        # inherited by rendered exporter Pods
    nodeSelector:
      <key>: <value>
    nodeAffinity: { ... }
    tolerations: [ ... ]
    resources:
      limits:
        devices.kubevirt.io/kvm: "1"
  images:                            # optional; overrides default container images
    exporter:                        # exporter (main container) image
      image: <string>               # e.g. quay.io/jumpstarter-dev/jumpstarter:v0.9.0
      imagePullPolicy: <Always|Never|IfNotPresent>
    runtime:                         # provisioner-specific runtime sidecar image
      image: <string>               # e.g. quay.io/jumpstarter-dev/virtual/qemu-runtime:v0.9.0
      imagePullPolicy: <Always|Never|IfNotPresent>
```

**ExporterSet (common fields):**

```yaml
spec:
  minReplicas: <int>                 # floor (default: 0)
  maxReplicas: <int>                 # ceiling (0 or omitted = no limit)
  minAvailableReplicas: <int>        # warm buffer: ready & unleased (default: 0)
  scaleDownCooldown: <duration>      # default: 5m
  recycleStrategy: ExitAndReplace | InPlaceReuse
  virtualTargetClassName: <string>   # VirtualTargetClass name in same namespace
  parameters:                       # optional nested overrides (deep-merged with class)
    <key>: <nested value>
  images:                           # optional; overrides VTC-level image defaults
    exporter:
      image: <string>
      imagePullPolicy: <Always|Never|IfNotPresent>
    runtime:
      image: <string>
      imagePullPolicy: <Always|Never|IfNotPresent>
  selector:
    matchLabels:
      <key>: <value>
  template:
    metadata:
      labels: { ... }
    spec:
      drivers:
        - name: <string>             # required; key in ExporterConfig export map
          type: <string>             # fully qualified Python driver class
          config: { ... }            # driver-specific config (schemaless)
```

### Image Overrides

Both `VirtualTargetClass` and `ExporterSet` expose an `spec.images` field with
typed sub-fields for overriding the default container images used by the
provisioner:

- **`images.exporter`** — the exporter (main container) image.
- **`images.runtime`** — the provisioner-specific runtime sidecar image
  (e.g. QEMU runtime for `qemu.jumpstarter.dev`).

Each sub-field is an `ImageSpec` with:

- **`image`** — container image reference (e.g. `quay.io/org/repo:tag`).
- **`imagePullPolicy`** — `Always`, `Never`, or `IfNotPresent`.

**Merge semantics:** ExporterSet-level images fully override VTC-level images
at the `ImageSpec` level (not individual fields within an `ImageSpec`). If an
ExporterSet specifies `images.runtime`, it replaces the entire VTC-level
`images.runtime`; VTC-level `images.exporter` is still inherited if the
ExporterSet does not specify one. When neither VTC nor ExporterSet specifies
an image, the provisioner falls back to its built-in default image, resolved
to the controller's build-time version.

**Use cases:**

- **Development clusters:** Override images to `:latest` for local testing.
- **Air-gapped environments:** Point to an internal registry mirror.
- **Version pinning:** Pin specific versions independent of the controller release.

### Dictionary-Based Parameters

Both `VirtualTargetClass` and `ExporterSet` expose a `spec.parameters` field
carrying provisioner-specific configuration as a **nested YAML object** (maps,
lists, and scalars) — not a flat `map[string]string`. This reads like normal
exporter/driver config rather than CSI's intentionally opaque string map.

**CRD representation:** The field is schemaless at the API level
(`type: object` with `x-kubernetes-preserve-unknown-fields: true`, or
`apiextensionsv1.JSON` in Go). OpenAPI does not validate nested structure at
`kubectl apply` time.

**Validation:** The active provisioner validates merged parameters during
reconcile and sets `ExporterSet` status conditions on error. Optional future:
`VirtualTargetClass.spec.parametersSchemaRef` pointing to a JSON Schema
ConfigMap per provisioner.

**Merge semantics:** When provisioning an instance, the controller computes:

```text
mergedParameters = deepMerge(VirtualTargetClass.spec.parameters,
                             ExporterSet.spec.parameters)
```

- **Maps** merge recursively — set keys override class keys at the same path.
- **Scalars and lists** in `ExporterSet.spec.parameters` replace the class
  value at that path entirely (lists are not concatenated).

**Example:**

```yaml
# VirtualTargetClass.spec.parameters
resources:
  cpu: 4
  memory: 4Gi
storage:
  size: 16Gi
  fsOverhead: "10%"
firmware:
  url: registry.example.com/firmware/rpi4:v1
  digest: sha256:abc...

# ExporterSet.spec.parameters (override memory only)
resources:
  memory: 8Gi

# mergedParameters passed to provisioner
resources:
  cpu: 4              # inherited from class
  memory: 8Gi         # overridden by set
storage:              # unchanged — set did not specify storage
  size: 16Gi
  fsOverhead: "10%"
firmware:             # unchanged — set did not specify firmware
  url: registry.example.com/firmware/rpi4:v1
  digest: sha256:abc...
```

**Guest disk (`qemu.jumpstarter.dev`):** `parameters.storage.size` is the
guest disk size (also mapped to the QEMU driver's `disk_size`). Optional
`parameters.storage.fsOverhead` (default `"10%"`, use `"0%"` to disable) inflates
the Kubernetes volume request to leave filesystem headroom for flashing.
`parameters.storage` also selects the Kubernetes volume backend:

```yaml
parameters:
  storage:
    size: 16Gi
    fsOverhead: "10%"
    storageClassName: gp3          # omit or "" → sized emptyDir
    accessModes: ["ReadWriteOnce"] # default ReadWriteOnce
```

When `storageClassName` is set, the provisioner attaches a [generic ephemeral
volume](https://kubernetes.io/docs/concepts/storage/ephemeral-volumes/#generic-ephemeral-volumes)
(`volumeClaimTemplate`) at `/disk` so the claim is created and deleted with the
Pod (ExitAndReplace). When it is omitted, `/disk` is a sized `emptyDir` and
the provisioner sets `ephemeral-storage` requests/limits on the runtime sidecar
for scheduler accounting. Unix sockets stay on the separate 100Mi `/shared` volume.
ExporterSet `parameters.storage` deep-merges over the class; an empty
`storageClassName` on the set forces emptyDir.

**Status subresource (ExporterSet):**

```yaml
status:
  replicas: 5
  readyReplicas: 3
  availableReplicas: 1               # warm (ready & unleased)
  leasedReplicas: 2
  conditions:
    - type: SetHealthy
      status: "True"
    - type: ScalingLimited
      status: "False"
```

**Scale subresource:** `specReplicasPath=.spec.maxReplicas` enables
`kubectl scale` and HPA/KEDA interoperability.

**Pluggable provisioners:**

```text
VirtualTargetClass.provisioner →
  qemu.jumpstarter.dev           →  k8s Pod (sidecar + runtime container)
  qemu-baremetal.jumpstarter.dev →  QEMU on off-cluster lab hosts (SSH/API)
  ec2.jumpstarter.dev            →  AWS API
  corellium.jumpstarter.dev      →  Corellium REST API
# backend is pluggable via provisioner string
```

**Changes to existing CRDs:**

**Exporter — new `enabled` field:**

Exporters gain an `enabled` boolean field (default: `true`). When set to
`false`, the `jumpstarter-controller` will not assign new leases to this
exporter. This is useful for:

- **Lab operations:** Temporarily taking a physical exporter offline for
  maintenance without deleting it.
- **Graceful scale-down:** `ExporterSet` controllers set `enabled: false` before
  terminating an instance, ensuring the controller doesn't race to assign a
  lease to an exporter that is about to be deleted.

```yaml
apiVersion: jumpstarter.dev/v1alpha1
kind: Exporter
metadata:
  name: qemu-rpi4-instance-3
spec:
  enabled: false  # Controller will not assign new leases to this exporter
```

The graceful scale-down sequence becomes:

1. `ExporterSet` controller sets `enabled: false` on the target exporter.
2. Controller waits to confirm no lease was assigned (watches for
   `status.leaseRef` to remain empty).
3. Controller deletes the Pod and Exporter CR.

### Hardware Considerations

This proposal is specifically designed to reduce reliance on physical hardware
for scalable testing. However:

- Virtual targets must faithfully emulate the interfaces exposed by physical
  hardware (serial, network, storage, power) through the existing driver model.
- Container-backed provisioners require `/dev/kvm` or equivalent; scheduling is
  expressed on `VirtualTargetClass.scheduling`.
- Timing-sensitive tests (USB/IP latency, boot ROM timeouts) may behave
  differently on virtual targets — the system should expose labels indicating
  whether a target is physical or virtual so users can filter when fidelity
  matters.

### External and Off-Cluster Provisioning

Provisioners are **not** limited to in-cluster Pods. The same
`VirtualTargetClass` + `ExporterSet` model applies whether the virtual target
runs as a Kubernetes Pod, on a cloud virtual-device API, or on **bare-metal lab
hosts** outside the cluster. `VirtualTargetClass.provisioner` selects the
backend implementation; `credentialsSecretRef` and nested `parameters` carry
everything the provisioner needs to reach remote infrastructure (API tokens,
SSH keys, host lists, board profiles).

**Design intent:** Scale a **logical pool** of exporters through familiar
`ExporterSet` semantics while placing workloads where fidelity or hardware
requires it — e.g. a high-fidelity automotive emulator that needs bare-metal
KVM, GPU passthrough, or vendor-specific tooling unavailable in the cluster.

**What stays the same:**

- Users lease with labels (`jmp lease -l board=sa8295,fidelity=high`) — no
  awareness of placement.
- Each pool member registers as a standard `Exporter` CR with
  `jumpstarter-controller`.
- Lessees flash and boot images via existing drivers after lease (see DD-7).

**What differs per provisioner:**

- **In-cluster (`qemu.jumpstarter.dev`):** exporter-set controller creates Pod +
  sidecar; scheduling from `VirtualTargetClass.scheduling`.
- **API-backed (`corellium.jumpstarter.dev`):** exporter Pod is a thin API
  client; cloud device lifecycle managed externally.
- **Off-cluster (`qemu-baremetal.jumpstarter.dev`):** exporter-set controller
  provisions exporter + QEMU (or vendor emulator) on remote hosts via SSH or a
  lab agent API; may run exporter as a local process on the host rather than a
  Pod. The controller still owns `Exporter` CRs in the cluster for lease
  assignment.

**Automotive example — Qualcomm reference board on bare metal:**

An automotive team runs SA8295-class targets on dedicated lab servers for
higher-fidelity behavior than in-cluster QEMU. The cluster hosts
orchestration only; emulators run on the bench network.

```yaml
apiVersion: virtualtarget.jumpstarter.dev/v1alpha1
kind: VirtualTargetClass
metadata:
  name: qcom-sa8295-baremetal
  namespace: jumpstarter
spec:
  provisioner: qemu-baremetal.jumpstarter.dev
  credentialsSecretRef:
    name: automotive-lab-ssh
  bindingMode: Immediate
  parameters:
    hosts:
      - name: bench-01.automotive.example.com
        arch: aarch64
        slots: 2                       # concurrent instances per host
      - name: bench-02.automotive.example.com
        arch: aarch64
        slots: 2
    runtime:
      binary: /usr/bin/qemu-system-aarch64
      kvm: true
    board:
      soc: sa8295
---
apiVersion: virtualtarget.jumpstarter.dev/v1alpha1
kind: ExporterSet
metadata:
  name: qcom-sa8295-hifi
  namespace: jumpstarter
spec:
  minReplicas: 0
  maxReplicas: 4
  minAvailableReplicas: 1
  virtualTargetClassName: qcom-sa8295-baremetal
  parameters:
    board:
      fidelity: high                   # deep-merged over class board defaults
  selector:
    matchLabels:
      board: sa8295
      fidelity: high
      virtual: "true"
  template:
    metadata:
      labels:
        board: sa8295
        fidelity: high
        virtual: "true"
    spec:
      drivers:
        - name: qemu
          type: jumpstarter_driver_qemu.driver.Qemu
        - name: tcp
          type: jumpstarter_driver_network.driver.TcpNetwork
          config:
            port: 22
        - name: serial
          type: jumpstarter_driver_serial.driver.QemuSerial
```

**Provisioner actions (off-cluster):**

1. Read merged `parameters` and `credentialsSecretRef`.
2. Select a host with free capacity (`slots`).
3. Deploy or attach exporter + runtime on the host (SSH, systemd, or lab agent).
4. Create an `Exporter` CR in the cluster with template labels; register with
   `jumpstarter-controller`.
5. On scale-down or failure, tear down the remote instance and delete the
   `Exporter` CR.

Physical reference boards on the same lab network can coexist in the pool —
users distinguish them with labels (`virtual=false` vs `virtual=true`) without
changing the lease workflow.

## Design Decisions

### DD-1: Pool-based scaling vs. purely on-demand provisioning

**Alternatives considered:**

1. **Pool-based with configurable min/max** — Maintain a warm pool of
   pre-spawned instances; scale between `minAvailableReplicas` and `maxReplicas`.
2. **Purely on-demand** — Spawn a new instance only when a lease request arrives;
   destroy it when the lease is released.

**Decision:** Pool-based with configurable min/max.

**Rationale:** Purely on-demand provisioning introduces noticeable latency for
CI pipelines (Pod scheduling + image pull + VM boot + exporter registration
typically takes 10-15s, and up to 60s with cold image pulls or heavy
provisioners). A warm pool provides instant lease fulfillment for the common
case. Setting `minAvailableReplicas: 0` still allows purely on-demand behavior
for rarely-used targets. `VirtualTargetClass.bindingMode: WaitForFirstConsumer`
maps to on-demand provisioning; `Immediate` maps to warm pools.

### DD-2: Provisioner controller deployment model

**Alternatives considered:**

1. **Separate binary per provisioner** — Each provisioner is a completely
   independent binary/image.
2. **Single binary, one deployment per provisioner** — One image contains all
   provisioner reconcilers; a CLI flag (`--provisioner=qemu.jumpstarter.dev`)
   selects which one to activate.
3. **Single binary, single deployment** — One Deployment runs all provisioners.
4. **Integrated into jumpstarter-controller** — Add reconcilers directly into
   the existing operator.

**Decision:** Option 2 — single binary, one Deployment per provisioner.

**Rationale:** A single image is cheaper to build, test, and productize.
Deploying as separate Deployments gives operational benefits: isolated logs,
independent restarts, and explicit `--provisioner` selection. Adding a new
backend means adding a Deployment manifest with a different flag — no new image
build required.

### DD-3: Pluggable provisioner vs. CRD-per-pool vs. typed claims

**Alternatives considered:**

1. **CRD per provider pool** (`QEMUExporterPool`, `AndroidExporterPool`, etc.)
   — provider typing at the pool CRD level.
2. **Generic `ExporterSet` + pluggable `VirtualTargetClass.provisioner` +
   nested `parameters`** — orchestration generic; backend selected by provisioner
   string; device config as nested YAML on class + set (deep-merge).
3. **Typed `*VirtualTarget` CRDs per provider** (`QEMUVirtualTarget`,
   `CorelliumVirtualTarget`, etc.) — strong schema per backend, referenced from
   `ExporterSet`.
4. **Fully generic opaque config** — single CRD with flat `provider.config` map.

**Decision:** Option 2 — generic `ExporterSet` + pluggable provisioner on
`VirtualTargetClass` with **dictionary-based nested `parameters`**. Reject
options 1 and 3.

**Rationale:** Separating orchestration (scaling, lease matching, graceful
shutdown) from provisioning (QEMU container, Corellium API, off-cluster hosts)
lets each provisioner implement backend-appropriate scaling while exposing an
identical scaling surface (`minReplicas`/`maxReplicas`/`minAvailableReplicas`).
Nested `parameters` on `VirtualTargetClass` and optional `ExporterSet` overrides
replace per-provider claim CRDs — homogeneous pools need only two admin CRDs.
Typed `*VirtualTarget` claims add maintenance overhead without benefit when
pools share one backend profile (2026-06 team review). New backends add a
provisioner string and parameter conventions, not pool-tier or claim-kind changes.

### DD-4: Per-lease parameters vs. pool flavors

**Alternatives considered:**

1. **Per-lease `parameters` dictionary** — Leases carry opaque hints (CPU,
   memory, storage) interpreted by provisioners.
2. **Multiple `ExporterSet` flavors** — Administrators create separate sets for
   different resource profiles; users select via label matching.

**Decision:** Option 2 — multiple set flavors via separate `ExporterSet` CRs.

**Rationale:** Per-lease parameters add complexity across every layer for a use
case already satisfied by separate sets with different labels and
`VirtualTargetClass` parameters. Per-lease parameters can be revisited in a
future JEP if needed.

### DD-5: Built-in scaling vs. HPA / KEDA

**Alternatives considered:**

1. **Built-in scaling logic** — Each provisioner implements lease-aware
   reconciliation with a consistent scaling API.
2. **Kubernetes HPA** — Horizontal Pod Autoscaler with custom metrics.
3. **KEDA** — Event-driven autoscaler with a custom Jumpstarter scaler.

**Decision:** Option 1 — built-in scaling logic with consistent API surface;
HPA/KEDA as complementary via `scale` subresource and exposed metrics.

**Rationale:** Each provisioner should implement autoscaling appropriate to its
backend (local container churn vs. EC2 quotas vs. external API rate limits). A
single generic autoscaler cannot express lease-aware matching, graceful
disable-before-delete, or `minAvailableReplicas` invariants. However, the
**same scaling vocabulary** (`minReplicas`/`maxReplicas`/`minAvailableReplicas`)
and the `scale` subresource apply across all provisioners — one mental model for
users, backend-specific logic underneath. Pool metrics for HPA/KEDA are listed
in *Future Possibilities*.

### DD-6: VirtualTargetClass vs. inline credentials

**Alternatives considered:**

1. **Inline credentials in every `ExporterSet`** — simple but duplicates secrets
   across pools sharing the same backend account.
2. **`VirtualTargetClass` (namespaced backend profile)** — class in the same
   namespace as the referencing `ExporterSet` holds credentials, nested
   `parameters`, and scheduling; `ExporterSet.spec.virtualTargetClassName`
   references the class by local name.
3. **Separate `ProviderConfig` CRD** — lighter-weight credential sharing without
   full class semantics.

**Decision:** Option 2 — **namespaced** `VirtualTargetClass` with optional future
`ProviderConfig` for multi-account credential reuse.

**Rationale:** Unlike CSI `StorageClass` (cluster-scoped), `VirtualTargetClass`
is **namespaced** so teams define isolated backend profiles, credentials, and
scheduling per namespace without cluster-admin involvement. `ExporterSet` may
only reference a class in the **same namespace**; `credentialsSecretRef` points
to a Secret in that namespace — credentials never appear on `ExporterSet`.
`bindingMode` and `reclaimPolicy` still map to warm-pool vs. on-demand and
external target retention. The StorageClass/PVC *separation of class and consumer*
is retained; only scope differs.

### DD-7: Instance TTL and image refresh (deferred)

**Alternatives considered:**

1. **`ExporterSet.spec.ttl` with image refresh** — declarative `maxAge`,
   `maxIdleAge`, and `imageRefreshPolicy` on the pool CRD; controller recycles
   instances and re-pulls container/firmware images to keep warm pools fresh.
2. **Manual / CronJob pool flush** — operators restart pools or delete Pods on a
   schedule outside Jumpstarter.
3. **Admin-pinned images in `parameters`** — declare expected OS/firmware refs on
   `VirtualTargetClass` / `ExporterSet`; provisioner always boots those images.
4. **User flash at lease time (v1)** — warm pool instances are provisioned with
   baseline runtime only; the lessee flashes and boots the image they want via
   existing drivers (`storage.flash`, power cycle) — same workflow as physical
   targets.
5. **Separate lifecycle controller (future)** — a cross-cutting controller that
   periodically visits **physical and virtual** exporters and flashes the
   expected image, without virtual-only fields on `ExporterSet`.

**Decision:** Reject options 1–3 for v1 — **no TTL, image-refresh, or
admin-pinned boot images on `ExporterSet` / `VirtualTargetClass`**. Option 4
matches current Jumpstarter behavior: users flash and boot what they need after
leasing. Option 5 remains the preferred direction for automated image hygiene
later.

**Rationale:** Time-based Pod recycle and provisioner-driven image re-pull are
virtual-pool mechanics that **physical exporters do not share**. Physical machines
have no `maxAge`; their OS changes when someone flashes them, not when a pool
controller rotates Pods. Putting TTL or pinned boot images on `ExporterSet` alone
would split the lease experience. In v1, virtual targets in the warm pool behave
like physical benches: the lessee selects and flashes the desired image. A future
**separate lifecycle controller** can watch `Exporter` resources regardless of
origin and apply uniform policies — e.g. periodic flash of a lab-defined expected
image to idle exporters, scheduled maintenance windows — combining long-lived
(non-refreshed) exporter instances with automated image updates when operators
choose to enable them.

## Design Details

### Reconciliation Loop

Each `ExporterSet` controller runs a continuous reconciliation loop, triggered by
changes to the set CR, owned Exporters, or matching Leases:

```text
for each ExporterSet CR:
  mergedParameters = deepMerge(class.parameters, set.parameters)
  ownedExporters = list Exporters owned by this CR
  replicas = count ownedExporters in Ready state
  leasedReplicas = count ownedExporters with an active LeaseRef
  availableReplicas = replicas - leasedReplicas
  pendingLeases = count pending Leases matching spec.selector
  
  # Invariant: maintain minAvailableReplicas warm buffer
  if availableReplicas < spec.minAvailableReplicas AND replicas < spec.maxReplicas:
    scale up to restore availableReplicas

  # Demand-driven scale-up
  elif pendingLeases > 0 AND replicas < spec.maxReplicas:
    scale up by min(pendingLeases, spec.maxReplicas - replicas)

  # Scale-down: excess idle replicas
  elif availableReplicas > spec.minAvailableReplicas AND cooldown elapsed:
    graceful scale down:
      1. set exporter.spec.enabled = false
      2. wait until leaseRef remains empty
      3. delete Pod and Exporter CR
    (never below minAvailableReplicas)
```

### Instance States

Each virtual exporter instance transitions through:

```text
Provisioning → Ready (warm pool) → Leased → Ready
                                              └→ Terminating → (deleted if available>min)
```

- **Provisioning:** Pod starting, virtual target provisioning, exporter registering.
- **Ready:** Exporter registered and available for lease.
- **Leased:** Exporter assigned to an active lease.
- **Terminating:** Instance being deleted (scale-down or failure replace).

### DriverConfig and ExporterConfig Generation

Each driver entry in `ExporterSet.spec.template.spec.drivers` is a `DriverConfig`:

```yaml
drivers:
  - name: qemu
    type: jumpstarter_driver_qemu.driver.Qemu
    config:
      arch: x86_64
      smp: 2
      mem: 2G
  - name: tcp
    type: jumpstarter_driver_network.driver.TcpNetwork
    config:
      port: 2222
```

**`name` field:** A required string that becomes the key in the generated
`ExporterConfig`'s `export:` map. Names must be unique across all driver
entries in the same template. Explicit names allow the provisioner's
enrichment logic to locate specific driver entries by name and ensure
predictable export map keys.

**Two-phase instance creation:** The reconciler creates each `Exporter` CR
first (phase 1). Only after the Jumpstarter controller provisions credentials
and an endpoint for the Exporter (populating `status.credential` and
`status.endpoint`) does the reconciler generate the `ExporterConfig` Secret
and create the Pod (phase 2). This ensures the config always contains valid
connection details.

**ExporterConfig generation:** The controller builds a complete `ExporterConfig`
YAML containing:

- **endpoint** — The controller's gRPC endpoint from `Exporter.status.endpoint`.
- **token** — Retrieved from the Secret referenced by `Exporter.status.credential`.
- **tls.ca** — Base64-encoded CA certificate from the `jumpstarter-service-ca-cert`
  ConfigMap (cert-manager integration).
- **export** — A map of driver name → `{type, config}` built from the template
  drivers list, enriched by the provisioner.

**Provisioner enrichment (`EnrichExporterExport`):** Before persisting the
config, the provisioner may inject or override driver entries. For example,
the `qemu.jumpstarter.dev` provisioner:

- Injects `launcher_socket` pointing to the shared-volume Unix socket path.
- Injects architecture-appropriate `default_partitions` (EDK2 firmware paths)
  unless the user already specified them.
- Injects `hostfwd` settings for SSH access unless already present.
- Auto-injects a `tcp` wrapper driver if not already present.

**Driver-name collision handling:** The `buildExportMap` function rejects
duplicate keys in the export map. If two `DriverConfig` entries share the
same `name`, the controller returns an error during config generation.

**Pod naming:** The Pod created for each Exporter uses the Exporter's own name
(`exporter.Name`) rather than a generated name. This 1:1 correspondence makes
it straightforward to correlate Pods, Exporters, and their logs.

**`exitOnLeaseEnd` derivation:** The `exitOnLeaseEnd` flag in the generated
`ExporterConfig` is derived from `ExporterSet.spec.recycleStrategy`:

- `ExitAndReplace` (default) → `exitOnLeaseEnd: true` — the exporter (main
  container) exits when its lease ends. The Pod uses `restartPolicy: Never`
  so that exit completes the Pod (rather than restarting `jmp run` in place);
  Kubernetes then terminates the `target-runtime` native sidecar. The
  ExporterSet controller replaces the instance. The exporter also best-effort
  calls `jumpstarter-exec shutdown` for a clean runtime exit before process
  death.
- `InPlaceReuse` → `exitOnLeaseEnd: false` — the exporter stays running and
  transitions back to available (runtime sidecar is left alone).

### Component Interaction

1. Administrator creates `VirtualTargetClass` and `ExporterSet` resources.
2. The provisioner controller provisions `minAvailableReplicas` Exporters.
3. Each instance Pod boots the virtual target and runs the Jumpstarter exporter,
   registering with the existing `jumpstarter-controller`.
4. Instances appear as regular exporters with labels from `spec.template.metadata`.
5. Users lease them normally — the existing controller handles assignment.
6. On lease release, the instance is recycled per `recycleStrategy`:
   - **Exit-and-replace (default):** Exporter exits; controller replaces the
     instance proactively to maintain `minAvailableReplicas`.
   - **In-place reuse:** Exporter resets internal state without exiting; Pod
     remains running and transitions back to Ready immediately.
7. The `ExporterSet` controller continuously monitors utilization and scales.

### Failure Modes

- **Pod crash:** Controller detects failure via Pod status, replaces the instance,
  maintains `minAvailableReplicas` invariant.
- **Resource exhaustion:** Cannot scale beyond cluster capacity; set stays at
  current size, new leases queue as for physical targets.
- **Provisioner startup failure:** Instance marked failed, controller retries with
  backoff, alerts via conditions on the set status.
- **Scaling storm:** Rate limiting on scale-up prevents creating too many
  instances simultaneously.

## Test Plan

### Unit Tests
Unit tests should meet the project test coverage requirements.

### Integration Tests

- Kind e2e suite labeled `exporterset-qemu` (`e2e/test/exporterset_qemu_test.go`,
  part of `make e2e-run` / CI `e2e-tests`): apply kind-friendly
  `VirtualTargetClass` + `ExporterSet`, wait for Exporter/Pod Ready, lease,
  flash Alpine UEFI tiny, expect a serial-console boot marker. Guest disk is a
  sized emptyDir (or ephemeral PVC when `parameters.storage.storageClassName` is
  set). Use `make e2e-exporterset-qemu` for a focused local run.
- Mixed physical/virtual lease orchestration
- Provisioner failure and recovery scenarios
- Parameter deep-merge and provisioner-side validation
- `VirtualTargetClass` credential injection

## Acceptance Criteria

- [ ] `VirtualTargetClass` and `ExporterSet` CRDs defined
- [ ] `ExporterSet` controller maintains `minAvailableReplicas` warm buffer
- [ ] Controller scales up when available pool is depleted (up to `maxReplicas`)
- [ ] Controller scales down idle replicas after cooldown (never below
      `minAvailableReplicas`)
- [ ] QEMU provisioner (`qemu.jumpstarter.dev`) fully implemented and tested
- [ ] Virtual instances register as standard exporters and are leasable without
      changes to the existing lease flow
- [ ] Pod failures detected and reported in `ExporterSet` status
- [ ] An `ExporterSet` with `minAvailableReplicas: 0` provisions on demand only
- [ ] Status subresource reports Deployment-style counters and health conditions
- [ ] `scale` subresource enables `kubectl scale` interoperability
- [ ] `parameters` deep-merge produces correct merged config for provisioner
- [ ] Provisioner validates merged `parameters` and surfaces errors via conditions
- [ ] Documentation covers `VirtualTargetClass` and `ExporterSet` configuration

## Graduation Criteria

### Experimental

- QEMU provisioner functional in a development cluster
- Basic set lifecycle works end-to-end (scale up, lease, release, scale down)
- Community feedback on CRD schema and scaling behavior

### Stable

- QEMU reference provisioner (`qemu.jumpstarter.dev`) production-ready; at least
  one additional topology validated (e.g. off-cluster bare metal or API-backed)
- Production usage by at least one team for >1 month
- Performance benchmarks documented (cold-start latency, scaling responsiveness)
- Provisioner authoring guide published (how to add a new provisioner)

## Backward Compatibility

- Existing physical-only workflows are unaffected; lease requests without
  virtual-specific labels continue to work as before.
- No changes to the existing gRPC protocol for physical exporters.
- New CRDs (`VirtualTargetClass`, `ExporterSet`) are additive.
- **Exporter `enabled` field:** Defaults to `true`, so all existing Exporters
  continue to behave exactly as before.
- Administrators upgrading see no behavior change until they explicitly deploy
  `ExporterSet` and `VirtualTargetClass` resources.

## Consequences

### Positive

- **Instant lease fulfillment:** Warm pools eliminate provisioning latency.
- **Elastic scaling:** Sets grow and shrink with demand.
- **Unified user experience:** Virtual and physical targets leased the same way.
- **Kubernetes-native UX:** `minReplicas`/`maxReplicas`/`minAvailableReplicas`,
  Deployment-style status, `kubectl scale` — familiar to cluster admins.
- **Pluggable backends:** New provisioners add a provisioner string.
- **Credential separation:** `VirtualTargetClass` keeps secrets off `ExporterSet` resources.
- **Fidelity ladder:** Same lease flow across sim, cloud virtual, and hardware tiers.

### Negative

- **Increased CRD surface:** `VirtualTargetClass` and `ExporterSet` add more
  resources to manage than a single pool CRD per provider.
- **Resource consumption:** Warm pools consume cluster resources when idle.
- **Sidecar complexity:** Container-backed provisioners require multi-container
  Pod orchestration and shared-volume protocols.

### Risks

- **Scaling storms:** Burst demand could exhaust cluster resources; rate limiting
  mitigates but may delay lease fulfillment.
- **Provisioner reliability:** Failed startups can cause crash-replace loops.

## Rejected Alternatives

- **Static fixed-size pools (status quo):** Cannot scale with demand.
- **External orchestration (Terraform/Ansible):** Breaks lease semantics integration.
- **Per-lease `parameters` dictionary:** See DD-4.
- **CRD-per-pool without VirtualTarget separation:** Couples scaling and provider
  config; rejected in favor of generic `ExporterSet` + pluggable provisioner.
- **Typed `*VirtualTarget` CRDs per provider:** Rejected at 2026-06 team review;
  see DD-3. Dictionary `parameters` on class + set suffice for homogeneous pools.
- **`ExporterSet.spec.ttl` and image refresh:** Rejected for v1; see DD-7. Would
  create virtual-only lifecycle semantics unlike physical exporters.

## Prior Art

- **LAVA:** Virtual DUTs via QEMU with static configuration; no on-demand scaling.
- **Crossplane:** General-purpose cloud composition; no Jumpstarter lease semantics.
  Useful reference for external API integration (e.g., Corellium) but does not
  replace pool-specific scaling logic.
- **CSI (StorageClass/PVC):** Class/consumer separation adopted; scope is
  namespaced rather than cluster-scoped (see DD-6).
- **KubeVirt:** VM orchestration with pre-mounted images; Jumpstarter differs by
  flash-at-runtime model and exporter-as-sidecar pattern.

## Unresolved Questions

- What is the exact scaling algorithm (proportional, step-based, predictive)?
- **Pod initialization for container-backed provisioners:** Native sidecar init
  containers (`restartPolicy: Always`, KEP-753) vs. lifecycle hooks vs. other
  co-location patterns for exporter + target-runtime. The sidecar sketch in this
  JEP is provisional; resolve in the QEMU provisioner implementation PR.

### Resolved

- **Observability (JEP-0013):** Provisioner controllers emit metrics per JEP-0013.
- **Lease release detection:** Controllers watch Lease objects directly.
- **Scheduled leases:** `Spec.BeginTime` on Lease CRs; controllers ignore future-dated
  leases until effective.

## Future Possibilities

The following extensions are explicitly **not** part of this JEP but the model
stays open to them:

- **Disaggregated/cross-node accelerators** — ARM64 runtime bridged to a remote
  GPU via virtio-gpu/RDMA.
- **Separate `ProviderConfig` CRD** — multi-account credential reuse and rotation
  referenced by multiple `VirtualTargetClass` resources.
- **Realized-instance CRD (PV analog)** — for static/pre-provisioned devices that
  exist outside the dynamic provisioning flow.
- **`ExporterDeployment` rollout tier** — Deployment analog for rolling updates
  across pool instances (versioned template changes).
- **Multiple/spawned-on-lease VirtualTargets per Exporter** — composite benches
  and multi-device topologies.
- **Universal physical+virtual `Target` abstraction** — single resource type
  spanning hardware and virtual backends.
- **Priority selectors / DeviceClass** — ordered label fallback ("prefer hardware,
  fall back to QEMU") at lease time.
- **HPA/KEDA metric exposure** — complementary external autoscaling once core
  provisioner controllers are stable.
- **Renode provider** — `renode.jumpstarter.dev` provisioner leveraging JEP-0010.
- **Additional cloud/container provisioners** — `corellium.jumpstarter.dev`,
  `android.jumpstarter.dev`, `ec2.jumpstarter.dev` (no typed claim CRDs).
- **Composite leases** — multiple exporters linked into one logical lease.
- **Cross-cutting lifecycle controller** — periodic flash of lab-defined expected
  images to idle **physical and virtual** exporters (see DD-7); long-lived pool
  instances combined with optional automated image updates, not virtual-only TTL
  on `ExporterSet`.

## Implementation Plan

The implementation is broken into phases. Each phase delivers a usable
increment and can be merged independently. **v1 focuses on the QEMU reference
implementation**; additional provisioners and lifecycle automation are deferred.

| Phase | Scope | Status |
| --- | --- | --- |
| 1 | Exporter `enabled` field | Near-term |
| 2 | `VirtualTargetClass` + `ExporterSet` CRDs; nested `parameters`; `qemu.jumpstarter.dev` | Near-term (v1) |
| 3 | External/off-cluster provisioning (`qemu-baremetal.jumpstarter.dev`) | Near-term |
| 4+ | Lifecycle controller, Corellium/Android, etc. | Deferred — see *Future phases* |

### Phase 1: Exporter `enabled` field

Add the `enabled` boolean field to the Exporter CRD and update the
`jumpstarter-controller` lease assignment logic to skip disabled exporters.

**Deliverables:**

- [ ] Add `spec.enabled` field to Exporter CRD (default: `true`)
- [ ] Update lease assignment in `jumpstarter-controller` to filter out
      disabled exporters
- [ ] Unit tests for the filtering logic
- [ ] Integration test: disable an exporter, verify it gets no new leases

### Phase 2: Core CRDs and QEMU reference provisioner

Define namespaced `VirtualTargetClass` and `ExporterSet` CRDs. Implement
**only** the `qemu.jumpstarter.dev` in-cluster provisioner — the reference
implementation for the 2-CRD model, parameter deep-merge, warm pool, and
flash-at-lease workflow (DD-7).

**Deliverables:**

- [ ] Define `VirtualTargetClass` and `ExporterSet` CRD schemas (namespaced;
      nested `parameters` with schemaless object fields; same-namespace reference
      rule)
- [ ] Implement parameter deep-merge and provisioner-side validation
- [ ] Implement exporter-set controller binary with `--provisioner=qemu.jumpstarter.dev`
- [ ] Sidecar Pod rendering (provisional init-container model — see Unresolved
      Questions)
- [ ] Core scaling logic: `minAvailableReplicas`, demand-driven scale-up, graceful
      scale-down
- [ ] Deployment-style status + `scale` subresource
- [ ] Watch Leases and Exporters for scaling decisions
- [ ] Add `exporterSets` section to `Jumpstarter` operator CR
- [x] Integration test: deploy `ExporterSet`, lease, flash, boot, release,
      observe scaling (`exporterset-qemu` e2e)

### Phase 3: External / off-cluster provisioning

Extend the exporter-set controller with an off-cluster QEMU provisioner to
validate the pluggable backend model beyond in-cluster Pods. Documents and
implements the flow in *External and Off-Cluster Provisioning*.

**Deliverables:**

- [ ] `qemu-baremetal.jumpstarter.dev` provisioner (or equivalent off-cluster
      stub) using the same binary with `--provisioner=qemu-baremetal.jumpstarter.dev`
- [ ] Remote host selection, SSH/agent deploy, and `Exporter` CR registration from
      off-cluster instances
- [ ] Example `VirtualTargetClass` + `ExporterSet` manifests for lab bare-metal
      (automotive profile)
- [ ] Integration test or documented manual test plan for off-cluster scale-up
      and lease

### Future phases (deferred)

The following are **explicitly out of v1** scope. They reuse the same
`VirtualTargetClass` + `ExporterSet` CRDs and nested `parameters` — no typed
claim CRDs.

**Additional provisioners**

- [ ] `corellium.jumpstarter.dev` — API-backed cloud virtual devices
- [ ] `android.jumpstarter.dev` — in-cluster Android emulator pools
- [ ] `ec2.jumpstarter.dev` — AWS-backed targets
- [ ] Provisioner authoring guide

**Cross-cutting lifecycle controller (DD-7)**

- [ ] Separate controller for periodic flash / maintenance on **physical and
      virtual** exporters — not `ExporterSet.spec.ttl`

## Implementation History

- 2025-10-30: RFE filed upstream (GitHub #41)
- 2026-06-03: JEP proposed
- 2026-06-18: Revised per review — ExporterSet, VirtualTargetClass, pluggable
  provisioner model; added end-to-end flow section
- 2026-06-18: Team review — dictionary `parameters`, removed typed VirtualTarget
  CRDs, namespaced `VirtualTargetClass`, deferred TTL (DD-7)
- 2026-07-24: Added `name` field to `DriverConfig` for export map key naming;
  documented two-phase instance creation (Exporter CR first, Pod after
  credentials ready); added ExporterConfig injection with auto-enrichment
  (firmware paths, hostfwd, wrapper drivers)
- 2026-07-27: Added typed `ImageOverrides` on `VirtualTargetClass` and
  `ExporterSet` for structured image/imagePullPolicy overrides with ExporterSet
  taking precedence over VTC; replaces previous unstructured parameters approach
- 2026-07-27: Documented driver-name collision rejection in `buildExportMap`,
  Pod naming matching Exporter names, `exitOnLeaseEnd` derivation from
  `recycleStrategy`, and provisioner enrichment precedence rules
- 2026-07-28: Made `DriverConfig.name` mandatory (no longer derived from type);
  removed `wait-for-binary.sh` script in favor of direct `jumpstarter-exec`
  entrypoint in `qemu-runtime` container; updated all examples
- 2026-09-01: Guest disk at `/disk` via `parameters.storage.size` (logical size),
  optional `parameters.storage.fsOverhead` (default `"10%"`), and optional
  `parameters.storage.storageClassName` (generic ephemeral PVC or sized emptyDir);
  sockets remain on `/shared`

## References

- [GitHub Issue #41: RFE: On-Demand Virtual Target Provisioning](https://github.com/jumpstarter-dev/jumpstarter/issues/41)
- [PITCREW-409: jumpstarter JEP: virtual scalable exporters](https://redhat.atlassian.net/browse/PITCREW-409)
- [JEP-0010: Renode Integration](JEP-0010-renode-integration.md) — Related provider
- [JEP-0013: Observability](JEP-0013-observability-telemetry-logs.md) — Integration point

---

This JEP is licensed under the
[Apache License, Version 2.0](https://www.apache.org/licenses/LICENSE-2.0),
