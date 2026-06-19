# Module 5 — Filtering & Transforming

> **Hands-on rule:** type every command. **Run everything from the project root `~/fluent-bit-django`.**
>
> **Environment:** macOS + Docker Desktop.
>
> **Scope (~75 min, the meatiest module):** Stop letting records flow through untouched. You'll add context fields, drop noise, redact secrets, restructure shape, and run custom logic — all *in flight*, before anything leaves Fluent Bit. Five filters, introduced one at a time, then assembled into one ordered chain.

---

## Chunk 1 — Where filters fit, and what they do

So far your pipeline is `input → parse → output`. A **filter** is a stage that sits *between* parse and output:

> `input → parse → [filter → filter → filter] → output`

Two things to internalize before any config:

- **Filters are matched by tag**, exactly like outputs. A filter with `match: 'django.access'` only touches access records and ignores the rest.
- **Filters run in the order you list them**, top to bottom. A record passes through each matching filter in sequence, so order is a real decision (Chunk 7).

Filters do one of two jobs: **transform** a record (add, rename, remove, restructure fields) or **drop** it entirely (discard records you don't want stored). We'll do both.

The Django app needs two new things to make these lessons concrete — a noisy health endpoint and a log line carrying something sensitive. **The `LOGGING` dict is unchanged from Module 4**; only the views change:

```python
logger = logging.getLogger("app")
err_logger = logging.getLogger("errors")


def index(request):
    logger.info(
        "handled request to /",
        extra={"client_ip": "203.0.113.5", "user_email": "user@example.com"},
    )
    return JsonResponse({"message": "hello from django"})


def health(request):
    return JsonResponse({"status": "ok"})


def boom(request):
    try:
        result = 1 / 0
    except Exception:
        err_logger.exception("failed to handle /boom")
    return JsonResponse({"detail": "an error was logged"}, status=500)


urlpatterns = [
    path("", index),
    path("health", health),
    path("boom", boom),
]
```

The `extra={...}` on `index` adds `client_ip` and `user_email` straight into the JSON app log (python-json-logger merges `extra` fields in) — `user_email` is our stand-in for "data that must not leave." The `/health` endpoint is our stand-in for a load-balancer probe hammering the app with useless 200s.

Filters live in a `filters:` list under `pipeline:`, alongside `inputs:` and `outputs:`. We'll add to that list one filter at a time.

---

## Chunk 2 — Add context with `modify`

When logs from many services land in one backend, every record needs to say where it came from. The `modify` filter is the workhorse for field edits — here, adding two fields to *every* record:

```yaml
    filters:
      - name: modify
        match: 'django.*'
        add:
          - service django
          - env dev
```

Each `add` entry is `key value`. `match: 'django.*'` means every record from every input gets stamped. Add this under `pipeline:` (between `inputs:` and `outputs:`), then run:

```bash
rm -f logs/*.log
docker compose up -d --build
curl -s localhost:8000 ; echo
docker compose logs fluent-bit
```

```
fluent-bit-1  | {"date":...,"time":"...","level":"INFO","logger":"app","message":"handled request to /","client_ip":"203.0.113.5","user_email":"user@example.com","service":"django","env":"dev"}
```

Every record now carries `service` and `env`. A lighter-weight alternative for pure additions is the `record_modifier` filter (`record: - service django`), which is faster but does less — use `modify` when you also need to rename, conditionally set, or remove fields, which we're about to do.

---

## Chunk 3 — Drop noise with `grep`

Hit the health endpoint a few times and watch the access log fill with junk:

```bash
curl -s localhost:8000/health >/dev/null
curl -s localhost:8000/health >/dev/null
docker compose logs fluent-bit | grep health
```

```
fluent-bit-1  | {"date":...,"logger":"django.server","method":"GET","path":"/health","protocol":"HTTP/1.1","status":"200","size":"15",...}
```

Probe traffic like this can be 90% of your access logs and tells you nothing. `grep` drops records by field pattern. Add it:

```yaml
      - name: grep
        match: 'django.access'
        exclude: 'path ^/health$'
```

`exclude: 'path ^/health$'` reads as "drop any record whose `path` field matches `^/health$`." Its inverse is `regex: 'FIELD PATTERN'`, which *keeps only* matching records. Re-run, hit `/health` again, and those records are gone from the output — discarded before they ever reach storage. Note the `match` is `django.access`: `grep` only inspects access records, because `path` is a field only they have.

---

## Chunk 4 — Redact secrets with `modify`

Your app log is still shipping `user_email` — exactly the kind of field that shouldn't leave the building. `modify` removes it:

```yaml
      - name: modify
        match: 'django.app'
        remove: user_email
```

Re-run, hit `/`, and check the app record:

```
fluent-bit-1  | {"date":...,"logger":"app","message":"handled request to /","client_ip":"203.0.113.5","service":"django","env":"dev"}
```

`user_email` is gone. If you'd rather *mask* than delete — keep the field but blank its value — use `set` instead, which overwrites: `set: 'user_email REDACTED'`. One caution worth stating plainly: redacting at the collector is a **safety net, not a license to log secrets**. The real fix is the app not logging them; this filter is the guardrail for when something slips through.

---

## Chunk 5 — Restructure with `nest` (and its inverse, `lift`)

Sometimes the backend wants a tidier shape — all the HTTP fields grouped under one key instead of scattered at the top level. `nest` does that:

```yaml
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
```

This moves the listed fields into a sub-object:

```json
{"logger":"django.server","service":"django","env":"dev",
 "http":{"method":"GET","path":"/","protocol":"HTTP/1.1","status":"200","size":"33"}}
```

The inverse filter is `lift` (`operation: lift`, `nested_under: http`), which flattens a sub-object back up to the top level — handy when a source sends you nested data your backend wants flat. You won't reach for these daily, but recognize them: `nest` groups, `lift` flattens.

---

## Chunk 6 — Custom logic with `lua`

When no built-in filter does what you need, `lua` is the escape hatch — a small script that runs per record. A common, useful case: bucket the HTTP status into a class (`2xx`, `4xx`, `5xx`) so dashboards and alerts (Module 6 and beyond) can group by it without parsing numbers every time.

```yaml
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
```

The function receives `(tag, timestamp, record)` and returns `(code, timestamp, record)`, where `code` is `1` for "I changed the record, use the new one," `0` for "leave it unchanged," and `-1` for "drop it." Here it reads the string `status`, converts it with `tonumber`, and adds a `status_class` field. (Prefer a separate file via `script: myfilters.lua` once your logic outgrows a few lines; inline `code` is fine for this.) One caveat: Lua runs for *every matching record*, so keep it lean — it's the most expensive filter.

---

## Chunk 7 — Order matters (and the full filter chain)

Because filters run top to bottom, sequence changes behavior:

- `grep` to drop noise should come **early** — no point transforming records you're about to discard.
- The `lua` status-class filter must run **before** `nest`. After `nest`, `status` lives at `record["http"]["status"]`, not `record["status"]`, so `tonumber(record["status"])` would return `nil` and the class would never be set. This is the single most common "my filter silently did nothing" cause: a field moved before the filter that needed it.

Here's the complete pipeline, filters in a deliberate order:

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
    - name: modify          # 1. stamp context onto everything
      match: 'django.*'
      add:
        - service django
        - env dev

    - name: grep            # 2. drop probe noise early
      match: 'django.access'
      exclude: 'path ^/health$'

    - name: modify          # 3. redact secrets from app logs
      match: 'django.app'
      remove: user_email

    - name: lua             # 4. derive status_class BEFORE nesting
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

    - name: nest            # 5. group HTTP fields last
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
```

Run the whole thing and exercise every endpoint:

```bash
rm -f logs/*.log
docker compose up -d --build
curl -s localhost:8000 >/dev/null
curl -s localhost:8000/health >/dev/null
curl -s localhost:8000/boom >/dev/null
docker compose logs fluent-bit
```

You should see: app records carrying `service`/`env` with `user_email` removed; access records for `/` carrying `status_class` and a nested `http` object; no `/health` records at all; and the error traceback still arriving as one multiline record with `service`/`env` added.

---

## Chunk 8 — Cheat sheet

| Goal | Filter |
|---|---|
| Add fields to records | `modify` with `add: - key value` |
| Add fields (lean, add/remove only) | `record_modifier` with `record:` |
| Remove a field (redact) | `modify` with `remove: field` |
| Mask a field (overwrite value) | `modify` with `set: 'field VALUE'` |
| Keep only matching records | `grep` with `regex: 'FIELD PATTERN'` |
| Drop matching records | `grep` with `exclude: 'FIELD PATTERN'` |
| Group fields under a key | `nest` (`operation: nest`, `nest_under:`) |
| Flatten a sub-object | `nest` filter (`operation: lift`, `nested_under:`) |
| Custom per-record logic | `lua` with `code:`/`script:` + `call:` |
| Where filters go | `filters:` list under `pipeline:` |

Two rules to remember: **filters are tag-matched** (like outputs), and **they run in order** (so a field must exist *before* the filter that reads it).

---

## Chunk 9 — Checkpoint challenges

From memory.

**Challenge A — transform and verify**
1. Add a third context field, `team: platform`, to every record.
2. Confirm a `/` request shows `service`, `env`, and `team`, and that `user_email` is absent.

**Challenge B — break it with order (the real lesson)**
1. Move the `nest` filter *above* the `lua` filter in the chain.
2. Re-run, hit `/`, and inspect an access record. Is `status_class` present? Explain precisely why or why not.
3. Put the order back and confirm `status_class` returns.

**Bonus question (mental model):** A filter with `match: 'django.access'` and a filter with `match: 'django.app'` both exist in the same chain. In one sentence, what decides which records each one touches — and why can `grep`'s `exclude: 'path ...'` only ever work on the access records?

---

*End of Module 5. Next: Module 6 — Routing & Multiple Outputs, the last hands-on module, where tags and `match` stop being a detail and become the steering wheel: same logs, sent to different destinations at once.*
