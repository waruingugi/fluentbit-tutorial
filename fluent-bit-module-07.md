# Module 7 — Buffering & Reliability *(Appendix — read first, build if curious)*

> **This is an appendix module.** The hands-on day ended at Module 6. Here the goal shifts from "type every command" to "understand it well enough to reason about it." There's one optional experiment near the end; everything else is conceptual, to be built for real when you take this pipeline to production.
>
> **Environment:** macOS + Docker Desktop.

---

## Chunk 1 — The question reliability answers

Everything in Modules 1–6 assumed the output just works. Reality: backends go down, networks blip, Elasticsearch gets slow under load. Meanwhile your inputs never stop — Django keeps writing logs whether or not Loki is reachable. So there's a gap between "produced" and "delivered," and the entire subject of reliability is: **where do records live in that gap, and what happens when the gap gets large?**

Fluent Bit holds undelivered records in a **buffer**, organized into **chunks**. When an output can't keep up, chunks pile up. That pile-up forces a decision, and how you've configured Fluent Bit decides whether the outcome is *lost data*, *paused ingestion*, or *graceful spill to disk*. Three settings govern it: where the buffer lives, how big it's allowed to get, and how hard Fluent Bit tries to deliver.

---

## Chunk 2 — Where the buffer lives: memory vs filesystem

Every input buffers its chunks somewhere, set by `storage.type`:

- **`memory`** (the default) — chunks live in RAM. Fast and simple, but two limits: the buffer is bounded by available memory, and **a crash or restart loses whatever was in flight**.
- **`filesystem`** — chunks are also written to disk. They survive a restart, and you can buffer far more than RAM holds, at the cost of some disk I/O.

Filesystem storage is configured in two places — a `storage.path` in the service section, and `storage.type: filesystem` on the input:

```yaml
service:
  flush: 1
  log_level: info
  storage.path: /var/log/flb-storage/
  storage.sync: normal
  storage.max_chunks_up: 128

pipeline:
  inputs:
    - name: tail
      path: /logs/app.log
      tag: django.app
      parser: app_json
      storage.type: filesystem
```

The trade-off in one line: **memory is fast but forgets; filesystem is durable but touches disk.** For a backend that's occasionally slow or briefly down, filesystem storage is what stops a hiccup from becoming data loss.

---

## Chunk 3 — Backpressure: what happens when the buffer fills

A buffer can't grow forever — unbounded growth is just an out-of-memory crash with extra steps. So you cap it, and the cap creates **backpressure**: when the buffer is full, Fluent Bit *pauses the input* until the backlog drains, rather than consuming more memory.

- With **memory** storage, `mem_buf_limit` caps how much an input's un-flushed data may occupy. Hit the limit and that input pauses.
- With **filesystem** storage, `storage.max_chunks_up` caps how many chunks stay *in memory* at once; the rest spill to disk, so the input can usually keep reading. `storage.pause_on_chunks_overlimit` lets you choose to pause instead.

Here's the part that ties back to Module 2: for a `tail` input, **the log file itself is a durable buffer.** If Fluent Bit pauses or restarts, the file is still there, and the offset database (`db:`, the thing we flagged in Module 2) lets `tail` resume exactly where it stopped. So for file-based inputs, backpressure usually means "slow down and catch up later," not "lose data" — as long as the source file isn't deleted or rotated away before Fluent Bit gets back to it.

---

## Chunk 4 — Retries: how hard Fluent Bit tries to deliver

When an output fails to deliver a chunk, Fluent Bit doesn't discard it immediately — it **retries**, with exponential backoff. The policy is per output:

```yaml
  outputs:
    - name: loki
      match: 'django.*'
      host: loki
      port: 3100
      retry_limit: 5
```

`retry_limit` is the number of attempts before Fluent Bit gives up on a chunk and drops it. Set it to `no_limits` for infinite retries — which guarantees no delivery-failure drops, but means a permanently dead backend will grow your backlog forever (and eventually trigger backpressure). The choice is a genuine trade-off: *bounded retries risk dropping data; unbounded retries risk filling your buffer.* Filesystem storage buys you room to choose unbounded safely.

---

## Chunk 5 — The whole reliability picture

Put the pieces together and "don't lose logs" has a concrete recipe:

1. **`storage.type: filesystem`** — survive restarts and crashes.
2. **`db:` on every `tail` input** — resume reading from the exact offset after any pause or restart.
3. **A sane `retry_limit`** — usually generous or unlimited, given filesystem storage absorbs the backlog.
4. **Enough disk** for the worst realistic outage.

And the honest list of where loss can *still* happen, so you're not falsely reassured:

- The source file is **rotated or deleted** before Fluent Bit reads it (move faster, or keep more history at the source).
- A **bounded `retry_limit`** is exhausted during a long outage.
- The **disk fills** while buffering a very long outage.

Reliability isn't a single switch — it's making each of those failure modes unlikely enough for your tolerance.

---

## Chunk 6 — Observing the observer

You can't trust a pipeline you can't see. Fluent Bit exposes its own internals over HTTP — turn it on in the service section:

```yaml
service:
  flush: 1
  http_server: on
  http_listen: 0.0.0.0
  http_port: 2020
  storage.metrics: on
```

The useful endpoints on port 2020:

- `/api/v1/metrics` — per-plugin counters: records in, records out, **retries**, **retries_failed**, dropped. The numbers that tell you whether logs are actually getting through.
- `/api/v1/storage` — buffer and chunk stats: how many chunks are queued, how much is sitting on disk.
- `/api/v1/health` and `/api/v1/uptime` — liveness.
- `/api/v1/metrics/prometheus` — the same metrics in Prometheus format, for when you wire Fluent Bit into the monitoring stack later.

The signals to watch: **retries climbing** means an output is struggling; **retries_failed or dropped above zero** means you're losing data; **a growing storage backlog** means you're buffering faster than you're delivering.

### Optional experiment — watch a failure and recovery

If you want to *see* retries happen, expose the monitoring port and add a deliberately broken output. In `docker-compose.yml`, add to the `fluent-bit` service:

```yaml
    ports:
      - "2020:2020"
```

Add an output pointing at an unreachable address, with filesystem storage on the inputs (Chunk 2):

```yaml
    - name: http
      match: 'django.*'
      host: 198.51.100.1
      port: 9999
      retry_limit: 3
```

Then generate traffic and watch the counters climb:

```bash
docker compose up -d --build
curl -s localhost:8000 >/dev/null
curl -s localhost:2020/api/v1/metrics
```

You'll see the `http` output's retry count rising and, after three failed attempts, dropped records — while `stdout` keeps working fine. That's the reliability machinery in motion: one failing destination retrying and eventually shedding load, without taking the healthy outputs down with it. Remove the broken output when you're done.

---

## Chunk 7 — Quick reference

| Concern | Setting |
|---|---|
| Survive restart/crash | `storage.type: filesystem` + service `storage.path` |
| Resume reading a file | `db:` on the `tail` input |
| Cap memory (memory buffer) | `mem_buf_limit` on the input |
| Cap in-memory chunks (fs buffer) | `storage.max_chunks_up` (service) |
| Pause input when full | backpressure (automatic at the limit) |
| Delivery attempts before drop | `retry_limit` on the output (`no_limits` = infinite) |
| Turn on self-monitoring | `http_server: on`, `http_port: 2020` |
| See in/out/retries/dropped | `GET /api/v1/metrics` |
| See buffer backlog | `GET /api/v1/storage` |

Mental model: **records sit in a buffer between produced and delivered; reliability is choosing what happens when that buffer fills — drop, pause, or spill to disk — and watching the metrics to know which is happening.**

---

## Chunk 8 — Check your understanding

No build required — just reason it through.

1. Your backend is down for ten minutes. With `storage.type: memory` and `retry_limit: no_limits`, what's the risk? With `storage.type: filesystem`, how does that risk change?
2. A `tail` input pauses under backpressure for two minutes, then resumes. Why does it lose no data — and which single setting makes that true?
3. You add Loki as a second output and its `retries` counter climbs while `stdout`'s stays at zero. What does that tell you, and is any data lost yet?

**Mental-model question:** For a `tail` input, the source log file already persists on disk. In one sentence, why does that make file-based log collection inherently more forgiving than, say, receiving logs over a network socket?

---

*End of Module 7. Next: Module 8 (appendix) — Shipping for Real & Deployment Shape, where the pipeline leaves your laptop: a real backend instead of `stdout`, and Fluent Bit deployed the way it actually runs in production — as a Kubernetes DaemonSet with its config in a ConfigMap.*
