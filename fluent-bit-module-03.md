# Module 3 — Parsing into Structure

> **Hands-on rule:** type every command. **Run everything from the project root `~/fluent-bit-django`.**
>
> **Environment:** macOS + Docker Desktop.
>
> **Scope (~60 min):** Turn the opaque `log` string from Module 2 into real fields — `level`, `logger`, `status`, and so on. Two routes: the easy one (make the app emit JSON) and the one that earns its keep (regex-parse logs you can't change). Expect the regex part to take a couple of iterations; that's normal, and learning to iterate *is* the lesson.

---

## Chunk 1 — Why a line in one field isn't enough

At the end of Module 2, every Django line arrived like this:

```json
{"log": "2026-06-18 09:14:02,118 INFO app handled request to /"}
```

That's *delivered*, but it isn't *usable*. You can't filter to "only ERROR lines," you can't route 5xx responses somewhere special, you can't count by logger — because to Fluent Bit there are no such things as a level or a status. There's one string called `log`.

A **parser** is the stage that fixes this. It takes a string and splits it into named fields. There are two situations, and they call for two different parsers:

- **You control the log format.** Then make the app emit **JSON** and let Fluent Bit's `json` parser do the work for free. This is the modern best practice — structure the log at the source.
- **You don't control the format.** Third-party tools, web-server access logs, legacy services — they emit whatever they emit. Here you reach for a **regex** parser and describe the shape yourself.

Your Django app is both at once: the application logger is yours to shape (→ JSON), but the framework's request/access logs come in a fixed text format you don't own (→ regex). So we'll do both.

---

## Chunk 2 — Route 1: make Django speak JSON

Update the app so the application logger emits JSON and the framework's access logs go to a *separate* plain-text file. Replace **`app/app.py`** with:

```python
import logging
import sys

from django.conf import settings
from django.core.management import execute_from_command_line
from django.http import JsonResponse
from django.urls import path

settings.configure(
    DEBUG=True,
    ALLOWED_HOSTS=["*"],
    ROOT_URLCONF=__name__,
    SECRET_KEY="dev-only-not-a-secret",
    MIDDLEWARE=[],
    LOGGING={
        "version": 1,
        "disable_existing_loggers": False,
        "formatters": {
            "json": {
                "()": "pythonjsonlogger.jsonlogger.JsonFormatter",
                "format": "%(asctime)s %(levelname)s %(name)s %(message)s",
                "rename_fields": {"asctime": "time", "levelname": "level", "name": "logger"},
            },
            "access": {
                "format": "%(asctime)s %(levelname)s %(name)s %(message)s",
            },
        },
        "handlers": {
            "app_file": {
                "class": "logging.FileHandler",
                "filename": "/logs/app.log",
                "formatter": "json",
            },
            "access_file": {
                "class": "logging.FileHandler",
                "filename": "/logs/access.log",
                "formatter": "access",
            },
        },
        "loggers": {
            "app": {"handlers": ["app_file"], "level": "INFO", "propagate": False},
            "django.server": {"handlers": ["access_file"], "level": "INFO", "propagate": False},
        },
    },
)

logger = logging.getLogger("app")


def index(request):
    logger.info("handled request to /")
    return JsonResponse({"message": "hello from django"})


urlpatterns = [path("", index)]

if __name__ == "__main__":
    execute_from_command_line(sys.argv)
```

What changed from Module 2, and why:

- The **`json` formatter** uses `python-json-logger` to render each app log as a JSON object. `rename_fields` maps Python's clunky attribute names (`asctime`, `levelname`, `name`) to clean keys (`time`, `level`, `logger`).
- There are now **two handlers**: `app_file` (your JSON app logs → `/logs/app.log`) and `access_file` (Django's plain request logs → `/logs/access.log`).
- The **`loggers`** section wires each logger to its own file: your `app` logger to JSON, the built-in `django.server` logger to plain text. `propagate: False` stops them leaking into each other.

Add the JSON library to **`app/requirements.txt`**:

```
Django>=5.1,<5.3
python-json-logger==2.0.7
```

`docker-compose.yml` and the `Dockerfile` are unchanged.

---

## Chunk 3 — Parse the JSON in Fluent Bit

Parsers can be defined right inside the YAML config, in a top-level `parsers:` section, then referenced by name from an input. Start with just the JSON path in **`fluent-bit/fluent-bit.yaml`**:

```yaml
service:
  flush: 1
  log_level: info

parsers:
  - name: app_json
    format: json

pipeline:
  inputs:
    - name: tail
      path: /logs/app.log
      tag: django.app
      parser: app_json
      read_from_head: true

  outputs:
    - name: stdout
      match: 'django.*'
      format: json_lines
```

The new pieces are the `parsers:` block (one parser named `app_json`, format `json`) and the `parser: app_json` line on the tail input — that's what says "run each line through this parser before emitting the record."

Clear out the old logs and run:

```bash
rm -f logs/*.log
docker compose up -d --build
curl -s localhost:8000 ; echo
docker compose logs fluent-bit
```

> **Predict before you look:** in Module 2 the app line came through as `{"log": "...one big string..."}`. With the `json` parser applied, what should the record look like now?

```
fluent-bit-1  | {"date":1750238042.118,"time":"2026-06-18 09:14:02,118","level":"INFO","logger":"app","message":"handled request to /"}
```

The single `log` string is gone. In its place: `level`, `logger`, `message`, `time` — real fields you can now filter and route on. That's the entire win of structuring at the source, and it cost you one parser line.

---

## Chunk 4 — Route 2: the log you don't control

Look at the access file Django wrote:

```bash
cat logs/access.log
```

```
2026-06-18 09:14:02,140 INFO django.server "GET / HTTP/1.1" 200 33
```

You can't make this JSON — it's Django's built-in request format, not yours. This is the normal situation with anything you didn't write. So you describe its shape with a **regex parser** and let Fluent Bit pull the fields out.

The tool is a regex with **named capture groups** — `(?<name>...)` — where each group becomes a field. Add a second parser to the `parsers:` section:

```yaml
parsers:
  - name: app_json
    format: json

  - name: django_access
    format: regex
    regex: '^(?<time>\S+ \S+) (?<level>\w+) (?<logger>[\w.]+) "(?<method>\w+) (?<path>\S+) (?<protocol>[^"]+)" (?<status>\d+) (?<size>\d+)$'
```

Read the regex against the line and it maps cleanly: `2026-06-18 09:14:02,140` → `time`, `INFO` → `level`, `django.server` → `logger`, then the quoted request splits into `method` / `path` / `protocol`, and the trailing numbers become `status` and `size`.

---

## Chunk 5 — Wire both, and meet the debugging loop

Add the second tail input so both files flow through the pipe. Full **`fluent-bit/fluent-bit.yaml`**:

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

  outputs:
    - name: stdout
      match: 'django.*'
      format: json_lines
```

Two inputs, each with its own parser and its own tag (`django.app`, `django.access`); one output catches both via `django.*`. Re-run:

```bash
rm -f logs/*.log
docker compose up -d --build
curl -s localhost:8000 ; curl -s localhost:8000 ; echo
docker compose logs fluent-bit
```

A successful run shows both record shapes:

```
fluent-bit-1  | {"date":...,"time":"2026-06-18 09:14:02,118","level":"INFO","logger":"app","message":"handled request to /"}
fluent-bit-1  | {"date":...,"time":"2026-06-18 09:14:02,140","level":"INFO","logger":"django.server","method":"GET","path":"/","protocol":"HTTP/1.1","status":"200","size":"33"}
```

**If your regex is even slightly off, here's exactly what you'll see instead** — and this is the most useful thing in the module:

```
fluent-bit-1  | {"date":...,"log":"2026-06-18 09:14:02,140 INFO django.server \"GET / HTTP/1.1\" 200 33"}
```

When a regex parser fails to match, Fluent Bit doesn't crash — it falls back to leaving the whole line in a `log` field, exactly like having no parser at all. **So the `log` field reappearing is your signal that the regex didn't match.** The fix is always the same loop: adjust the regex, `rm -f logs/*.log`, re-run, look at the output, repeat until the fields appear. Different Django versions vary the access line slightly, so if it doesn't match first try, that's expected — tweak and re-run. Iterating on a regex against real output is the actual job.

---

## Chunk 6 — Two things to file away about parsers

- **Regex fields are always strings.** Notice `"status":"200"` — quoted, not a number. A regex parser extracts text; it doesn't know `200` is an integer. Converting types is a *filter's* job (Module 5), so don't expect to do numeric comparisons on `status` yet.
- **Time is a field until you make it a timestamp.** Right now `time` is just another string field; Fluent Bit is using its own read-time as the record's real timestamp (the `date` key). Promoting the parsed `time` to the record's official timestamp is done with `time_key` / `time_format` on the parser — useful in production so the timestamp reflects when the event *happened*, not when Fluent Bit read it. We're skipping it here to avoid `strptime` fiddliness while you're learning the field-extraction itself.

---

## Chunk 7 — Cheat sheet

| Goal | Config |
|---|---|
| Define parsers inline | top-level `parsers:` list |
| Parse JSON lines | parser `format: json` |
| Parse arbitrary text | parser `format: regex` with `regex:` |
| Named capture group | `(?<fieldname>...)` |
| Apply a parser to an input | `parser: <name>` on the `tail` input |
| Tell a regex failed | the whole line reappears under the `log` key |
| The iterate loop | edit regex → `rm -f logs/*.log` → `up -d --build` → read output → repeat |
| Emit JSON from Django | `python-json-logger`'s `JsonFormatter` + `rename_fields` |

Mental model: **a parser turns one string into many fields. JSON when you own the format; regex when you don't.**

---

## Chunk 8 — Checkpoint challenges

From memory.

**Challenge A — JSON win**
1. Confirm the app log arrives as discrete fields (`level`, `logger`, `message`), not one `log` string.
2. Add a second log call in the `index` view at WARNING level (`logger.warning("slow path hit")`) and show it coming through as structured JSON with `level: "WARNING"`.

**Challenge B — break and fix a regex (the real skill)**
1. Deliberately break `django_access` — delete the `(?<status>\d+)` group from the regex.
2. Re-run and describe precisely what the access records look like now and *why* (what does the failed match fall back to?).
3. Restore the group and confirm `status` returns as a field.

**Bonus question (mental model):** Both `app.log` and `access.log` end up as structured records, but they took different routes to get there. In one sentence, what determines whether a given log source should be parsed with `json` versus `regex` — and which one required *no* cooperation from the thing producing the logs?

---

*End of Module 3. Next: Module 4 — Multiline, where a single Django exception spanning fifteen lines of traceback stops becoming fifteen broken records and starts becoming one.*
