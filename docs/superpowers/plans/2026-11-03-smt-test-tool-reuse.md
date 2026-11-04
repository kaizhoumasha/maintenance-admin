# Formal SMT test tool reuse

**Goal:** Merge the duplicated formal SMT harnesses without changing existing test bodies, collection, runtime behavior or evidence.

**Scope:** `tests/helpers.py::formal_manual_fixture` and `tests/businesses/smt_manual/factory_profile.py::profile_fixture`. Keep earlier items 1–5 uncommitted. No production, dependency, CI, commit or push changes.

**Design:** Extend the existing formal harness with the supplied profile and its evidence destination. The profile entry delegates to it. Share real runtime assembly, loopback protocol responses, callbacks/results, dispatch and cleanup; preserve the explicit profile differences in line configuration, default inbound response, raw row queries, route/target assertions and evidence retention.

**Constraints:** Retain default prefix-owned cleanup, profile exact-code cleanup, preexisting table-row snapshots, real transaction hooks and rollback, profile assertions before frozen intents and each dispatch, and evidence/zero-row cleanup reports even when the test body fails. Keep profile devices without a bound role and four-line worker cases.

## Steps

- [x] Measure the five affected test files before editing on dedicated PostgreSQL/Redis stores.
- [x] Consolidate the harnesses, preserving callers and all test functions/assertions.
- [x] Compare the same affected files and inspect exceptional-body evidence/cleanup behavior.
- [x] Run all three CI groups, collection equality, lint/format and an independent review.
- [x] Record actual timings and remove task-owned containers/volumes.

## Evidence

Initial source snapshot and measurements: `/Users/kaizhou/.codex/tmp/ci-smt-tools-i8he3m75`.

## Paired measurements and review

- Same five affected files, same commands/instrumentation and dedicated store ports: 43 passed in 80.17s before and 80.27s after (+0.10s / +0.12%, effectively unchanged).
- Setup/call/teardown phase sums: 6.04/62.65/11.34s before; 5.44/63.76/10.97s after. No runtime speedup claimed.
- CREATE 256, DROP 4, TRUNCATE 37 and PostgreSQL metadata probes 45 in both runs. Native database and protocol work remains intact.
- Profile module shrinks 463 -> 18 lines; shared helpers grow by 150 lines, net reduction 295 lines. No new dependency or runtime cache.
- All 1881 existing test functions, decorators and assertions are byte-identical; all five affected files and ordered 43 collection IDs/results are unchanged.
- Two temporary external contracts pass in 2.00s: profile/generic inbound defaults and overrides, profile target/route rejection before sending, evidence and zero-row cleanup after an intentional test-body assertion, four generated lines and prefix-extra route cleanup preserving a preexisting line. The checks remain outside the repository.
- Independent source review compared both saved implementations and found no actionable findings. Existing setup/evidence-write failure semantics are retained.
- Ruff check, Ruff format check (673 files) and diff whitespace checks pass.
- The initial 33 dirty files are preserved: 32 are byte-identical; only tests/helpers.py has the item 6 addition. The previously clean profile module is the only other source file edited.

## Final full verification

| Group | Elapsed | Passed | Skipped |
| --- | ---: | ---: | ---: |
| Migrations | 332.23s | 182 | 74 |
| Workflows | 331.90s | 964 | 6 |
| Remaining | 283.22s | 4273 | 41 |

- Total 5419 passed / 121 skipped; all 5540 ordered collection IDs and per-group outcome counts match the preceding items-1–5 run. These full-group timings are local validation records, not another paired benchmark.
- Prior group timings were 341.12 / 341.38 / 289.82s. The affected-file paired benchmark remains the relevant comparison for item 6: 80.17 -> 80.27s, no measurable speedup.
- All three disposable PostgreSQL databases had zero remaining owned fixture schemas. All six task-owned PostgreSQL/Redis containers and their anonymous volumes were removed; no task container remains.
- Earlier dirty content is retained. Branch remains feature/ci-parallel-fixtures; no commit, push, remote CI or main-checkout operation performed.
- Artifacts: comparison.json, current.diff, before/after-workflows logs and metrics, final group logs and metrics, external contract checks, review.txt and cleanup-audit.json in the evidence directory above.
