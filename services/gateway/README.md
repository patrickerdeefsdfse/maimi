# Gateway DSH runtime input

The Gateway keeps DSH optional. The repository contains an empty
`vendor-wheels/` directory; on the Linux test host, copy the already verified
`deepseek_harness_runtime_bin` and `deepseek_harness_sdk` wheels into that
directory before building. The wheels are test deployment inputs and must not
be committed to GitLab. Keep `DSH_ENABLED=false` until the image reports the
expected SDK/runtime versions and the model gateway endpoint is separately
verified.

When DSH is explicitly enabled, the adapter takes the DSH SDK path first. The
platform `tokenkey` provider maps to DSH's `deepseek-official` provider while
`TOKENKEY_BASE_URL` and the read-only secret file supply the internal compatible
endpoint and credential. The REST TokenKey backend remains the default until
that SDK path passes a bounded model probe.
