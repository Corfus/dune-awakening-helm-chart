# Dune: Awakening server on Kubernetes

Install, configure and operate a Dune: Awakening server with Helm or Argo CD.
Based on [Funcom's official self-hosted server package](https://duneawakening.com/self-hosted-servers/),
with operator deployment definitions adapted from
[dune-dedicated-server-manager](https://github.com/adainrivers/dune-dedicated-server-manager/blob/307ae52df028898bdf73a3ead1aef4b8927a8e6d/crates/dune-manager-core/src/orchestration/ubuntu_ssh_setup/operator_yaml.rs).
Original project code is licensed under the [MIT License](LICENSE).
See [THIRD-PARTY-NOTICES.md](THIRD-PARTY-NOTICES.md) for third-party licensing.

Use a local checkout of this repository for the commands below. Published GitHub
releases also provide a packaged chart; see [Installing a release](#installing-a-release).

## Prerequisites

- Helm 3, cert-manager, and a configured storage class. Set `storage.class` to
  the storage class available in your cluster.
- An amd64 node with AVX2. Set `nodeSelector` to restrict workloads to suitable
  nodes; the default `{}` does not pin them to a node.
- Registry access on every node running operators or game workloads. Publish the
  required images to your own registry and set `images.registry` to its path.
- Reachable node IP and free UDP ports 7777–7810 and TCP NodePort 31982. The
  bootstrap Job advertises its node's `status.hostIP`; NAT requires forwarding
  and a player-reachable advertised address.

Only one installation is supported per cluster: Funcom's CRDs, ClusterRoles,
webhooks and resource names are shared. The release namespace is used in RBAC,
certificate DNS names, webhook references and CA injection annotations. All
namespaced resources explicitly use this namespace.

## Credentials and installation

Choose any namespace and use the same value for credentials and installation
(`my-game` below is only an example):

```bash
DUNE_NAMESPACE=my-game
kubectl create namespace "$DUNE_NAMESPACE"
python3 scripts/create-credentials.py --namespace "$DUNE_NAMESPACE"
helm upgrade --install dune . --namespace "$DUNE_NAMESPACE" --wait --timeout 15m \
  -f values.private.yaml
```

Create `values.private.yaml` locally (it is ignored by Git):

```yaml
images:
  registry: registry.example.com/dune-awakening-images
```

Replace the example registry with the path where you published the required images.
The helper prompts privately for an optional player login password and stores it in
`dune-credentials`, under `server-login-password`. Omit this key or leave it empty
for public player access. Player passwords are inserted at runtime, so they do not
appear in Helm values or the template ConfigMap. The upstream build still requires
them in the live BattleGroup, generated configuration, startup arguments and game
logs; restrict access to all of these.

The helper prompts privately for the Funcom self-hosting token, extracts its HostId,
and generates a world ID and independent passwords. It reuses an existing
`dune-credentials` Secret. Keep this Secret backed up with the database; do not
regenerate credentials or world identity for an existing installation.

You can instead provision a Secret yourself with these keys: `fls-token`,
`world-id`, `database-password`, `postgres-password`, `rmq-auth-secret`, `fls-apikey`.
Generate `fls-apikey` once as a random UUID and preserve it on upgrades. Optional
`server-login-password` sets the player password; newlines, quotes, apostrophes and
pipes are unsupported. Set
`credentials.existingSecret` if its name differs. `world-id` must be
`sh-<lowercase-HostId>-<six-lowercase-letters>`. The chart does not bundle the
cluster-specific SealedSecret from the original deployment. Manage it separately;
the helper's `--sealed-output` option remains available.

## Installing a release

Each published GitHub release runs the `Publish Helm chart` workflow, which
validates the chart and attaches `dune-awakening-<version>.tgz` to that release.
The release tag must match `Chart.yaml`'s version, with an optional `v` prefix.

After creating the namespace, credentials and `values.private.yaml` as described
above, download and install the chart archive. Choose an available release from
[GitHub releases](https://github.com/Corfus/dune-awakening-helm-chart/releases):

```bash
CHART_VERSION=0.2.6
CHART_TAG="v${CHART_VERSION}" # Use the release's exact tag, including any v prefix.
curl --fail --location \
  "https://github.com/Corfus/dune-awakening-helm-chart/releases/download/${CHART_TAG}/dune-awakening-${CHART_VERSION}.tgz" \
  --output "dune-awakening-${CHART_VERSION}.tgz"
helm upgrade --install dune "./dune-awakening-${CHART_VERSION}.tgz" \
  --namespace "$DUNE_NAMESPACE" --wait --timeout 15m -f values.private.yaml
```

## Image configuration

All image names are hardcoded in the chart. Registries include any project path,
without the image name or tag. Version values are Docker tags and should be quoted.

### Getting the images

This chart does not build or distribute the Funcom images. Obtain the official
Linux dedicated-server package from Steam (App `4754530`, depot `4754532`) and
prepare/publish the required images to a registry your Kubernetes nodes can
access. For example, SteamCMD can download the Linux server package into a local
directory:

```sh
steamcmd \
  +force_install_dir "$PWD/dune-server" \
  +login anonymous \
  +app_update 4754530 validate \
  +quit
```

The chart's current game image pin is `2162777-0-shipping` (depot manifest
`6252913279158604722`); its operator image pin is `v1.7.0`. Use the downloaded
package and the corresponding operator artifacts in your image build/publish
process, tagging the resulting images with the versions configured in the chart.
For example, a published game image might be
`registry.example.com/dune-awakening-images/seabass-server:2162777-0-shipping`;
set `images.registry` to `registry.example.com/dune-awakening-images`.
See [Image configuration](#image-configuration) below for the full list of
image names and tags the chart expects.

```yaml
images:
  registry: registry.example.com/dune-awakening-images
  operators:
    version: "v1.7.0"
  game:
    version: "2162777-0-shipping"
    revision: 2162777
  postgres:
    version: "17.4-alpine-fc-13"
```

This renders images such as
`registry.example.com/dune-awakening-images/seabass-server:2162777-0-shipping`.
The same registry supplies all four `igw-k8s-*-operator` images, `igw-postgres`,
`seabass-server`, `seabass-server-bg-director`, `seabass-server-rabbitmq`,
`seabass-server-gateway` and `seabass-server-text-router`.

Auxiliary images have separate registry and version values under `images.yq`,
`images.python`, `images.pghero` and `images.filebrowser`. Their fixed names are
`mikefarah/yq`, `library/python`, `ankane/pghero` and `filebrowser/filebrowser`.
Unknown keys in `images` are rejected, including attempts to set image names.

Update the game revision together with its version. Coordinate upstream changes
with CRDs, operator flags, webhook paths, the BattleGroup template and image pins;
changing a tag alone does not migrate schemas. Values determine the rendered
image references.

## Settings and lifecycle

[values.yaml](values.yaml) exposes node placement, storage class and sizes, world
title, stop flag, BattlEye, PvP mode, gameplay, per-map resources, credentials Secret
name and image settings. Defaults retain
20Gi RWO database storage, 5Gi RWX shared storage, and upstream map limits
(Survival 12Gi, Overmap 2Gi). Additional maps increase memory requirements.

Set anti-cheat and combat rules in your values file:

```yaml
world:
  battlEyeEnabled: false
  pvpMode: NoPVP
```

`world.battlEyeEnabled` accepts `true` or `false`; the default `null` leaves the
game image's BattlEye setting unchanged. It renders `BattlEye.Enabled` in
`UserEngine.ini`. Explicit BattlEye launch arguments take precedence over this setting.
`world.pvpMode` renders `PVPMode` in `UserServerCustomSettings.ini`: `NoPVP` selects
PvE, `Limited` (default) retains normal PvP zones, and `FullPVP` selects PvP everywhere.
Upgrade the release to apply these values, then restart the full battlegroup through
Funcom lifecycle controls to load the settings. Rendering checks do not verify
anti-cheat enforcement or combat rules in a running game.

### Gameplay customization

All settings below live under `world.gameplay`. Numeric multipliers default to `1`.
Zero disables the corresponding effect only where the allowed range includes zero.
The shipped INI settings define these ranges; gameplay behavior still requires
verification in a running server.

| Value | Allowed values | Purpose / default |
|---|---|---|
| `gatheringMultiplier` | 0.1–10 | Hand and vehicle mining yield |
| `xpMultiplier` | 0–10 | Global XP gain |
| `combatXpMultiplier` | 0–10 | Combat XP gain |
| `gatheringXpMultiplier` | 0–10 | Gathering XP gain |
| `missionXpMultiplier` | 0–10 | Mission XP gain |
| `craftingTimeMultiplier` | 0–5 | Crafting/refining time; zero is instant |
| `craftingCostMultiplier` | 0–10 | Crafting costs; zero removes costs |
| `buildingCostMultiplier` | 0–10 | Building costs; zero removes costs |
| `inventoryVolumeMultiplier` | 0.1–10 | Carrying capacity |
| `lootRespawnSpeed` | 0.1–10 | Higher values mean slower loot respawn |
| `resourceRespawnSpeed` | 0.1–10 | Higher values mean faster resource respawn |
| `equipmentDropOnDeath` | `All`, `Backpack`, `Default`, `None` | Equipment loss; default `Default` |
| `sandwormDeathPenalty` | `All`, `Backpack`, `Default`, `None` | Sandworm death consequences; default `All` |
| `playerDeathLootRule` | `DependsOnSecurityZone`, `NeverAllowOtherPlayers`, `AlwaysAllowOtherPlayers` | Who can loot dead players; default `DependsOnSecurityZone` |
| `itemDurabilityDrainMultiplier` | 0–10 | Item durability drain |
| `maxDurabilityLossEnabled` | boolean | Permanent maximum durability loss; default `true` |
| `vehicleDurabilityDamageMultiplier` | 0–10 | Vehicle durability damage |
| `sandwormsEnabled` | boolean | Worm spawning and danger zones; default `true` |
| `sandstormsEnabled` | boolean | Ordinary sandstorms and storm treasure; default `true` |
| `coriolisStormsEnabled` | boolean | Coriolis storm auto-spawning; default `true` |
| `buildingPieceLimitMultiplier` | 0.1–10 | Building piece limits |
| `maxBases` | integer 0–10, or `null` | Base count; default `null` preserves the image default |
| `infiniteBuildingStability` | boolean | Infinite building stability; default `false` |
| `dynamicBuildingDamageEnabled` | boolean | Environmental building damage; default `true` |
| `thirstMultiplier` | 0–10 | Thirst rate |
| `heatBuildupMultiplier` | 0–10 | Heat buildup rate |

For example:

```yaml
world:
  pvpMode: NoPVP
  gameplay:
    gatheringMultiplier: 2
    xpMultiplier: 2
    craftingTimeMultiplier: 0.5
    equipmentDropOnDeath: "None"
    sandwormDeathPenalty: Backpack
    maxBases: 5
    coriolisStormsEnabled: false
```

Gathering and sandworm/sandstorm settings are synchronized across the custom,
engine and game INI files. Coriolis storms have their own independent switch.
The remaining defaults are retained, including landclaim expansion limits and
other advanced building constraints; changing `maxBases` only changes base count.
Avoid conflicting manual overrides of the same keys. Apply a Helm upgrade and
restart the battlegroup after changing gameplay settings.

### Per-map CPU and memory

`world.mapResources` accepts existing map names with `requests` and `limits`, each
containing optional `cpu` and `memory` quantities. For example:

```yaml
world:
  mapResources:
    Survival_1:
      requests: {cpu: "1", memory: 8Gi}
      limits: {cpu: "4", memory: 16Gi}
    DeepDesert_1:
      requests: {cpu: 500m, memory: 10Gi}
```

Partial overrides preserve that map's original memory limit: Survival has 12Gi,
Deep Desert 15Gi, and other maps have 2Gi, 3Gi or 6Gi. By default, CPU limits and
resource requests are omitted. Overrides affect existing maps and do not enable
maps, increase replicas or change database/operator resources. All 35 bundled
maps are supported; unknown names and resource keys are rejected.

Quote CPU quantities (`"1"`, `"0.5"`, `"500m"`); CPU precision is limited to
millicores. Memory quantities are strings such as `8Gi`, `500M` or `"1073741824"`.
Keep requests within limits and node capacity; Kubernetes validates scheduling
constraints when the operator creates workloads. An empty override retains the
map's default resources.

Advanced map settings, replicas, region and director settings live in
`files/battlegroup.yaml`. Global INI templates live in `files/UserEngine.ini`,
`files/UserServerCustomSettings.ini`, and the embedded `UserGame.ini` in
`files/battlegroup.yaml`. The Helm-rendered template is
stored in a ConfigMap; never apply the unresolved file directly. Keep region
arguments and utility environment variables consistent.

The post-install/post-upgrade Job renders credentials at runtime, applies the two
environment Secrets and the BattleGroup using server-side apply, and retries
transient admission/API startup failures. It runs without privileges or a Docker
daemon. The Job succeeds when configuration is accepted, before all maps are
necessarily ready. Inspect progress with:

```bash
kubectl -n dune get battlegroups,pods,pvc,svc
kubectl -n dune logs deployment/battlegroupoperator-controller-manager
```

Set `world.stop: true` and upgrade to stop the world through Funcom's operator;
set it to false and upgrade to restart. After changing player login passwords,
restart running maps through Funcom lifecycle controls to pick up launch arguments.
For token rotation, replace only `fls-token` in the existing Secret, then upgrade.

## Security boundaries

The unauthenticated upstream file browser is suspended by default. Keep it suspended;
the operator starts it with `--noauth` and host networking. Use a dedicated namespace
and firewall node access to the required game ports. Optional network policies
provide additional protection for workloads on the pod network, as described below.

Operator writes and pod execution are limited to the release namespace. However,
Funcom v1.7.0 operators have cluster-wide caches and no namespace-scoping flag, so
cluster-wide read access (including Secrets and custom resources) remains necessary.
Use a dedicated cluster if unrelated credentials must be isolated. Fully removing
these reads requires upstream operator support; namespace-only read permissions
would prevent the current operators from starting correctly.

Database and superuser passwords and the Funcom token are substituted into the live
BattleGroup and propagated to child custom resources. The bundled database CRDs
accept literal password strings, not Secret references. Treat access to these
resources, pod specifications, logs and backups as credential access, even for
read-only users. Secret encryption at rest alone does not protect credentials in
custom resources; configure encryption for those resources too. Eliminating these
copies requires an upstream schema/operator change.

Chart packaging excludes local private values, common Secret filenames, private
keys and editor state via `.helmignore`. Use these filenames for local credentials
and inspect archives before publishing. Arbitrarily named sensitive files must
stay outside the chart directory.

## Network policies

Policies are opt-in because upstream host-networked clients require trusted node IP
exceptions specific to your cluster. Enable them in your values file after setting
`nodeCIDRs` to the node addresses used for internal connections. Prefer individual
`/32` IPv4 or `/128` IPv6 addresses; broad subnets also trust other machines in those
subnets. This example uses documentation-only addresses; replace them with your own:

```yaml
networkPolicy:
  enabled: true
  nodeCIDRs: ["192.0.2.10/32", "192.0.2.11/32"]
  # Optional: restrict players instead of the default IPv4/IPv6 public access.
  playerCIDRs: ["198.51.100.0/24"]
  # Optional: restrict webhook callers to the API server's observed source IPs.
  apiServerCIDRs: ["192.0.2.20/32"]
  monitoring:
    enabled: true
    namespaceSelector:
      matchLabels:
        kubernetes.io/metadata.name: monitoring
    podSelector:
      matchLabels:
        app.kubernetes.io/name: prometheus
    ports:
      - protocol: TCP
        port: 15692
```

When enabled, policies select all pods in the release namespace and deny incoming
traffic except:

- Traffic from pods in the same namespace and trusted node IP ranges, to workloads
  other than the file browser. These allowances preserve upstream internal traffic.
- Player traffic to `role: igw-server` on UDP 7777–7810, configurable under
  `networkPolicy.game.port` and `endPort`.
- Player traffic to `role: igw-message-queue, messagequeue: game` on the container's
  named TCP `amqp` port (5672), behind NodePort 31982. Admin RabbitMQ and management
  ports receive no public allowance.
- Webhook calls to the four operators on TCP 9443. The default `apiServerCIDRs`
  allows all source IPs on this port; narrow it using the source addresses visible
  at the pods, accounting for SNAT and managed control planes.
- Optional monitoring from pods matching both configured namespace and pod
  selectors, only on configured ports. Monitoring defaults to disabled; port 15692
  is RabbitMQ's Prometheus endpoint. The file browser is excluded.

The chart does not restrict outbound traffic, preserving DNS, Kubernetes API,
storage and Funcom connections. It does not override existing egress restrictions.
Other namespaces can still reach the explicitly allowed public ports, but receive
no general internal-service access unless their traffic appears as a trusted node.
The policies affect unrelated pods too if you put them in the release namespace.

Kubernetes 1.25+ and a network plugin that enforces NetworkPolicy and `endPort`
ranges are required when enabling policies. The upstream v1.7.0 game servers and
file browser use `hostNetwork: true`; enforcement for these pods depends on the
network plugin and may be bypassed. Node-originated traffic is also not fully
restricted by ordinary NetworkPolicies. The file browser remains suspended and has
no chart policy allowing ingress, but this cannot guarantee host-network isolation.
Keep the node firewall in place. Policies are additive, so another policy can grant
additional access. See the [Kubernetes NetworkPolicy documentation](https://kubernetes.io/docs/concepts/services-networking/network-policies/).

Before relying on these policies, verify player connectivity, map startup, RabbitMQ,
webhook admission and monitoring in a test cluster. Confirm that a pod in another
namespace cannot reach database or RabbitMQ management ports. Helm validation
checks policy rendering and intended permissions; it does not test CNI enforcement.

Helm installs CRDs from `crds/` but does not upgrade or delete them. Review and apply
CRD changes explicitly before upgrading operators. The hook-created BattleGroup
and environment Secrets are outside Helm ownership; uninstall does not remove them
or their data. Stop the world explicitly before uninstalling operators.

## Argo CD and migration

Argo CD can render the chart directly from this GitHub repository, pinned to a
release tag; no Helm repository or downloaded chart archive is required. Replace
the example tag, image registry and storage class below with your own settings.
Use an existing release tag, including its exact `v` prefix
if present.

Before syncing, meet the [prerequisites](#prerequisites), create your destination
namespace and provision `dune-credentials` there as described in
[Credentials and installation](#credentials-and-installation). Keep credentials
managed separately from the Application; do not put secrets in its Helm values.

Save this as `application.yaml`:

```yaml
apiVersion: argoproj.io/v1alpha1
kind: Application
metadata:
  name: dune
  namespace: argocd
spec:
  project: default
  source:
    repoURL: https://github.com/Corfus/dune-awakening-helm-chart.git
    targetRevision: v0.2.6
    path: .
    helm:
      releaseName: dune
      valuesObject:
        images:
          registry: registry.example.com/dune-awakening-images
        storage:
          class: your-storage-class
        credentials:
          existingSecret: dune-credentials
  destination:
    server: https://kubernetes.default.svc
    namespace: my-game
  syncPolicy:
    syncOptions:
      - CreateNamespace=true
```

Apply the Application and perform a full sync:

```bash
kubectl apply -f application.yaml
argocd app sync dune
```

Argo CD passes the destination namespace to Helm as `.Release.Namespace`. A full
sync runs the Sync hook; selective syncs skip hooks. Existing Argo CD sync waves
and CRD server-side-apply/prune protections are retained. To upgrade, update
`targetRevision` to the next release tag, review any required values or CRD
changes, apply the Application and perform another full sync.

For an existing Kustomize installation, back up the credentials Secret and database,
keep the namespace and world ID unchanged, and switch the existing Argo CD
Application's source to this chart repository. Preserve its name so resource
tracking continues; use the same release name. Keep the existing SealedSecret
managed separately and prevent it being pruned during the source switch. Do not run
two Applications managing the same operators. When switching to Helm CLI instead,
existing resources require an explicit ownership handover; a direct install will
reject resources without Helm ownership metadata.

