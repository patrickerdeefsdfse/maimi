# MEGMEET office Host adapter v1

This is a separately staged DSH Host plugin. It adds office document tools without
editing DSH core packages, renderer bundles, or native filesystem tools.
Verified against the pinned DSH `0.1.6-alpha.1` runtime.

## Runtime files and loading

Stage only `index.mjs`, `adapter.mjs`, and `transport.mjs` into
`<runtime>/office-adapter/`, next to the runtime's `node_modules`. The tests and
probe are development files. The release owner must include these three files,
the independent engine and its Python/parser assets in its integrity manifest.
The process-local patch uses an absolute `file:` URL, computed from the installed
runtime path on each launch, rather than a developer machine path:

```json
[{"insert":[{"id":"megmeet-office-adapter","name":"file:///installed/runtime/office-adapter/index.mjs","config":{"engineRoot":"/opt/office","pythonExecutable":"/opt/office/venv/bin/python","timeoutMs":120000}}]}]
```

On Windows `pythonExecutable` points to the bundled `python/python.exe` under
the deployed engine root. Both paths come from trusted launch configuration.
The model cannot select an executable, environment, working directory or limits.

## Tools and engine contract

- `office_capabilities` returns the actual installed engine's supported formats,
  operation options and limitations; models must consult it before writing.
- `office_attachments` lists the latest 100 unique user-uploaded references in the
  current session, with full `sha256:<64 hex>` IDs. Native text placeholders expose
  only short digests or absolute storage paths, which are not accepted as input.
- `office_document` accepts `operation`, workspace-relative `input` or the current
  session's `attachment_id`, optional **new** `output`, and operation `options`.
  Its operations are inspect/read/create/edit/convert/pdf/image/ocr. PDF merge
  takes workspace-relative `options.inputs` instead of a primary input.

One isolated Python process receives one UTF-8 JSON object on stdin:

```json
{"protocol":"megmeet-office-v1","operation":"read","workspace":"<session's trusted absolute cwd>","input":"report.docx","options":{"offset":0,"limit":30000},"limits":{"maxInputBytes":67108864,"maxOutputBytes":134217728,"maxTextChars":120000}}
```

The response is `{"protocol":"megmeet-office-v1","ok":true,"result":...}` or
`{"protocol":"megmeet-office-v1","ok":false,"error":{"code":"...","message":"..."}}`.
Each engine invocation uses `python -I -B engine.py`, no shell, a small allowlisted
environment, and no inherited IAM/TokenKey/proxy credentials. Requests are bounded
to 1 MiB and stdout to 2 MiB. Cancellation and timeout terminate the owned process
tree and wait for exit before attachment cleanup. stderr is drained, not returned
to the model. Engine errors remain actionable without arbitrary diagnostic text.
The trusted interpreter's invocation path is preserved: resolving the executable
symlink inside a Linux venv would bypass that venv's dependency environment.
One engine invocation is admitted per Host across all its sessions. A concurrent
request receives `OFFICE_BUSY` and a retry-later message; there is no unbounded
queue. Completion, cancellation and failure all release this admission slot.

## Workspace, attachments and permissions

The session header supplies cwd. DSH `ctx.fs.resolve/contains/processPath` must
map it to the same local execution filesystem. Remote execution backends without
that mapping are refused. Inputs are regular files inside cwd; absolute paths,
traversal, links/junctions, hard links, Windows device/ADS names and adapter-private
staging paths are refused. All PDF merge inputs are individually checked and have
a 64 MiB combined limit. Outputs must not exist and their parent must already exist.
The engine performs its own path checks and fail-if-exists publication too.

Uploaded refs are authorized through `sessionQuery.observeSession(session.id,
{projectionMode:'none'})`, examining actual user/message file or image parts.
Assistant text and another session cannot grant attachment access. Provider bytes
are copied into a random private directory beneath the selected workspace and
checked against the recorded SHA-256 and byte count. Only the owned file/directory
are removed after processing, including errors. Attachment storage itself is never
deleted or edited. A read-only operation may create this private processing cache;
it does not write a user-selected deliverable.

Creating/editing/converting PDFs, images and office files requires the current
native sandbox policy to be workspace-write or danger-full-access; missing policy
or read-only denies the operation. This adapter does not elevate permissions or
accept an escalation switch. It is registered via native `defineTool`, so existing
DSH pre-execution policy and approval denial still run before the tool body. A
document's content is untrusted data and cannot select commands, enable macros or
change tool policy. Outputs can be shown through DSH's existing `present` tool.

This is application-level containment, not a kernel sandbox against a malicious
same-OS-account process racing filesystem names or a parser vulnerability. Python
network audit restrictions also do not create an OS network sandbox around native
converter subprocesses. The independently maintained engine owns format validation
and active/external-content rejection. Native DSH tools outside this adapter retain
their existing policy. UI preview and OCR do not claim general visual reasoning.

## Verification and upgrade boundary

From the repository root on the supplied Windows development runtime:

```powershell
& new-system/desktop-local/runtime/bin/node.exe --test new-system/dev-local/dsh/office-adapter/test-office-adapter.mjs
& new-system/desktop-local/runtime/bin/node.exe new-system/dev-local/dsh/office-adapter/probe-native-host.mjs
```

The unit/integration suite uses actual DSH LocalFS, defineTool, SessionStore and
sessionQuery plus synthetic files and the isolated Python engine. The Host probe
loads the extension with a file URL into an entirely new DSH_HOME, denies external
Node TCP access, uses native attachment storage and tools.execute, and verifies
read-only and pre-execute denial. It does not call an external model or company IAM.
It prints bounded evidence and leaves its synthetic temporary directory for review.

Re-run both when DSH, the adapter or engine is upgraded. Only these upstream seams
need compatibility validation: tools/defineTool and tools.execute, execution agent
session header, FS mapping, sandboxPolicy.resolve, exact-session observation leases,
and attachment provider read methods. New engine versions must continue this JSON
protocol or use a separately versioned adapter. Final packaged Windows/Linux tests
and model-driven chat acceptance remain the release integrator's responsibility.

The Linux candidate can be checked, once its build has completed, with:

```powershell
wsl -d MegmeetDev -- python3 /home/megmeet/project/new-system/dev-local/dsh/office-adapter/probe-linux.py
```

That development-only script resolves `megmeet-personal-dsh:office-candidate` to
one immutable repository digest, checks the embedded engine/adapter against source, and runs the
engine format suite and live native tool probe in its own network-none container.
It uses the current cloud limits (768 MiB memory, no swap, one CPU, 128 PIDs,
64 MiB temporary filesystem), reads aggregate peak memory and OOM counters, and
then removes only that randomly named probe container. No employee state/volume
is mounted. A missing image reports not-ready once rather than polling.
`--reviewed-transport` explicitly mounts only the reviewed transport source over
the candidate read-only and prints its SHA; this is a diagnostic and cannot prove
baked-image equality. `--capacity office` is a separate 1536 MiB/128 MiB temporary
filesystem comparison. It does not change production resource settings.

The 2026-09-18 isolated Linux comparison found that 768 MiB was insufficient:
eight native checks passed, then live OCR was OOM-killed. With 1536 MiB/no swap,
one CPU and 128 MiB `/tmp`, all 15 real-format tests and nine native checks passed;
the combined cgroup peak was 1,104,904,192 bytes, with no memory-limit or OOM
events. This comparison used a clearly labelled read-only reviewed transport
overlay. The release integrator must still validate the final baked image and
adopt appropriate cloud limits. These small synthetic documents do not establish
maximum-file-size performance, multiple concurrent OCR capacity or company load.

Final baked-image validation also passed without that overlay: platform manifest
`sha256:5bd16c8a299b629796f3254756673162df09fcb785a40316f4f7af30698fe8c9`,
all embedded runtime sources matched, 15 formats in 16.868 seconds and nine native
checks in 26.276 seconds. Under the 1536 MiB profile, the cgroup peak was
989,003,776 bytes with no memory-limit/OOM events. Evidence is in
`output/office-adapter-20260918/linux-office-probe-baked-final.txt` at the project
root. The final transport includes Host-wide single-task admission and its
18-test adapter suite passed. No company model or IAM endpoint was called.
