# Module 2 — Tailing Django

> **Hands-on rule:** type every command. Every code block shows the command *and* what to expect.
>
> **Environment:** macOS + Docker Desktop. **Run every command in this module from the project root `~/fluent-bit-django`** — the Compose file and the volume paths assume it. (If you `cd` into a subfolder, the relative paths break, exactly like the bind-mount trap from Module 1.)
>
> **Scope (~45–60 min):** Stand up a tiny Django app that writes logs to a file, point Fluent Bit's `tail` input at that file, and watch real log lines flow through the pipe you built in Module 1. No parsing yet — the lines arrive whole, on purpose.

---

## Chunk 1 — From fake logs to real ones

In Module 1 the `dummy` input invented records out of thin air. That was useful for learning the pipe, but it taught you nothing about getting *real* logs out of a *real* app. That's the whole job, so let's do it.

Four things change in this module:

- A **Django app** appears — deliberately tiny, a single file. It exists only to produce log lines.
- Django writes those lines to a **shared log file** on disk.
- Fluent Bit gains a **`tail` input** — the plugin that reads lines from a file as they're appended (think `tail -f`).
- We introduce **Docker Compose**, because two containers now have to cooperate: Django writes the file, Fluent Bit reads it. Compose declares both containers and the shared file in one place.

The `dummy` → `stdout` shape from Module 1 doesn't disappear — we're just swapping `dummy` for `tail`. Same pipe, real input.

---

## Chunk 2 — The Django app (one file)

Everything Django-related lives in `app/`. Three files.

**`app/app.py`** — a complete Django app compressed into a single file. It does exactly one thing: log a line when you hit `/`.

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
            "plain": {
                "format": "%(asctime)s %(levelname)s %(name)s %(message)s",
            },
        },
        "handlers": {
            "file": {
                "class": "logging.FileHandler",
                "filename": "/logs/django.log",
                "formatter": "plain",
            },
        },
        "root": {
            "handlers": ["file"],
            "level": "INFO",
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

The single-file form is a tutorial compression — real projects spread this across `settings.py`, `urls.py`, and a view. But **the part that matters here is the `LOGGING` dict, and it is identical to what you'd paste into a real `settings.py`.** That dict says: format each line as `time LEVEL logger-name message`, and send it to a file at `/logs/django.log`. Notice it logs *only* to a file — no console handler — so the file is the single, clean source Fluent Bit will tail.

**`app/requirements.txt`**:

```
Django>=5.1,<5.3
```

**`app/Dockerfile`**:

```dockerfile
FROM python:3.12-slim
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY app.py .
CMD ["python", "app.py", "runserver", "0.0.0.0:8000", "--noreload"]
```

`--noreload` turns off Django's auto-reloader so we get one clean process, not a parent-and-child pair — simpler logs while learning.

---

## Chunk 3 — Letting two containers share one file

A log file written *inside* the Django container is invisible to the Fluent Bit container — each container has its own filesystem. The fix is a **shared volume**: a directory on your Mac mounted into *both* containers at `/logs`. Django writes `/logs/django.log`; Fluent Bit reads the very same file.

Create the shared directory and the Compose file at the project root:

```bash
mkdir -p logs
```

**`docker-compose.yml`** (at `~/fluent-bit-django/`):

```yaml
services:
  django:
    build: ./app
    ports:
      - "8000:8000"
    volumes:
      - ./logs:/logs

  fluent-bit:
    image: fluent/fluent-bit:3.2
    command: ["-c", "/fluent-bit/etc/fluent-bit.yaml"]
    volumes:
      - ./fluent-bit/fluent-bit.yaml:/fluent-bit/etc/fluent-bit.yaml
      - ./logs:/logs
    depends_on:
      - django
```

The line that makes the whole thing work is `./logs:/logs` appearing under **both** services — that's the one folder on your Mac that both containers can see. Django's `-v` from Module 1's mental model is still here; it's just declared in YAML now instead of on the command line. Fluent Bit also keeps its config bind-mount, exactly as in Module 1.

---

## Chunk 4 — The `tail` input

Replace the contents of **`fluent-bit/fluent-bit.yaml`** with a tail-based pipeline:

```yaml
service:
  flush: 1
  log_level: info

pipeline:
  inputs:
    - name: tail
      path: /logs/django.log
      tag: django.app
      read_from_head: true

  outputs:
    - name: stdout
      match: 'django.*'
      format: json_lines
```

The output half is unchanged from Module 1 — `stdout`, `json_lines`, matching the `django.*` tag family. The input is the new part:

- `path: /logs/django.log` — the file to follow. It also accepts globs, e.g. `path: /logs/*.log`, to tail many files at once.
- `tag: django.app` — every line read from this file becomes a record tagged `django.app`, which is why the output's `match: 'django.*'` accepts it.
- `read_from_head: true` — read the file from the **beginning**. By default `tail` behaves like `tail -f`: it only picks up lines *appended after Fluent Bit starts*, ignoring whatever was already in the file. For learning, `read_from_head: true` means you see existing lines too, so nothing feels lost.

---

## Chunk 5 — Run it and watch logs flow

> **Predict before you run:** Django logs only to `/logs/django.log`. Fluent Bit tails that same file and prints to stdout. When you `curl` the Django endpoint, whose container output will show the resulting Fluent Bit record — `django`'s or `fluent-bit`'s?

Bring the whole stack up from the project root:

```bash
docker compose up -d --build
```

```
[+] Running 2/2
 ✔ Container fluent-bit-django-django-1      Started
 ✔ Container fluent-bit-django-fluent-bit-1  Started
```

Generate a few log lines:

```bash
curl -s localhost:8000 ; echo
curl -s localhost:8000 ; echo
# {"message": "hello from django"}
# {"message": "hello from django"}
```

First, look at the **raw file** Django wrote — it's on your Mac because `./logs` is bind-mounted:

```bash
cat logs/django.log
```

```
2026-06-18 09:14:02,118 INFO app handled request to /
2026-06-18 09:14:02,140 INFO django.server "GET / HTTP/1.1" 200 33
2026-06-18 09:14:03,201 INFO app handled request to /
2026-06-18 09:14:03,222 INFO django.server "GET / HTTP/1.1" 200 33
```

Now see what **Fluent Bit** did with those same lines:

```bash
docker compose logs fluent-bit
```

```
fluent-bit-1  | [2026/06/18 09:14:00] [ info] [input:tail:tail.0] inotify_fs_add(): inode=... name=/logs/django.log
fluent-bit-1  | {"date":1750238042.118,"log":"2026-06-18 09:14:02,118 INFO app handled request to /"}
fluent-bit-1  | {"date":1750238042.140,"log":"2026-06-18 09:14:02,140 INFO django.server \"GET / HTTP/1.1\" 200 33"}
fluent-bit-1  | {"date":1750238043.201,"log":"2026-06-18 09:14:03,201 INFO app handled request to /"}
```

(Answer to the prediction: the record shows in **`fluent-bit`**'s output — it's the one tailing the file and printing records. Django only ever wrote to the file.)

Look closely at one record:

```json
{"date":1750238042.118,"log":"2026-06-18 09:14:02,118 INFO app handled request to /"}
```

The entire Django line landed inside **one field called `log`**, as a single opaque string. Fluent Bit faithfully delivered the line — but it has no idea that `INFO` is a level, or that `app` is the logger, or where the message starts. To Fluent Bit, right now, it's just text. Teaching it to crack that string into real fields (`level`, `logger`, `message`) is the entire job of Module 3.

Leave the stack running, or tear it down for now:

```bash
docker compose down
```

(Your `logs/django.log` survives on your Mac, since it's a bind-mount, not a container volume.)

---

## Chunk 6 — Three `tail` behaviors worth knowing now

You'll meet these constantly, so name them while the context is fresh:

- **Follow vs. from-head.** Without `read_from_head: true`, `tail` only reads lines added *after* startup — correct for production (you don't want to re-ingest gigabytes of old logs on every restart), but it can look like "nothing's happening" when you're testing against a file that already has content.
- **Globs.** `path: /logs/*.log` tails every matching file and picks up new ones as they appear — this is how a single Fluent Bit instance follows many services at once.
- **The offset database.** In production you add `db: /var/log/fluentbit.db` to the tail input. Fluent Bit records how far it has read into each file there, so a restart resumes exactly where it left off instead of re-reading from the top. We're skipping it here to keep things simple, but know it's the answer to "why did my logs get duplicated after a restart?"

---

## Chunk 7 — Cheat sheet

| Goal | Command / config |
|---|---|
| Create the shared log dir | `mkdir -p logs` |
| Build + start everything (detached) | `docker compose up -d --build` |
| Generate a log line | `curl -s localhost:8000` |
| See the raw file Django wrote | `cat logs/django.log` |
| See Fluent Bit's records | `docker compose logs fluent-bit` |
| Follow Fluent Bit live | `docker compose logs -f fluent-bit` |
| Stop everything | `docker compose down` |
| Tail a file | input `tail` with `path:` |
| Tail many files | `path: /logs/*.log` |
| Read existing content too | `read_from_head: true` |
| Resume after restart (prod) | `db: /path/to/offset.db` |

Default behavior to remember: a tailed line arrives as a single string under the key **`log`** — unparsed.

---

## Chunk 8 — Checkpoint challenges

From memory — no scrolling up.

**Challenge A — wire and observe**
1. From a clean state, bring the stack up and generate three requests to Django.
2. Show the raw lines in `logs/django.log`, then show how Fluent Bit rendered those same lines.
3. Identify, in one Fluent Bit record, which field holds the entire Django line.

**Challenge B — prove the tag routing (spiral from Module 1)**
1. Change the `tail` input's tag from `django.app` to `web.access`.
2. Without changing anything else, predict whether records will still appear in the `stdout` output. Why or why not?
3. Run it, confirm your prediction, then make the output accept the new tag again.

**Bonus question (mental model):** Django and Fluent Bit run in separate containers with separate filesystems, yet Fluent Bit reads a file Django wrote. In one sentence, what makes that possible — and what single line in `docker-compose.yml` is doing the work?

---

*End of Module 1's promise, delivered: real logs now flow through Fluent Bit. Next: Module 3 — Parsing into Structure, where we stop treating each Django line as one opaque string and split it into real fields you can filter and route on.*
