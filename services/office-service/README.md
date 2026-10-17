# Shared Office service

Private server-side queue for the existing `office-engine/engine.py`. It runs the
eight existing operations (`read`, `inspect`, `ocr`, `create`, `edit`, `convert`,
`pdf`, `image`) in isolated job directories and separately cancellable process
groups. Desktop and cloud adapters upload only explicitly selected files and
save returned artifacts under a new local output path. The worker never mounts
employee workspaces and does not fetch arbitrary URLs.

## Gateway contract

Only the Gateway can call this service on port 8093. Every Office request needs:

- `X-Office-Service-Key`: private service credential, 32–256 bytes, matching the
  read-only file `OFFICE_SERVICE_SECRET_FILE` (default `/run/secrets/office-service`).
- `X-Office-Owner`: SHA256 of `megmeet-office-owner-v1:` plus trusted `actor.user_no`.
- `X-Office-Scope`: SHA256 of `megmeet-office-scope-v1:` plus trusted
  `actor.auth_session_id`.

Owner and scope are lowercase 64-hex identifiers. The Gateway discards inbound
copies of these headers, authenticates the browser cookie/desktop bearer/cloud
lease, and generates the internal identity. Neither IAM credentials, personal
model keys nor tokens reach this worker. A foreign owner or scope gets the same
404 as an absent job. `/health` exposes only a constant liveness status.

| Method and path | Contract |
| --- | --- |
| `GET /v1/office/capabilities` | Existing engine capability object (`protocol: megmeet-office-v1`) plus `remote_limits`; cached after first bounded engine call. |
| `POST /v1/office/jobs` | JSON `{protocol: "megmeet-office-remote-v1", request: {operation, input?, output?, options?}, inputs: [{path, size_bytes}]}`. Returns 201 and an `uploading` job. |
| `PUT /v1/office/jobs/{job_id}/inputs/{index}` | Raw bytes, exact declared length. An unsuccessful upload is removed and may be retried. Successful uploads cannot be overwritten. |
| `POST /v1/office/jobs/{job_id}/run` | Requires every declared input. Returns 202 after admission to the bounded queue. |
| `GET /v1/office/jobs/{job_id}` | State, original engine result on success, or a fixed error code. |
| `GET /v1/office/jobs/{job_id}/outputs/{id}` | Successful artifact only; streaming bytes with `Content-Length` and `X-Office-SHA256`. |
| `DELETE /v1/office/jobs/{job_id}` | Cancel queued work or kill the running process group, remove inputs/output, return `cancelled`. Short-lived cancellation tombstones may be reclaimed for capacity; a later 404 also means no accessible job remains. |

Every job response has `protocol`, 32-hex `job_id`, `state`, and `outputs`.
States are `uploading`, `queued`, `running`, `succeeded`, `failed`, `cancelled`.
Queued responses add a one-based `queue_position` (position, not an ETA).
Successful responses add `result`, exactly the engine's result object, and at
most one output `{id: "0", path, size_bytes, sha256}`. Read/inspect/OCR have an
empty output list. Failure adds `error: {code}`; stderr, exception strings and
absolute server paths are never returned. HTTP validation errors use
`{detail: "FIXED_CODE"}`. JSON responses are bounded at 2 MiB.

All paths are job-relative. No executable, workspace, environment or limit can
be selected by a request. PDF merge may refer only to declared inputs in
`options.inputs`. Any unselected extra input is refused. Output must differ from
every input and be a new file. Regular-file, link-count, size and digest checks
run before an artifact is exposed. Client adapters still enforce their own
session attachment and write permissions and verify the downloaded digest.

## Resource and lifecycle policy

One ASGI worker only; an exclusive spool lock refuses another process using the
same root. Default global execution concurrency is **2** (configurable 1–4),
with at most **1** running job per owner. The scheduler skips an owner already
running so one employee cannot block another employee's available slot.

| Setting | Default | Bound |
| --- | --- | --- |
| `OFFICE_CONCURRENCY` | 2 | 1–4 |
| `OFFICE_QUEUE_LIMIT` | 16 | 1–64 queued jobs |
| `OFFICE_OWNER_PENDING` | 4 | 1–16 unfinished jobs per owner |
| `OFFICE_MAX_JOBS` | 64 | 1–256 retained records |
| `OFFICE_SPOOL_BYTES` | 2 GiB | 128 MiB–16 GiB reserved input/output |
| `OFFICE_TTL_SECONDS` | 900 | 30–3600 seconds |
| `OFFICE_TIMEOUT_SECONDS` | 120 | 1–300 seconds per execution/upload |
| `OFFICE_CHILD_MEMORY_BYTES` | 3 GiB | 256 MiB–8 GiB address space per child |

Fixed protocol limits: 20 input files; 64 MiB each, 128 MiB total; output 128 MiB;
request 1 MiB and options 512 KiB. Input bytes plus the maximum possible output
are reserved before accepting a job. Terminal jobs retain only their output and
bounded JSON result for the TTL; cancellation removes files immediately. A
five-second reaper removes expired completed, abandoned-upload and queued jobs.
Restart discards this service's previous temporary jobs; clients must resubmit.
This queue is not durable document storage.

Engine children inherit an allowlisted environment, unique HOME/TMPDIR, and no
service secret or proxy/model/IAM variables. The Linux bootstrap limits address
space, CPU time, file size and open files, disables core dumps, and caps numeric
library threads at one. Timeout, cancellation and shutdown terminate the whole
owned process group, including converter children. Input/output policy reuses
the engine's macro, external-resource and archive checks. It is not a claim that
Python path checks constitute an OS sandbox against document-parser exploits.

## Container deployment boundary

The Dockerfile reuses the verified server Office runtime; it does not build a
desktop package or publish a release. Use a private `internal: true` Docker
network shared only with Gateway, **no published host port**, non-root UID
10001, read-only rootfs, all capabilities dropped, `no-new-privileges`, pids 256,
CPU 2 and an initial memory ceiling of 8 GiB. Mount only the service secret
read-only and an ephemeral, size-limited job spool, for example a 2 GiB tmpfs
at `/var/lib/megmeet-office/jobs` with uid/gid 10001 and mode 0700. The tmpfs or
storage quota also bounds converter scratch files; application reservations
alone cannot cap all library temporary files. `/tmp` can use a separate small
tmpfs. No Docker socket, user file volumes or outbound network belongs here.

The reported 32 GiB server memory does not establish a safe larger concurrency;
measure OCR/conversion memory and system load before increasing it. A process
memory limit may cause a task to fail safely on unusually large documents.
Office health is separate from successful company IAM/TokenKey login.

## Local verification

Run `python -m unittest discover -s new-system/services/office-service -p test_service.py -v`
from a Linux environment with FastAPI and HTTPX. Tests use generated documents,
real engine subprocesses for text/inspection/unsafe-archive checks, real
parent/grandchild processes for cancellation and timeout, and fake completion
gates for deterministic scheduling. Office-format/OCR dependencies are supplied
by the server runtime; the existing `office-engine/test_engine.py` remains its
format-level suite. Local passing tests are not company deployment evidence.
