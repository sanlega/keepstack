# How keepstack works

AWS Emulator keeps all state in memory. keepstack makes that state durable without modifying the emulator: it is a
small HTTP proxy that records successful writes and replays them into a fresh emulator whenever one starts.

```
Terraform / AWS CLI / SDKs ──http──► :9297 keepstack ──https──► :9298 AWS Emulator (Docker, --auth off)
                                          │
                                          └── .keepstack/journal.db   (SQLite)
```

## Lifecycle

1. `keepstack up` binds its port **first**. If another keepstack already owns it, the new process exits with code 1
   before touching the journal or Docker — so a mistaken second `keepstack up --reset` cannot wipe a running state.
2. It builds the `keepstack-emulator` image if it does not exist (from the official AWS Emulator package; the
   architecture is picked automatically).
3. It starts a clean emulator container (`keepstack-<port>`) and **replays** the journal.
4. It serves traffic. A watcher thread waits on the container; if it dies (Docker restart, OOM, `docker kill`), the
   watcher starts a new one and replays again. Requests wait during a restore (up to 5 minutes, then HTTP 503).
5. Ctrl+C or `SIGTERM` stops the container. The journal stays on disk.

## Recording

Each request is classified by its AWS operation name, taken from `X-Amz-Target` (JSON protocols), the `Action`
parameter (query protocols) or the Smithy RPC v2 path. Header lookups are case-insensitive.

| Classified as | Rule |
|---|---|
| Read (passed through) | Operation starts with `Describe`, `Get`, `List`, `Head`, `Query`, `Scan`, `BatchGet`, `Search`, `Select`, `Lookup`, `Check`, `Validate`, `Estimate`, `Preview` or `Simulate`; or a REST call with `GET`/`HEAD`/`OPTIONS` |
| Write (journaled) | Everything else |
| Consuming read | `ReceiveMessage` is journaled only when it returned messages, so consumed SQS messages do not reappear |

Writes are serialized and **journaled before they are forwarded**:

1. Insert the request into SQLite with `status = 0` (pending). If SQLite fails, the client gets an error and the
   emulator is never touched.
2. Forward it. If the emulator rejects it, the row is deleted.
3. On success, store the status and the response (as the client saw it).

Rows still pending at the next start (keepstack was killed mid-call) are skipped and reported in the log.

## Replay

Rows are replayed in order against the new emulator. The emulator is faster than AWS at accepting calls but some
resources still need time to settle (a DynamoDB table is `CREATING` for a few seconds), so a failing call is retried
with exponential backoff for up to 30 seconds. The log ends with:

```text
[keepstack] state restored: 23 calls replayed, 0 failed, 16 values translated
```

## Translating generated values

Server-generated values differ between the original run and the replay: KMS key IDs are random UUIDs, Secrets Manager
appends a random suffix to ARNs, Route 53 zone IDs are random, SQS receipt handles change, and so on. Terraform state
and your scripts hold the original values.

For every replayed call, keepstack compares the **recorded response** with the **new response**:

1. Base64 blobs (containing `+` or ending in `=`) are compared as whole units.
2. The rest is split into tokens (`[A-Za-z0-9_-]+`). Responses with the same token count are paired by position; if
   the structure differs, tokens are only paired when they follow the same key (difflib alignment, capped at 4,000
   tokens to keep it linear in practice).
3. A pair is learned only if both values **look generated**: at least 6 characters and either mixing letters and
   digits (`vpc-0a1b…`, UUIDs, `Z0123ABC…`) or a hyphenated token whose last segment mixes upper and lower case
   (`db-pass-AbCdEf`). Words, statuses (`CREATING`/`ACTIVE`), timestamps, numbers and request IDs are never learned.

The learned table is applied with a single compiled regex at token boundaries (also after URL-encoded separators like
`%2F`): **requests** are rewritten original → current, **responses** current → original.

If the fresh emulator hands out a value that the client already associates with a *different* resource (possible when
counters drift), keepstack gives the new value a unique alias with the same prefix, so two resources can never
appear with the same ID.

## Limitations

- **Heuristic.** A generated value that never appears in a write response cannot be learned, and very large responses
  whose structure changed between runs are not aligned.
- **Ordering of concurrent consumers.** `ReceiveMessage` is journaled without the write lock (so a 20-second long poll
  does not block other writes). With several concurrent consumers the journal order may differ from the real one.
- **Interrupted writes.** If keepstack dies after the emulator applied a write but before the journal confirmed it,
  that write is skipped on replay (and reported).
- **Side effects run again.** Lambda `Invoke`, SNS → SQS deliveries and anything the emulator runs internally happen
  again when the calls that caused them are replayed.
- **In-memory bodies.** Requests and responses are buffered (no streaming). Tested with 20 MB multipart S3 uploads.
- **The journal only grows.** Compact it with `keepstack up --reset` followed by `terraform apply`.
