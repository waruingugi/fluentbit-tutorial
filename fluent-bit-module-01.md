# Module 1 — Fluent Bit's Mental Model, and Your First Pipeline

> **Hands-on rule:** type every command. Reading is not learning here; your fingers need to learn the verbs. Every code block shows the command *and* what to expect, so you always know whether you're on track.
>
> **Environment:** macOS + Docker Desktop. Fluent Bit runs as a container; we use its **YAML** configuration (the current recommended format).
>
> **Scope (~30–40 min):** No Django yet, no files to tail, no backend. We run Fluent Bit with a fake log generator and watch records flow through the pipe. The goal is the *mental model* — once that clicks, every later module is just "add a stage."

---

## Chunk 1 — What Fluent Bit actually is

Fluent Bit is not a log store and not a UI. It's a **pipeline**: data comes in one end, gets reshaped in the middle, and is sent out the other end. That's the whole product. Everything you'll learn is a variation on "add or configure a stage of that pipe."

The pipe has five stages, in order:

> **input → parser → filter → buffer → output**

For this module we touch only the two ends — `input` and `output`. Parsers, filters, and buffering each arrive in a later module, introduced when Django's logs make you *want* them.

Three words carry the entire routing model, so learn them now:

- A **record** is one log event: a timestamp plus a set of key/value fields. Everything moving through Fluent Bit is a record.
- A **tag** is a label stamped onto a record *at the input stage*. It travels with the record and decides where it's allowed to go.
- A **match** is a rule on an output that says "I accept records whose tag matches this pattern."

So the flow of any record is: it enters at an input, gets a tag, travels the pipe, and lands in whichever outputs have a `match` that fits its tag. Tag and match are the steering wheel. Hold onto that — most "why is nothing showing up?" confusion later is a tag that doesn't match.

---

## Chunk 2 — The smallest possible pipeline

Make a home for the course (Django joins it in Module 2, so we leave room):

```bash
mkdir -p ~/fluent-bit-django/fluent-bit
cd ~/fluent-bit-django
```

Create the config file **`fluent-bit/fluent-bit.yaml`**:

```yaml
service:
  flush: 1
  log_level: info

pipeline:
  inputs:
    - name: dummy
      tag: demo.hello
      dummy: '{"message": "hello from fluent bit"}'

  outputs:
    - name: stdout
      match: '*'
```

Reading it top to bottom:

- `service.flush: 1` — every 1 second, Fluent Bit pushes whatever it has buffered to the outputs. `log_level: info` controls Fluent Bit's *own* logs (the startup chatter), not your records.
- `inputs` is a list. The `dummy` plugin is a built-in fake generator — it emits the record you give it, once per second, with no external dependency. Perfect for learning. `tag: demo.hello` stamps every record it produces.
- `outputs` is a list. The `stdout` plugin prints records to the terminal. `match: '*'` means "accept every tag" — the `*` is a wildcard.

That's a complete, runnable pipeline: generator in, printer out.

---

## Chunk 3 — Run it and read the output

> **Predict before you run:** `dummy` emits one record per second and `flush` is 1 second. Roughly how many lines per second should you see printed?

```bash
docker run --rm \
  -v "$(pwd)/fluent-bit/fluent-bit.yaml:/fluent-bit/etc/fluent-bit.yaml" \
  fluent/fluent-bit:3.2 \
  -c /fluent-bit/etc/fluent-bit.yaml
```

Decode it before moving on — most of this is the Docker vocabulary you already have, with one Fluent-Bit-specific twist:

- `docker run --rm` — create and start a container, and auto-delete it when it exits, so repeated runs don't litter your machine.
- `-v "$(pwd)/fluent-bit/fluent-bit.yaml:/fluent-bit/etc/fluent-bit.yaml"` — bind-mount your local config file into the container at the path Fluent Bit reads from. The config lives on your Mac; the container just sees it through the mount.
- `fluent/fluent-bit:3.2` — the image. This pins a known-good 3.x line; a newer 3.x tag is fine too.
- `-c /fluent-bit/etc/fluent-bit.yaml` — the one Fluent-Bit-specific flag: it tells Fluent Bit *which* config file to load. The image defaults to a classic `.conf` file, so this flag is what makes it use **your** YAML instead.

The pattern to internalize: the container ships Fluent Bit, the `-v` mount supplies your pipeline, and `-c` points Fluent Bit at it. That means you edit the YAML on your Mac, re-run, and the new config takes effect immediately — no rebuilding. You'll lean on exactly that loop in the next two chunks, where you change the config and re-run repeatedly.

Expect Fluent Bit's own startup banner, then a steady drip of records:

```
Fluent Bit v3.2.x
[2026/06/17 13:00:00] [ info] [fluent bit] version=3.2.x
[2026/06/17 13:00:00] [ info] [input:dummy:dummy.0] initializing
[2026/06/17 13:00:00] [ info] [output:stdout:stdout.0] worker #0 started
[0] demo.hello: [[1750166400.001234, {}], {"message"=>"hello from fluent bit"}]
[0] demo.hello: [[1750166401.002345, {}], {"message"=>"hello from fluent bit"}]
[0] demo.hello: [[1750166402.003456, {}], {"message"=>"hello from fluent bit"}]
```

(One line per second, as predicted.) Decode one record line — this structure *is* a Fluent Bit record, and you'll be reading it all day:

```
[0]            demo.hello:   [[1750166400.001234, {}],        {"message"=>"hello from fluent bit"}]
 ^index         ^tag           ^timestamp   ^metadata          ^the record's fields
```

The `[0]` is the record's position in the current flush batch. `demo.hello` is the tag you assigned. Then comes the timestamp and a small metadata map (usually empty here), and finally the actual fields. Everything Fluent Bit does — parsing, filtering, routing — is operating on that bracketed thing.

Stop it with `Ctrl+C`. Because of `--rm`, the container cleans itself up.

---

## Chunk 4 — Prove that tags and matches do the steering

Right now `match: '*'` accepts everything, so it's easy to assume output is automatic. It isn't — it's gated by the tag. Prove it. Change the output's match to a pattern the tag *doesn't* fit:

```yaml
  outputs:
    - name: stdout
      match: 'nope.*'
```

Re-run the same `docker run` command. You'll see the startup banner and the input initializing — but **no record lines**. The `dummy` input is still generating records tagged `demo.hello`; they simply have nowhere to go, because no output matches their tag. They're created and silently dropped.

Now set the match to a wildcard that *does* fit the tag:

```yaml
  outputs:
    - name: stdout
      match: 'demo.*'
```

Re-run — records flow again. `demo.*` matches `demo.hello` (and `demo.anything-else`). That's the whole routing mechanism: **an output only ever sees records whose tag its `match` accepts.** When logs go missing later, this is the first thing to check.

---

## Chunk 5 — Make the output actually readable

The default stdout format shows Fluent Bit's internal representation — those `=>` arrows. When you're debugging what Fluent Bit is *producing*, plain JSON is far easier to read. Add one line to the output:

```yaml
  outputs:
    - name: stdout
      match: 'demo.*'
      format: json_lines
```

Re-run. Same data, rendered as one JSON object per line:

```
{"date":1750166400.001234,"message":"hello from fluent bit"}
```

`json_lines` will be your default debugging lens for the rest of the course — it's how you'll confirm that a parser or filter did what you expected to each record.

---

## Chunk 6 — Cheat sheet

| Goal | Command / config |
|---|---|
| Make the project layout | `mkdir -p ~/fluent-bit-django/fluent-bit && cd ~/fluent-bit-django` |
| Run Fluent Bit with your config | `docker run --rm -v "$(pwd)/fluent-bit/fluent-bit.yaml:/fluent-bit/etc/fluent-bit.yaml" fluent/fluent-bit:3.2 -c /fluent-bit/etc/fluent-bit.yaml` |
| Stop it | `Ctrl+C` (`--rm` auto-cleans the container) |
| Fake log generator | input plugin `dummy` |
| Print records | output plugin `stdout` |
| Accept every tag | `match: '*'` |
| Accept a tag prefix | `match: 'demo.*'` |
| Readable JSON output | `format: json_lines` on the stdout output |
| Push interval | `service.flush` (seconds) |

**The three words:** *record* (timestamp + fields), *tag* (stamped at input), *match* (the output's filter on tags).

---

## Chunk 7 — Checkpoint challenges

From memory — no scrolling up.

**Challenge A — build a pipe from scratch**
1. Write a config with a `dummy` input that emits `{"message": "checkpoint"}`, tagged `app.test`.
2. Send it to `stdout`, accepting only tags starting with `app.`.
3. Run it, confirm ~1 record/second, then switch the output to `json_lines` and re-run.

**Challenge B — selective routing**
1. Add a *second* `dummy` input with a different tag, e.g. `app.noise`.
2. Configure the `stdout` output so that only `app.test` records appear and `app.noise` records do not.
3. Predict what you'll see before running, then verify.

**Bonus question (mental model):** In one sentence, what are the two things whose relationship decides whether a given record reaches a given output? And why does Fluent Bit stamp the tag at the *input* stage rather than letting the output decide at the end?

---

*End of Module 1. Next: Module 2 — Tailing Django, where we stand up a small Django app, point Fluent Bit's `tail` input at its real logs, and route them through the exact pipe you just built — no more dummy data.*
