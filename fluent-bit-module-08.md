# Module 8 — Shipping for Real & Deployment Shape *(Appendix — read first, build on a real cluster)*

> **This is the final appendix module and the course capstone.** It takes the pipeline off your laptop in two leaps. There's an optional backend experiment; the Kubernetes half is conceptual, to be built when you have a cluster — and, as you'll see at the end, mostly generated for you in practice.
>
> **Environment:** macOS + Docker Desktop for the optional backend bit; a Kubernetes cluster for the deployment shape.

---

## Chunk 1 — The two leaps to production

Everything you built works, but it lives on your laptop, prints to `stdout`, and runs under Compose. Production needs two changes:

1. **A real destination** instead of `stdout` — Elasticsearch now, Loki later, per your architecture decision.
2. **A real deployment shape** instead of one Compose container tailing a shared file — Fluent Bit running as a Kubernetes DaemonSet, collecting every pod's logs on every node.

The good news, earned across six modules: **everything between input and output stays the same.** Parsers, multiline, filters, routing — all unchanged. Only the outputs and the way Fluent Bit is *deployed* change. That's the portable-edges principle paying off.

---

## Chunk 2 — Swap stdout for Elasticsearch

Sending to Elasticsearch is the same kind of output block you've been writing, with the `es` plugin:

```yaml
  outputs:
    - name: es
      match: 'django.*'
      host: elasticsearch
      port: 9200
      index: django-logs
      suppress_type_name: on
      http_user: elastic
      http_passwd: ${ES_PASSWORD}
      tls: on
```

The shape is identical to the `file` and `stdout` outputs — `match` still routes by tag. The new keys are backend specifics: where Elasticsearch lives, the `index` to write into, auth (`http_user`/`http_passwd`, with the password pulled from an env var, never hard-coded), and `tls`. `suppress_type_name: on` is the one Elasticsearch-8 quirk worth remembering — ES 8 removed mapping types, and this flag stops Fluent Bit sending the old `_type` field.

### Optional experiment — see logs land in Elasticsearch

If you want to watch it work, add a single-node Elasticsearch to `docker-compose.yml` (note: it's a memory-hungry JVM — give Docker Desktop a few GB):

```yaml
  elasticsearch:
    image: docker.elastic.co/elasticsearch/elasticsearch:8.13.4
    environment:
      - discovery.type=single-node
      - xpack.security.enabled=false
      - ES_JAVA_OPTS=-Xms512m -Xmx512m
    ports:
      - "9200:9200"
```

Point the `es` output at `host: elasticsearch`, `port: 9200`, `tls: off`, drop the auth lines (security disabled), then after generating traffic:

```bash
curl -s 'localhost:9200/django-logs/_search?pretty&size=2'
```

You'll see your Django records stored as Elasticsearch documents — the same records, now in a real searchable store instead of scrolling past in your terminal.

---

## Chunk 3 — Elastic *and* Loki: the dual-write

Here's the migration move from your architecture brief. Writing to Loki as well is one more output block matching the same tag:

```yaml
    - name: loki
      match: 'django.*'
      host: loki
      port: 3100
      labels: job=fluentbit, service=django, env=dev
      label_keys: $level
```

Both `es` and `loki` blocks present = **dual-write**: every record fans out to both backends (the router from Module 6 doing its job). That's how you migrate with zero downtime — run both, validate Loki, then delete the `es` block.

The one Loki-specific discipline to carry from our very first conversation: **`labels` and `label_keys` must stay low-cardinality.** `service`, `env`, `level` are safe — a handful of values each. Never promote a request ID, user ID, or anything unbounded to a Loki label; that explodes Loki's stream count and destabilizes it regardless of volume. High-cardinality data stays *in the log line*, searched at query time with LogQL. This is the single rule that decides whether Loki behaves.

---

## Chunk 4 — The deployment leap: who writes the file

On your laptop, the chain was: **Django writes a file → Fluent Bit tails it** (sharing a volume). In Kubernetes it's subtly different and important:

> Apps log to **stdout** → the **container runtime** writes those streams to files under `/var/log/containers/*.log` on each node → a Fluent Bit **DaemonSet** tails those node files.

Your `tail` skill transfers exactly — Fluent Bit still reads lines from files. What changed is *who writes them*: not the app, but the runtime. And the runtime wraps each line in the **CRI envelope** you met in the very first lesson:

```
2026-06-18T09:14:02.118Z stdout F {"level":"INFO","logger":"app","message":"handled request to /"}
```

So the production tail input strips that wrapper with the built-in **`cri`** parser before anything else happens:

```yaml
    - name: tail
      path: /var/log/containers/*.log
      multiline.parser: cri
      tag: kube.*
      read_from_head: true
```

That closes the loop on the pipeline's first principle: apps print to stdout, and collection is someone else's problem — in production, the node's problem, picked up by one Fluent Bit per node.

---

## Chunk 5 — Fluent Bit as a DaemonSet

A **DaemonSet** runs exactly one pod on every node. That's the right shape because logs are a *per-node* resource — each node has its own `/var/log/containers`, so each node needs its own collector. The pod mounts the node's log directory via `hostPath`, and gets its config from a **ConfigMap**.

A representative (trimmed) DaemonSet:

```yaml
apiVersion: apps/v1
kind: DaemonSet
metadata:
  name: fluent-bit
  namespace: logging
spec:
  selector:
    matchLabels: { app: fluent-bit }
  template:
    metadata:
      labels: { app: fluent-bit }
    spec:
      serviceAccountName: fluent-bit
      containers:
        - name: fluent-bit
          image: fluent/fluent-bit:3.2
          volumeMounts:
            - name: varlog
              mountPath: /var/log
              readOnly: true
            - name: config
              mountPath: /fluent-bit/etc/
      volumes:
        - name: varlog
          hostPath: { path: /var/log }
        - name: config
          configMap: { name: fluent-bit-config }
```

The two mounts are the whole story: `varlog` (the node's logs, read-only) is *what it reads*, and `config` (the ConfigMap) is *how it behaves*. The ConfigMap simply holds the `fluent-bit.yaml` you've been editing all course — same file, now delivered to every node's pod.

---

## Chunk 6 — Automatic metadata: the `kubernetes` filter + RBAC

On your laptop you stamped context by hand (`modify` adding `service`/`env`). In Kubernetes there's a purpose-built filter that does it automatically and far more richly — the **`kubernetes`** filter:

```yaml
    - name: kubernetes
      match: 'kube.*'
      merge_log: on
      k8s-logging.parser: on
```

For each log line, it works out which pod produced it and calls the Kubernetes API to attach that pod's metadata: namespace, pod name, labels, annotations, container name, node. This is what turns a raw line into something you can slice by `namespace` or `app` in Grafana — the production equivalent of the `service`/`env` fields, except discovered, not hard-coded.

Because it queries the API, Fluent Bit needs permission. That's a small **RBAC** bundle: a `ServiceAccount` (referenced by the DaemonSet above), a `ClusterRole` granting read access to pods and namespaces, and a `ClusterRoleBinding` tying them together:

```yaml
apiVersion: rbac.authorization.k8s.io/v1
kind: ClusterRole
metadata:
  name: fluent-bit-read
rules:
  - apiGroups: [""]
    resources: ["pods", "namespaces"]
    verbs: ["get", "list", "watch"]
```

(Plus the matching `ServiceAccount` and `ClusterRoleBinding`.) Without it, the `kubernetes` filter can't read metadata and your logs arrive anonymous.

---

## Chunk 7 — The production config, in shape

Stitching the production pieces together, the ConfigMap's `fluent-bit.yaml` has the same skeleton you know — only the input source, one filter, and the outputs differ from your laptop version:

```yaml
service:
  flush: 1
  log_level: info
  storage.path: /var/log/flb-storage/

pipeline:
  inputs:
    - name: tail
      path: /var/log/containers/*.log
      multiline.parser: cri
      tag: kube.*
      storage.type: filesystem
      read_from_head: true

  filters:
    - name: kubernetes
      match: 'kube.*'
      merge_log: on

  outputs:
    - name: es
      match: 'kube.*'
      host: elasticsearch
      port: 9200
      index: app-logs
      suppress_type_name: on

    - name: loki
      match: 'kube.*'
      host: loki
      port: 3100
      labels: job=fluentbit
      label_keys: $kubernetes['namespace_name']
```

Same five-stage pipeline. The `tail` now reads node container logs with the `cri` parser, the `kubernetes` filter replaces your hand-rolled context, and the outputs point at real backends — with `storage.type: filesystem` (Module 7) for durability. Everything you learned maps straight across.

---

## Chunk 8 — Quick reference

| Goal | Config |
|---|---|
| Ship to Elasticsearch | output `es` (`host`, `port`, `index`, `suppress_type_name`) |
| Ship to Loki | output `loki` (`host`, `port`, `labels`, `label_keys`) |
| Dual-write | both `es` and `loki` outputs, same `match` |
| Tail node container logs | `path: /var/log/containers/*.log` |
| Unwrap the CRI envelope | `multiline.parser: cri` |
| Auto-add pod/namespace metadata | `kubernetes` filter (needs RBAC) |
| One collector per node | `kind: DaemonSet` |
| Config delivery | a `ConfigMap` holding `fluent-bit.yaml` |
| API access for metadata | ServiceAccount + ClusterRole + binding |

---

## Chunk 9 — A real-world shortcut, and where to go next

One honest note before you hand-write any of those manifests: **you usually don't.** The Fluent Bit Helm chart generates the DaemonSet, ConfigMap, ServiceAccount, and RBAC for you — you supply values (image, the `fluent-bit.yaml` config, the outputs) and it produces the rest. Knowing what the chart builds, which is what this module showed you, is exactly what lets you configure it sensibly rather than blindly.

That points naturally at the rest of the journey:

- **Loki + Grafana** — you now *produce* logs into Loki; the next step is querying them in LogQL and building dashboards in Grafana. That's where these logs become answers.
- **Prometheus** — metrics alongside logs, the other half of observability (and Fluent Bit's `/api/v1/metrics/prometheus` from Module 7 plugs straight in).
- **Helm** — the tool that deploys all of the above without hand-written manifests.

---

*End of Module 8, and the end of the course. You started by watching a fake record move through a pipe, and you finish able to collect real application logs from every node in a cluster, parse each source into structure, reassemble tracebacks, reshape and redact records in flight, route them to two backends at once, buffer them durably through an outage, and deploy the whole thing the way production actually runs it. The pipe never changed — input, parse, filter, buffer, output. You just learned every stage.*
