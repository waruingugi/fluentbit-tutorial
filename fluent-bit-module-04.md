# Module 4 — Multiline: Taming Tracebacks

> **Hands-on rule:** type every command. **Run everything from the project root `~/fluent-bit-django`.**
>
> **Environment:** macOS + Docker Desktop.
>
> **Scope (~45–60 min):** Make a Django exception — a timestamped error line followed by a multi-line traceback — arrive in Fluent Bit as **one** record instead of one-broken-record-per-line. This is the single most Django-specific skill in the course, because Python apps throw multi-line tracebacks constantly.

---

## Chunk 1 — One error, fifteen useless records

Fluent Bit's `tail` reads a file **one physical line at a time**. That's correct for most logs — one line, one event. But a Python traceback is one *event* spread across many *lines*:

```
2026-06-18 10:01:15,300 ERROR errors failed to handle /boom
Traceback (most recent call last):
  File "/app/app.py", line 62, in boom
    result = 1 / 0
             ~~^~~
ZeroDivisionError: division by zero
```

Read line-by-line, Fluent Bit turns that single error into six separate records. Worse, only the first has a timestamp and a level — the rest are orphaned fragments:

```json
{"log": "2026-06-18 10:01:15,300 ERROR errors failed to handle /boom"}
{"log": "Traceback (most recent call last):"}
{"log": "  File \"/app/app.py\", line 62, in boom"}
{"log": "    result = 1 / 0"}
{"log": "ZeroDivisionError: division by zero"}
```

The message says "failed," but the *actual cause* — `ZeroDivisionError` — is in a different record with no level, no timestamp, no connection to the error it belongs to. You can't alert on it, can't search it, can't tell which traceback line goes with which error. Let's create this problem, then fix it.

First, give Django a way to throw an error into a plain-text file. Replace **`app/app.py`**:

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
            "plain": {
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
                "formatter": "plain",
            },
            "error_file": {
                "class": "logging.FileHandler",
                "filename": "/logs/error.log",
                "formatter": "plain",
            },
        },
        "loggers": {
            "app": {"handlers": ["app_file"], "level": "INFO", "propagate": False},
            "django.server": {"handlers": ["access_file"], "level": "INFO", "propagate": False},
            "errors": {"handlers": ["error_file"], "level": "ERROR", "propagate": False},
        },
    },
)

logger = logging.getLogger("app")
err_logger = logging.getLogger("errors")


def index(request):
    logger.info("handled request to /")
    return JsonResponse({"message": "hello from django"})


def boom(request):
    try:
        result = 1 / 0
    except Exception:
        err_logger.exception("failed to handle /boom")
    return JsonResponse({"detail": "an error was logged"}, status=500)


urlpatterns = [
    path("", index),
    path("boom", boom),
]

if __name__ == "__main__":
    execute_from_command_line(sys.argv)
```

What's new since Module 3: the `access` formatter is renamed `plain` (it's now used for two things), there's an `error_file` handler writing **plain text** to `/logs/error.log`, an `errors` logger routed to it, and a `/boom` view that catches a real exception and logs it with `err_logger.exception(...)` — which writes the message *and* the full traceback. It's plain text, so those traceback lines land as real, separate physical lines in the file. (Your JSON `app.log` would have escaped the newlines into one line — which is exactly why the multiline problem only bites plain-text logs.)

Now point Fluent Bit at that error file **with no multiline handling yet**, so you can see the breakage. Set **`fluent-bit/fluent-bit.yaml`** to:

```yaml
service:
  flush: 1
  log_level: info

pipeline:
  inputs:
    - name: tail
      path: /logs/error.log
      tag: django.error
      read_from_head: true

  outputs:
    - name: stdout
      match: 'django.*'
      format: json_lines
```

Run it and trigger an error:

```bash
rm -f logs/*.log
docker compose up -d --build
curl -s localhost:8000/boom ; echo
docker compose logs fluent-bit
```

```
fluent-bit-1  | {"date":...,"log":"2026-06-18 10:01:15,300 ERROR errors failed to handle /boom"}
fluent-bit-1  | {"date":...,"log":"Traceback (most recent call last):"}
fluent-bit-1  | {"date":...,"log":"  File \"/app/app.py\", line 62, in boom"}
fluent-bit-1  | {"date":...,"log":"    result = 1 / 0"}
fluent-bit-1  | {"date":...,"log":"ZeroDivisionError: division by zero"}
```

There's the problem, live: one exception, five disconnected records.

---

## Chunk 2 — The one question multiline parsing answers

To glue those lines back together, Fluent Bit needs to answer a single question for every incoming line:

> *Does this line **start a new event**, or does it **continue the previous one**?*

That's the whole idea. You give Fluent Bit two patterns:

- a **start** pattern that recognizes the first line of an event, and
- a **continuation** pattern that recognizes lines belonging to the event already in progress.

For Django logs the answer is beautifully simple: **a real event always begins with a timestamp.** `Traceback...`, `  File ...`, and `ZeroDivisionError...` never start with a timestamp — so they must be continuations of the line above. That single observation is enough to stitch any traceback back together, no matter how long.

This is expressed as a tiny state machine in a top-level `multiline_parsers:` section.

---

## Chunk 3 — Write the multiline parser and fix it

Update **`fluent-bit/fluent-bit.yaml`** to define and use a multiline parser:

```yaml
service:
  flush: 1
  log_level: info

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
      path: /logs/error.log
      tag: django.error
      multiline.parser: django_multiline
      read_from_head: true

  outputs:
    - name: stdout
      match: 'django.*'
      format: json_lines
```

Read the state machine the way Fluent Bit does:

- **`start_state`** — a line beginning with a timestamp (`2026-06-18 10:01:15`) is the first line of an event. Matching it moves the machine into `cont`.
- **`cont`** — a line that does **not** begin with a timestamp (that's the `(?!...)` negative-lookahead doing the work) is a continuation; append it and stay in `cont`. When the next timestamped line arrives, it matches `start_state` again, which flushes the assembled record and begins the next one.

The input change is one line: `multiline.parser: django_multiline` instead of a plain `parser:`. Re-run:

```bash
rm -f logs/*.log
docker compose up -d
curl -s localhost:8000/boom ; echo
docker compose logs fluent-bit
```

> **Predict before you look:** the error event was six physical lines. How many records should Fluent Bit emit for it now?

```
fluent-bit-1  | {"date":...,"log":"2026-06-18 10:01:15,300 ERROR errors failed to handle /boom\nTraceback (most recent call last):\n  File \"/app/app.py\", line 62, in boom\n    result = 1 / 0\n             ~~^~~\nZeroDivisionError: division by zero"}
```

One record. The whole traceback — message, frames, and the `ZeroDivisionError` cause — now lives together in a single `log` field, newlines preserved as `\n` inside it. You can finally search for `ZeroDivisionError` and land on the complete error in one piece.

---

## Chunk 4 — The flush_timeout gotcha

Notice `flush_timeout: 1000` in the parser. It matters, and here's why: Fluent Bit can't *know* a traceback has ended until either a new timestamped line arrives or some time passes. Without a timeout, the **last** error in a file would sit in limbo forever, waiting for a "next event" that may never come — and you'd swear your final traceback vanished.

`flush_timeout: 1000` means "if no new line extends this event for 1000 ms, consider it complete and emit it." That's why, when you `curl /boom` once and wait a second, the record appears even though nothing came after it. If you ever see a multiline event that only shows up *after* you trigger the next one, an absent or too-long `flush_timeout` is the cause.

---

## Chunk 5 — The shortcut: built-in multiline parsers

You wrote a custom parser because it teaches the mechanism and handles our exact "timestamp-prefixed" format. But Fluent Bit ships **built-in** multiline parsers for common stack-trace formats — including `python`, `java`, and `go`. For a file that contains *bare* Python tracebacks, you can skip writing rules entirely:

```yaml
    - name: tail
      path: /logs/error.log
      tag: django.error
      multiline.parser: python
      read_from_head: true
```

The trade-off: built-ins recognize the traceback *shape* itself, while our custom "new events start with a timestamp" rule is more robust when each error is wrapped in your own log line (as Django's are). Knowing both means you reach for the built-in when it fits and write rules when it doesn't.

---

## Chunk 6 — Where this sits in the full pipeline

You've been testing the error file in isolation to keep focus. In the real config it joins the JSON and regex inputs from Module 3 — three sources, three parsing strategies, one output:

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

  outputs:
    - name: stdout
      match: 'django.*'
      format: json_lines
```

Note the division of labour: single-line sources use `parser:` (JSON, regex), the multi-line source uses `multiline.parser:`. They're different stages — one splits a line into fields, the other joins many lines into one record.

---

## Chunk 7 — Cheat sheet

| Goal | Config |
|---|---|
| Define a multiline parser | top-level `multiline_parsers:` list |
| Detect "this starts an event" | rule `state: start_state` with a `regex` |
| Detect "this continues an event" | rule `state: cont` with a `regex` |
| "Starts with a timestamp" rule | `/^\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}/` |
| "Does NOT start with a timestamp" | `/^(?!\d{4}-\d{2}-\d{2} ).+/` (negative lookahead) |
| Apply to an input | `multiline.parser: <name>` on `tail` |
| Flush the last event | `flush_timeout:` (milliseconds) |
| Skip writing rules | built-in `multiline.parser: python` (or `java`, `go`) |

Mental model: **a single-line parser splits one line into many fields; a multiline parser joins many lines into one record.** Multiline asks one question per line — *new event, or continuation?*

---

## Chunk 8 — Checkpoint challenges

From memory.

**Challenge A — see it both ways**
1. Trigger `/boom` with the multiline parser *removed* and count the records the one error produces.
2. Add the multiline parser back, trigger it again, and confirm it's a single record containing `ZeroDivisionError`.

**Challenge B — prove the flush_timeout**
1. Set `flush_timeout` very high (say `60000`) and trigger one `/boom`.
2. Watch the output — does the record appear right away? Trigger a second event and observe what shakes the first one loose. Explain what's happening.
3. Restore `flush_timeout: 1000`.

**Bonus question (mental model):** Your JSON `app.log` never suffered the multiline problem even when it logged exceptions, but the plain-text `error.log` did. In one sentence, why does the log *format* decide whether multiline handling is needed at all?

---

*End of Module 4 — the conceptual core is now complete: you can collect, structure, and reassemble any Django log. Next: Module 5 — Filtering & Transforming, where you start reshaping records in flight — adding fields, dropping noise, and redacting what shouldn't leave the building.*
