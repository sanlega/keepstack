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
   before touching the journal or Docker, so a mistaken second `keepstack up --reset` cannot wipe a running state.
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

## Compatibility shims

`keepstack up --shims` enables small, targeted rewrites for known AWS Emulator bugs. They run on the emulator side of
the ID translation, on live traffic **and** on replay, and are off by default so they never hide an incompatibility
you did not ask to hide. Once a state directory has been used with `--shims`, the choice is remembered in the
journal (`meta['shims']`) so the recorded calls replay the same way; `--reset` forgets it.

| Shim | Emulator bug | Rewrite |
|---|---|---|
| `ec2-ipprotocol-all-ports` | With `IpProtocol=-1`, AWS ignores `FromPort`/`ToPort`; the emulator requires them to match, so Terraform cannot create `aws_security_group` (`InvalidPermission.NotFound` when revoking the default egress rule). | Drop `FromPort`/`ToPort` from `-1` permissions in `Authorize*`/`Revoke*SecurityGroup*` calls. |
| `dynamodb-ondemand-throughput` | On-demand tables return `OnDemandThroughput {-1, -1}`, which AWS omits when no limit is set; Terraform shows a perpetual diff. | Remove that object from `CreateTable`/`DescribeTable`/`UpdateTable` responses when both values are `-1`. |

## Snapshots and compaction

- **Snapshots** (`keepstack snapshot save/restore`) copy the journal with SQLite's backup API, so saving is safe while
  keepstack runs. `--include FILE` stores extra files (typically `terraform.tfstate`) and puts them back on restore.
  Restoring or importing requires keepstack to be stopped and first saves the current state as `pre-restore`.
- **Compaction** (`keepstack compact`) drops resources that were created and later deleted: a `Delete<Noun>` is paired
  with the latest `Create<Noun>` whose request or response contains the deleted resource's identifiers, and the calls
  in between that reference only that resource go with them; for REST APIs a `DELETE` on a path cancels earlier writes
  to it (versioned S3 buckets are left alone). Anything shared with a surviving resource is kept. The result is
  replayed in a throwaway emulator before it replaces the journal, and the previous journal is saved as `pre-compact`.

## Crash safety

The journal is written **before** a write reaches the emulator and confirmed only when the emulator answered 2xx,
in an SQLite transaction. Every way of dying mid-apply ends in a state that matches what Terraform was told:

| What dies in the middle of `terraform apply` | What happens |
|---|---|
| **The emulator** (`docker kill`, Docker restart, OOM) | keepstack notices, starts a new emulator and replays the journal while new requests wait. The SDK's retries go through once the state is back: the apply usually finishes by itself. |
| **keepstack** (`kill -9`), restarted while Terraform retries | A write that was in flight is left unconfirmed and skipped on the next start (the client never got an answer, so Terraform does not have it either). Terraform's retries then succeed. |
| **Everything at once** (power cut) | The same, plus Terraform dies: run `terraform apply` again and it converges. |
| **The client gives up** while its request waits (restore, slow write) | The request is not executed: keepstack checks the connection before running a request that waited. Otherwise the resource would be created behind the client's back and its retry would fail with "already exists". |

This is exercised by [`scripts/chaos.py`](../scripts/chaos.py), which kills one of the three at a random moment of
an apply of the quickstart example and requires the re-apply to converge and the plan to stay clean after another
restart. It runs nightly in CI ([`chaos.yml`](../.github/workflows/chaos.yml)); the deterministic case of the
abandoned write is an end-to-end test (`tests/test_e2e_crash.py`).

## Limitations

- **Heuristic.** A generated value that never appears in a write response cannot be learned, and very large responses
  whose structure changed between runs are not aligned.
- **Ordering of concurrent consumers.** `ReceiveMessage` is journaled without the write lock (so a 20-second long poll
  does not block other writes). With several concurrent consumers the journal order may differ from the real one.
- **Interrupted writes.** If keepstack dies after the emulator applied a write but before the journal confirmed it,
  that write is skipped on replay (and reported); the client never got an answer, so it retries or reports an error.
  A write the client abandoned after the emulator started running it cannot be undone (as on AWS).
- **Side effects run again.** Lambda `Invoke`, SNS → SQS deliveries and anything the emulator runs internally happen
  again when the calls that caused them are replayed.
- **In-memory bodies.** Requests and responses are buffered (no streaming). Tested with 20 MB multipart S3 uploads.
- **The journal only grows** until `keepstack compact` drops the resources that were created and later deleted.
- **CloudFormation-generated names.** Replaying `CreateStack` rebuilds every resource of the template and the stack
  ID is translated, but physical names that CloudFormation generates (a queue without `QueueName` becomes
  `stack-Q-C212895A2991`) only appear in read responses (`DescribeStackResources`), which are not journaled, so they
  get new values after a restart. Give explicit names to resources you reference from outside the stack
  ([#19](https://github.com/sanlega/keepstack/issues/19) tracks translating them).
