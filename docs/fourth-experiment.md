# Fourth experiment: a company-sized world

The earlier experiments used 20 to 26 records and four or five types. This one
asks what happens at about 200 types and a million records, on one machine, and
whether an agent can still find its way.

## Where the engine stood

Before this work, every operation read every record. Reading one record took
15 ms with 1,000 records, 97 ms with 10,000, and 474 ms with 50,000. At a
million it would have taken about ten seconds.

## What changed

- **A reference index.** The engine keeps a table of which record refers to
  which. Finding what depends on a record no longer reads everything.
- **Field indexes.** One index per field name, shared by every type that has
  the field. The first version used one index per type and field. With 201
  types that was 1,600 indexes, and each write took 78 ms. There are now 33.
- **Queries use the indexes.** The engine takes the parts of a `where`
  expression that name exact values, including one step through a reference,
  and asks SQLite for only the matching records. It then judges each candidate
  by the whole expression. Rules that range over another type do the same.
- **Pages and counts.** A query returns at most 100 records, or `limit` up to
  1,000, with `more` and `next` for the rest. `"count": true` returns only the
  number and shows no record.
- **An index of types.** A world with more than 12 types describes itself as a
  list of type names and descriptions. `GET /api/world?types=A,B` returns the
  fields and rules for the types named.

## The world

`worlds/enterprise.py` holds the calibration lab unchanged plus 197 generated
types across 20 departments, 201 in all. `python3 -m bench.scale build` writes
a million records: 100,000 instruments, 300,000 calibrations, 50,000
deviations, and about 2,800 of each generated type. Every record has its
creation in the history and its references in the index.

```sh
python3 -m bench.scale build
python3 -m bench.scale measure
python3 -m bench.scale clients
python3 -m bench agent --domain enterprise --command bench/agents/claude.sh --repeat 3
```

## Size

The database is 1,395 MB, or 1,394 bytes a record. It builds in 79 seconds.

| Part | Size | Share |
| --- | --- | --- |
| Records | 316 MB | 23% |
| History | 484 MB | 35% |
| Field indexes | 350 MB | 25% |
| Reference index | 249 MB | 18% |

The engine and protocol are 80 KB. The whole system fits on a small USB key.
Three quarters of the database is history and indexes, and neither has been
made compact yet.

## Single operations

Medians on the million-record database, called directly on the engine.

| Operation | Time |
| --- | --- |
| Describe the world (index of types) | 2 ms |
| Describe three types in full | 2 ms |
| Read one record by ID | 12 ms |
| Find one record by an exact field | 9 ms |
| Find through a reference | 8 ms |
| Count by two exact fields | 25 ms |
| First 100 of a common value | 35 ms |
| Create one record | 14 ms |
| Update one record | 22 ms |
| Audit lines for one record | 2 ms |
| Count a value a third of 100,000 records share | 1,010 ms |
| Search part of a text across 100,000 records | 3,038 ms |

The last two are the weak spots. A count still judges every candidate in
Python, and text search has no index.

Most of each read's 8 to 12 ms is waiting for the disk. Every read writes its
audit lines, and SQLite waits for the disk before it confirms a write.

## Many clients at once

Clients over HTTP, each making 100 requests: half reads, four in ten searches,
one in ten creates.

| Clients | Requests a second | Median | 95th percentile |
| --- | --- | --- | --- |
| 1 | 55 | 17 ms | 21 ms |
| 10 | 137 | 15 ms | 242 ms |
| 50 | 157 | 20 ms | 1,546 ms |

Throughput levels off near 160 requests a second, because SQLite allows one
writer at a time and each write waits for the disk. An agent makes a request
every few seconds, so 50 agents ask for about 10 a second. That is well inside
the limit. Fifty clients sending requests as fast as they can is not: one in
twenty waits more than a second and a half.

With SQLite told not to wait for the disk on each commit (`synchronous = NORMAL`):

| Clients | Requests a second | Median | 95th percentile |
| --- | --- | --- | --- |
| 10 | 361 | 8 ms | 65 ms |
| 50 | 445 | 12 ms | 440 ms |

That is nearly three times the throughput. The cost is that a power cut can
lose the last moments of confirmed changes, which a system of record should not
accept by default. The engine keeps the safe setting. The better fix is to
confirm several waiting requests with one wait for the disk, which keeps the
guarantee. It is not built.

## An agent in the large world

Claude Code ran the lab's five tasks, three times each, against the
million-record world. The prompts were the same as for the 20-record lab.

| | 20 records, 4 types | 1,000,000 records, 201 types |
| --- | --- | --- |
| Task runs correct | 15 of 15 | 15 of 15 |
| Calls | 44 | 61 |
| Agent time | 176 s | 170 s |
| Bytes read, median run | 7.1 KB | 23 KB |

The agent made one more call per task: it read the index of types, then asked
for the two or three types it needed. It took no longer. No request was
rejected.

In the first run the checker marked three correct runs wrong. An earlier
measurement had written 80 calibrations into the database the tasks start from,
and the checker counted them. Measurements now work on a copy. The task was run
again on a clean build and all three runs passed.

## What this does not show

- **No control at this size.** EspoCRM has not been loaded with the same 201
  types and a million records. Its metadata was 191 KB with three custom
  entities, and an agent's trouble with it came from that size.
- **One machine, one disk.** Nothing here is replicated or backed up.
- **Generated data.** The 197 extra types share one shape. Real types differ
  more, and their descriptions are longer.
- **Views are not in the size.** Every read adds about 100 bytes of audit. A
  busy year of reads would outweigh the records.

## Next

1. Confirm several writes with one wait for the disk.
2. Count and search inside SQLite when the actor may read every candidate.
3. Make history and the reference index compact.
4. Load the control with the same world, on hardware agreed with Ben.
