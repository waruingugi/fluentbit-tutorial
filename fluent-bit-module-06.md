# Module 6 — Routing & Multiple Outputs

> **Hands-on rule:** type every command. **Run everything from the project root `~/fluent-bit-django`.**
>
> **Environment:** macOS + Docker Desktop.
>
> **Scope (~45–60 min, the last hands-on module):** Make tags earn their keep. So far one `stdout` output has swept up everything. Now you'll send different records to different places — and the *same* record to several places at once — using nothing but tags and `match`. This is the mechanism behind "write to any backend, or two at once."

---

## Chunk 1 — The router you've been using all along

There's a stage between your filters and your outputs that's never had a name in this course: the **router**. After a record clears the filters, the router offers it to *every* output you've defined. Each output independently decides whether to accept it by comparing the record's **tag** against its own **`match`**.

Two consequences fall out of that, and they're the whole module:

- An output takes a record **only if** its `match` fits the tag. Tag-and-match is how you say "these logs go here, those go there."
- A single record can match **several** outputs at once, so it gets sent to all of them. This is **fan-out** — one log, many destinations.

You've been seeing the trivial case: one output with `match: 'django.*'` catching all three tags. The three words from Module 1 — *record, tag, match* — now become a routing system. Tags (`django.app`, `django.access`, `django.error`) aren't decoration; they're addresses.

Nothing in the app, parsers, or filters changes this module. We only touch the `outputs:` list.

---

## Chunk 2 — Match patterns in depth

`match` is a simple wildcard, where `*` stands for "any characters, including dots." That gives you a small but complete vocabulary:

| Pattern | Matches | Use |
|---|---|---|
| `*` | every record | catch-all |
| `django.*` | `django.app`, `django.access`, `django.error` | a whole family by prefix |
| `django.error` | only that exact tag | one specific stream |
| `match_regex: '^django\.(app|error)$'` | app and error, not access | precise multi-tag selection |

This is exactly why we tagged logs with dots from the start. A dotted tag is a little hierarchy: `django.error` is a child of the `django.*` family. That structure is what lets one output grab the whole family while another grabs a single member — which is what we're about to do.

---

## Chunk 3 — Fan-out #1: errors to their own destination

Errors deserve special handling — you might want them in a separate place for fast access during an incident. Add a second output that catches *only* error records, writing them to a file (a stand-in for "a dedicated destination"):

```yaml
  outputs:
    - name: stdout
      match: 'django.*'
      format: json_lines

    - name: file
      match: 'django.error'
      path: /logs
      file: errors.out
```

The `file` output writes to `path/file` — here `/logs/errors.out`, which lands in your shared `./logs` folder so you can read it on your Mac. Crucially its `match` is `django.error`, the *exact* tag, so only tracebacks reach it. Run and trigger one of each:

```bash
rm -f logs/*
docker compose up -d --build
curl -s localhost:8000 >/dev/null
curl -s localhost:8000/boom >/dev/null
cat logs/errors.out
```

```
django.error: [1750238475.300000, {"log":"2026-06-18 10:01:15,300 ERROR errors failed to handle /boom\n...ZeroDivisionError: division by zero","service":"django","env":"dev"}]
```

Only the error landed in `errors.out` — the `/` request didn't, because its tag `django.app` doesn't match `django.error`. (The default `file` format prefixes the tag and timestamp; it's configurable via `format:`, but the routing is the point here, not the serialization.)

---

## Chunk 4 — Fan-out #2: everything to a "backend"

Now add a third output that takes *everything* — the stand-in for your real log store:

```yaml
    - name: file
      match: 'django.*'
      path: /logs
      file: all.out
```

> **Predict before you run:** with all three outputs in place, a single `/boom` error record will reach how many of them — `stdout`, `errors.out`, `all.out`?

```bash
rm -f logs/*
docker compose up -d
curl -s localhost:8000 >/dev/null
curl -s localhost:8000/boom >/dev/null
echo "--- errors.out ---" ; cat logs/errors.out
echo "--- all.out ---"    ; cat logs/all.out
docker compose logs fluent-bit | tail -5
```

Trace where each record went:

- The **error** record (tag `django.error`) matched all three — it's in `stdout`, `errors.out`, *and* `all.out`. **One record, three destinations.**
- The **app** record (tag `django.app`) matched `stdout` and `all.out`, but not `errors.out`.

That's fan-out working: the router copied each record to every output whose `match` accepted its tag. You didn't duplicate any data by hand — you described *where things go* with tags, and the router did the rest.

(One quiet safeguard: our `tail` inputs use explicit paths — `app.log`, `access.log`, `error.log` — not a glob, so Fluent Bit never tries to tail `errors.out` or `all.out` and create a feedback loop. If you ever tail `/logs/*.log`, keep output files out of that pattern.)

---

## Chunk 5 — Why this is the whole point: dual-write

Look again at the `all.out` output. It's a `file` only because we haven't stood up a real backend. Sending to **Elasticsearch** instead is the same output block with the plugin swapped:

```yaml
    - name: es
      match: 'django.*'
      host: elasticsearch
      port: 9200
      index: django-logs
```

And here's the move from your architecture decision — writing to Elasticsearch **and** Loki at the same time is just *two* output blocks matching the same tag:

```yaml
    - name: es
      match: 'django.*'
      host: elasticsearch
      port: 9200
      index: django-logs

    - name: loki
      match: 'django.*'
      host: loki
      port: 3100
      labels: service=django, env=dev
```

This is exactly the "portable edges, swappable core" principle made real. The router already fans a record out to every matching output — so dual-writing during a backend migration costs you one extra output block, and nothing above it (inputs, parsers, filters) changes at all. Cut over by deleting the old block once you've validated the new one. (Actually standing up Elasticsearch or Loki is the appendix — Modules 7–8 — but the routing that makes it trivial is what you just built.)

---

## Chunk 6 — The complete pipeline

This is the full config at the end of the hands-on course — every stage you've built, end to end:

```yaml
service:
  flush: 1
  log_level: info

parsers:
  - name: app_json
    format: json
  - name: django_access
    format: regex
    regex: '^(?<time>\S+ \S+) (?<level>\w+) (?<logger>[\w.]+) "(?<method>\w+) (?<path>\S+) (?<protocol>[^"]+)" (?<status>\d+) (?<size>\d+)$'

multiline_parsers:
  - name: django_multiline
    type: regex
    flush_timeout: 1000
    rules:
      - state: start_state
        regex: '/^\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}/'
        next_state: cont
      - state: cont
        regex: '/^(?!\d{4}-\d{2}-\d{2} ).+/'
        next_state: cont

pipeline:
  inputs:
    - name: tail
      path: /logs/app.log
      tag: django.app
      parser: app_json
      read_from_head: true

    - name: tail
      path: /logs/access.log
      tag: django.access
      parser: django_access
      read_from_head: true

    - name: tail
      path: /logs/error.log
      tag: django.error
      multiline.parser: django_multiline
      read_from_head: true

  filters:
    - name: modify
      match: 'django.*'
      add:
        - service django
        - env dev

    - name: grep
      match: 'django.access'
      exclude: 'path ^/health$'

    - name: modify
      match: 'django.app'
      remove: user_email

    - name: lua
      match: 'django.access'
      call: add_status_class
      code: |
        function add_status_class(tag, timestamp, record)
            local s = tonumber(record["status"])
            if s == nil then return 0, timestamp, record end
            if s >= 500 then record["status_class"] = "5xx"
            elseif s >= 400 then record["status_class"] = "4xx"
            elseif s >= 200 then record["status_class"] = "2xx"
            else record["status_class"] = "other" end
            return 1, timestamp, record
        end

    - name: nest
      match: 'django.access'
      operation: nest
      wildcard:
        - method
        - path
        - protocol
        - status
        - size
      nest_under: http

  outputs:
    - name: stdout
      match: 'django.*'
      format: json_lines

    - name: file
      match: 'django.error'
      path: /logs
      file: errors.out

    - name: file
      match: 'django.*'
      path: /logs
      file: all.out
```

Read top to bottom, it's the entire course: three sources tailed and parsed three different ways, reshaped by a filter chain, then routed to three destinations by tag.

---

## Chunk 7 — Cheat sheet

| Goal | Config |
|---|---|
| Match everything | `match: '*'` |
| Match a tag family | `match: 'django.*'` |
| Match one exact tag | `match: 'django.error'` |
| Match by regex | `match_regex: '^django\.(app\|error)$'` |
| Write records to a file | output `file` with `path:` + `file:` |
| Send a stream to its own place | a second output with a narrower `match` |
| Send one record to many places | several outputs whose `match` all accept its tag |
| Dual-write to two backends | two outputs (`es`, `loki`) with the same `match` |

Mental model: **the router copies each record to every output whose `match` fits its tag.** Routing is a property of tags, not of data duplication — you declare destinations, Fluent Bit delivers.

---

## Chunk 8 — Checkpoint challenges

From memory.

**Challenge A — route a single stream**
1. Add a fourth output that writes *only* access records to `/logs/access.out`.
2. Hit `/` and `/boom`, then show that `access.out` contains the `/` request but not the error.

**Challenge B — reason about fan-out**
1. Without running anything, list which of the four outputs (`stdout`, `errors.out`, `all.out`, `access.out`) a single `django.error` record reaches, and which a single `django.access` record reaches.
2. Run it and confirm your predictions by inspecting the files.

**Bonus question (mental model):** Sending logs to Elasticsearch and Loki simultaneously requires no change to your inputs, parsers, or filters — only outputs. In one sentence, why does the router's design make dual-writing (and therefore a zero-downtime backend migration) almost free?

---

*End of Module 6 — and the end of the hands-on day. You can now collect Django logs from multiple sources, parse each into structure, reassemble multi-line tracebacks, reshape records in flight, and route them anywhere — including two backends at once. What remains (Modules 7–8: buffering & reliability, and shipping to a real backend as a production DaemonSet) is the read-only appendix: the concepts that turn this working pipeline into a durable, production-grade one.*
