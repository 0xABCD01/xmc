---
name: XMC Release
description: Orchestrates an end-to-end XMC release workflow — deploying the latest main to nightly, running and verifying e2e tests, capturing visual snapshot galleries, generating and iterating on structured release notes with the user, and publishing the release. Use when preparing, orchestrating, or publishing a formal XMC release from main.
---

# XMC Release Orchestration Playbook

This skill guides an agent through preparing, validating, and publishing an XMC release across 5 sequential phases.

> [!IMPORTANT]
> All execution steps in this skill MUST strictly adhere to the policy rules defined in [Core Engineering Standards](../../rules/core_engineering.md) and [Skills & Automation Guidelines](../../rules/skills_and_automation.md).

### Resumption & Phase Selection
- **Idempotency & Recent Run Check**: If resuming an interrupted release or if `main` has already been deployed and tested today, load `TARGET_SHA` from `${STATE_FILE}`, verify that `git show "${TARGET_SHA}:VERSION"` matches the persisted release `version` (and is not a `-dev` version), and check for existing successful workflow runs targeting that commit before re-triggering using `--find-existing`:
  ```bash
  STATE_FILE="${ARTIFACT_DIR:-/tmp}/release_state.json"
  if [[ -z "${TARGET_SHA:-}" && -f "${STATE_FILE}" ]]; then
    TARGET_SHA=$(jq -r '.target_sha // empty' "${STATE_FILE}")
  fi
  : "${TARGET_SHA:?Error: TARGET_SHA not set and not found in ${STATE_FILE}; run Prerequisites first}"
  EXPECTED_VERSION=$(jq -r '.version // empty' "${STATE_FILE}")
  ACTUAL_VERSION=$(git show "${TARGET_SHA}:VERSION")
  echo "Persisted version: ${EXPECTED_VERSION} | Commit VERSION: ${ACTUAL_VERSION}"
  test -n "${EXPECTED_VERSION}" && test "${ACTUAL_VERSION}" = "${EXPECTED_VERSION}"

  PYTHONPATH=. python3 tools/release/dispatch_and_watch.py \
    --workflow nightly-build.yml \
    --target-sha "${TARGET_SHA}" \
    --find-existing \
    --state-file "${STATE_FILE}"
  ```
- **Direct Entry**: If deployment and E2E tests have already succeeded for the target `TARGET_SHA` commit (whose `VERSION` matches the target release version), the agent may proceed directly to Phase 4 or Phase 5. Load `TARGET_SHA` from `"${STATE_FILE}"` if unset and verify `VERSION`:
  ```bash
  STATE_FILE="${STATE_FILE:-${ARTIFACT_DIR:-/tmp}/release_state.json}"
  if [[ -z "${TARGET_SHA:-}" && -f "${STATE_FILE}" ]]; then
    TARGET_SHA=$(jq -r '.target_sha // empty' "${STATE_FILE}")
  fi
  : "${TARGET_SHA:?Error: TARGET_SHA not set and not found in ${STATE_FILE}; run Prerequisites first}"
  EXPECTED_VERSION=$(jq -r '.version // empty' "${STATE_FILE}")
  ACTUAL_VERSION=$(git show "${TARGET_SHA}:VERSION")
  echo "Persisted version: ${EXPECTED_VERSION} | Commit VERSION: ${ACTUAL_VERSION}"
  test -n "${EXPECTED_VERSION}" && test "${ACTUAL_VERSION}" = "${EXPECTED_VERSION}"
  ```
- **Post-Review Resumption**: When resuming after the user approval gate, proceed with executing the publishing step in Phase 5 using the release notes at `"${ARTIFACT_DIR:-/tmp}/release_notes_v<version>.md"` without re-running earlier phases.

> [!CAUTION]
> **STRICT INVARIANT: RELEASE CONFIRMATION GATES & SAFEGUARDS**
> 1. If any deployment or E2E test fails, **ABORT IMMEDIATELY AND NOTIFY THE USER**. Never proceed with subsequent steps or release generation on a broken build.
> 2. **NEVER** create a git tag or publish a GitHub release without explicit, affirmative confirmation from the user in the current conversation turn (see [Release Publishing Gate](../xmc_generate_release_notes/SKILL.md#6-mandatory-confirmation-gate--release-publishing)).

---

## Prerequisites & Tools

- Ensure `gh` CLI is installed and authenticated (`gh auth status`).
- Ensure working directory is the root of the XMC repository (can be executed from any branch or git worktree).
- Fetch the latest remote tracking refs and tags to ensure local tooling has the latest commits and tags from `main` without needing to check out or switch branches:
  ```bash
  git fetch origin main --tags
  ```
- **Determine Release `<version>`, Prepare Root `VERSION`, & Persist Target SHA**:
  Root `VERSION` on `main` holds the next development version (e.g., `1.2.0-dev`) between releases, and `tools/release/publish_release.py` enforces that `VERSION` at `${TARGET_SHA}` matches `--tag` (ignoring any leading `v`).
  1. Run [Release Range & Discovery](../xmc_generate_release_notes/SKILL.md#1-release-range--git-reference-discovery) and [Deterministic Metadata Extraction](../xmc_generate_release_notes/SKILL.md#2-deterministic-metadata-extraction--approved-flags-discovery) (including interactive flag approval) against `--target-ref origin/main` to obtain `recommended_version`, and confirm the target release `<version>` with the user before triggering nightly runs.
  2. Inspect `VERSION` on `origin/main`:
     ```bash
     git show origin/main:VERSION
     ```
     If `VERSION` does not yet equal `<version>` (for example, it still ends in `-dev` or needs a patch/major override), follow the `VERSION` bump procedure in [Draft Generation & Interactive Review Gate](../xmc_generate_release_notes/SKILL.md#5-draft-generation--interactive-review-gate) so nightly deployment, E2E verification, visual snapshots, and `publish_release.py` all pin the exact same commit. *(If Phase 2, 3, 4, or 5 aborts after the pre-release bump PR has merged, keep `VERSION` at `<version>` while fixing and retrying the release, or ask the user whether to open a Draft PR restoring `VERSION` to its pre-bump value if the release is postponed.)*
  3. Resolve and persist `${TARGET_SHA}` and `<version>` to `"${ARTIFACT_DIR:-/tmp}/release_state.json"`:
     ```bash
     VERSION="<version>"
     TARGET_SHA=$(git rev-parse origin/main)
     STATE_FILE="${ARTIFACT_DIR:-/tmp}/release_state.json"
     jq -n --arg sha "${TARGET_SHA}" --arg v "${VERSION}" '{target_sha: $sha, version: $v}' > "${STATE_FILE}"
     ```
- Helper tools are executed via Python with `PYTHONPATH=.` configured.

---

## Phase 1: Deploy Newest Main to Nightly

Trigger the nightly build and deployment workflow using `tools/release/dispatch_and_watch.py`:

```bash
# Dispatch and watch nightly deployment
DEPLOY_RESPONSE=$(PYTHONPATH=. python3 tools/release/dispatch_and_watch.py \
  --workflow nightly-build.yml \
  --target-sha "${TARGET_SHA}" \
  --state-file "${STATE_FILE}")
```

Notify the user:
> *"Triggered nightly deployment workflow (`nightly-build.yml`) for commit `<TARGET_SHA>`. Monitoring progress..."*

---

## Phase 2: Verify Deployment Outcome & Gate

Inspect the JSON response from `dispatch_and_watch.py`:

```bash
DEPLOY_CONCLUSION=$(echo "${DEPLOY_RESPONSE}" | jq -r '.result.conclusion // empty')
DEPLOY_RUN_URL=$(echo "${DEPLOY_RESPONSE}" | jq -r '.result.url // empty')
```

### Outcome Handling:
- **If `DEPLOY_CONCLUSION != "success"` (FAILED / CANCELLED / SHA MISMATCH)**:
  1. Inspect the outcome details:
     ```bash
     echo "${DEPLOY_RESPONSE}" | jq -r '.result.conclusion // .conclusion'
     echo "${DEPLOY_RESPONSE}" | jq -r '.result.failed_jobs // empty'
     ```
  2. **ABORT THE RELEASE PIPELINE IMMEDIATELY**.
  3. Notify the user with exact outcome details:
     - **If SHA Mismatch (`DEPLOY_CONCLUSION == "sha_mismatch"`)**:
       > *"🚨 **Release Aborted**: Commit SHA mismatch detected for `nightly-build.yml`."*
       > - **Expected Commit**: `<TARGET_SHA>`
       > - **Dispatched Commit**: `<echo "${DEPLOY_RESPONSE}" | jq -r '.result.head_sha'>`
       > - **Run URL**: `<DEPLOY_RUN_URL>`
     - **If Cancelled (`DEPLOY_CONCLUSION == "cancelled"`)**:
       > *"🚨 **Release Aborted**: Nightly deployment run was cancelled (`<DEPLOY_RUN_URL>`)."* (Include superseded run ID if present).
     - **If Failed (`DEPLOY_CONCLUSION == "failure"`)**:
       > *"🚨 **Release Aborted**: Nightly deployment failed on `main`."*
       > - **Run URL**: `<DEPLOY_RUN_URL>`
       > - **Failed Jobs**: `<List of failed jobs and steps from output JSON>`
  4. Stop execution and await user instructions.
- **If successful**:
  Notify the user:
  > *"Nightly deployment succeeded (`<DEPLOY_RUN_URL>`). Proceeding to E2E test suite..."*
  
  The successful completion of the deployment workflow on `main` serves as the prerequisite gate verifying that the nightly environment is updated before triggering E2E tests in Phase 3. Proceed to Phase 3.

---

## Phase 3: Run End-to-End Test Suite & Abort on Failure

Trigger and monitor the nightly examples E2E test suite against the freshly deployed nightly environment using `tools/release/dispatch_and_watch.py`:

```bash
E2E_RESPONSE=$(PYTHONPATH=. python3 tools/release/dispatch_and_watch.py \
  --workflow nightly-examples-e2e.yml \
  --target-sha "${TARGET_SHA}" \
  --state-file "${STATE_FILE}")
```

Inspect the test outcome:

```bash
E2E_CONCLUSION=$(echo "${E2E_RESPONSE}" | jq -r '.result.conclusion // .conclusion // empty')
E2E_RUN_URL=$(echo "${E2E_RESPONSE}" | jq -r '.result.url // .url // empty')
```

### Outcome Handling:
- **If `E2E_CONCLUSION != "success"`**:
  1. Inspect failed test jobs:
     ```bash
     echo "${E2E_RESPONSE}" | jq -r '.result.failed_jobs // empty'
     ```
  2. **ABORT THE RELEASE PIPELINE IMMEDIATELY**.
  3. Notify the user:
     > *"🚨 **Release Aborted**: Nightly E2E test suite failed."*
     > - **Run URL**: `<E2E_RUN_URL>`
     > - **Failed Examples**: `<List of failed test matrix items from output JSON>`
  4. Stop execution and await user instructions.
- **If all E2E tests pass**:
  Notify the user:
  > *"All E2E tests passed (`<E2E_RUN_URL>`). Proceeding to visual snapshot gallery..."*

---

## Phase 4: Generate Visual Snapshot Gallery

Generate and monitor the visual snapshot gallery verifying golden UI routes on the nightly deployment:

```bash
VISUAL_RESPONSE=$(PYTHONPATH=. python3 tools/release/dispatch_and_watch.py \
  --workflow nightly-visual-snapshot.yml \
  --target-sha "${TARGET_SHA}" \
  --state-file "${STATE_FILE}")
```

Inspect the gallery generation outcome:

```bash
VISUAL_CONCLUSION=$(echo "${VISUAL_RESPONSE}" | jq -r '.result.conclusion // .conclusion // empty')
VISUAL_RUN_URL=$(echo "${VISUAL_RESPONSE}" | jq -r '.result.url // .url // empty')
PAGE_URL=$(echo "${VISUAL_RESPONSE}" | jq -r '.result.page_url // .page_url // empty')
```

### Outcome Handling:
- **If visual snapshot run fails**:
  1. Inspect the failure details:
     ```bash
     echo "${VISUAL_RESPONSE}" | jq -r '.result.failed_jobs // empty'
     ```
  2. Notify the user with the failure details and ask whether to abort or proceed with release notes:
     > *"⚠️ **Visual Snapshot Gallery Failed**: Review logs at `<VISUAL_RUN_URL>`. Awaiting confirmation on whether to abort or proceed."*
- **If successful**:
  Provide the user with the snapshot gallery URL:
  > *"✅ Visual snapshot gallery generation completed."*
  > - **Run URL**: `<VISUAL_RUN_URL>`
  > - **Interactive Gallery Link**: `<PAGE_URL>` (defaults to `https://google.github.io/xmc/`)

---

## Phase 5: Generate Notes, Review, and Publish Release

Delegate the end-to-end release documentation, interactive user review, and publishing workflow to [xmc_generate_release_notes](../xmc_generate_release_notes/SKILL.md):

1. **Extract Metadata & Synthesize Notes**:
   Always re-run `extract_release_metadata.py` against the pinned `${TARGET_SHA}` so the release notes cover the exact tagged commit (including the pre-release `VERSION` bump PR and any commits merged before step 3 pinned `${TARGET_SHA}`). Reuse `metadata.approved_flags` already confirmed in Prerequisites without re-prompting the user (omit `--approved-flags` if none were approved):
   ```bash
   BASE_TAG="<base_tag>"
   APPROVED_FLAGS=(rod_deployment_enabled rodDeployment.enabled) # replace with metadata.approved_flags confirmed in Prerequisites
   STATE_FILE="${ARTIFACT_DIR:-/tmp}/release_state.json"
   if [[ -z "${TARGET_SHA:-}" && -f "${STATE_FILE}" ]]; then
     TARGET_SHA=$(jq -r '.target_sha // empty' "${STATE_FILE}")
   fi
   : "${TARGET_SHA:?Error: TARGET_SHA not set and not found in ${STATE_FILE}; run Prerequisites first}"
   python3 ./tools/release/extract_release_metadata.py \
       --base-ref "${BASE_TAG}" \
       --target-ref "${TARGET_SHA}" \
       --approved-flags "${APPROVED_FLAGS[@]}" \
       --output "${ARTIFACT_DIR:-/tmp}/release_metadata.json"
   ```
   Follow [Semantic Commit Classification](../xmc_generate_release_notes/SKILL.md#3-semantic-commit-classification--section-routing-agent-reasoning) and [Semantic Synthesis & Narrative Formatting](../xmc_generate_release_notes/SKILL.md#4-semantic-synthesis--narrative-formatting) to draft the release notes at `"${ARTIFACT_DIR:-/tmp}/release_notes_v<version>.md"`.

2. **Interactive Review**:
   Follow [Draft Generation & Interactive Review Gate](../xmc_generate_release_notes/SKILL.md#5-draft-generation--interactive-review-gate) to present the proposed version increment and draft notes to the user, refine content iteratively based on feedback, and handle any corrective `VERSION` bump (restarting from [Phase 1](#phase-1-deploy-newest-main-to-nightly) if the target commit changes).

3. **Preflight Validation & Publishing Confirmation Gate**:
   Follow [Preflight Validation (`--dry-run`)](../xmc_generate_release_notes/SKILL.md#5-draft-generation--interactive-review-gate) and [Mandatory Confirmation Gate & Release Publishing](../xmc_generate_release_notes/SKILL.md#6-mandatory-confirmation-gate--release-publishing) to run `--dry-run` against `${TARGET_SHA}`, enforce the mandatory affirmative confirmation gate, publish the draft release against `${TARGET_SHA}`, and initiate the post-release `VERSION` `-dev` bump Draft PR after user confirmation.
   *(Note: Never pass `--force`. If the tag already exists, abort and consult the user).*
