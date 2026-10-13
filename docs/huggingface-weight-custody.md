# Hugging Face Weight Custody

**Specification for the Nepher site (frontend and backend) and for this repository's miner, validator, and reward code.**

Status: draft for implementation. Owner of the site changes: Josh Brolin. The sections marked **Site** are the contract the backend and frontend must meet. The sections marked **This repo** are implemented in `nepher-cli` and `nepher-subnet`.

---

## 1. Decision

Large model weights (VLA, WAM, LWM; 10 GB and up) stay in the **miner's private Hugging Face repo**. Nepher never receives them during a tournament.

- The miner uploads a **small signed code archive** (`agent.yaml`, `brain/`, `train/`) to Nepher, plus a **seal**: the repo id, an immutable commit, and the SHA-256 of every weight file.
- The Nepher backend verifies the seal against Hugging Face, rejects stolen or duplicated weights, and keeps watching the repo.
- Validators download the sealed weights **directly from Hugging Face's CDN** through short-lived per-file URLs minted by the backend, re-hash every byte, and refuse anything that does not match the seal.
- After the private evaluation ends and an admin selects a winner, the **backend copies the sealed bytes to Nepher storage and re-hashes them**. The winner is announced and paid only after that copy succeeds. If the copy fails or differs from the seal, the winner is disqualified.

Cost effect: Nepher stores weights only for paid podium places. It stores one copy per paid place, and pays no per-miner storage or validator egress for the weights during evaluation.

The current RL tournament (**Unitree Go2 LiDAR Maze Challenge - Phase 1**) keeps the existing zip upload (`submission_mode = inline`). Its contract does not change.

### 1.1 Hugging Face behaviors this design relies on

Verified against Hugging Face documentation on 2026-10-13. Items marked *confirm in staging* depend on response shapes that the documentation does not pin down.

| Behavior | Source | Consequence |
|---|---|---|
| A repo in a **user** namespace has no collaborators. Only the owner can read it unless it is public or gated. | [Repository Settings](https://huggingface.co/docs/hub/repositories-settings) | A "bot collaborator" design is impossible. The miner supplies a **fine-grained, read-only token scoped to that one repo** instead. |
| Fine-grained tokens can be limited to specific repos. The token owner can revoke or rotate it at any time. | [User access tokens](https://huggingface.co/docs/hub/security-tokens) | Revocation makes the backend unable to read, which disqualifies the submission. This is intended. |
| `GET /api/whoami-v2` reports the token owner and auth method. | [Hub API](https://huggingface.co/.well-known/openapi.md) | Used to prove ownership and to reject over-broad tokens. *Confirm the fine-grained scope fields in staging.* |
| `GET /api/models/{ns}/{repo}/tree/{rev}?recursive=true` and `POST .../paths-info/{rev}` list files with LFS metadata. | Hub API | Source of per-file `lfs.oid` (SHA-256) and size. *Confirm that Xet-backed files still report a SHA-256 `lfs.oid`.* |
| `GET /{repo}/resolve/{rev}/{path}` with a bearer token answers LFS files with a **302/308 redirect to a signed CDN URL**. The bearer token must not be sent to the CDN. For LFS files, `X-Linked-Etag` is the SHA-256. | [huggingface_hub file download](https://huggingface.co/docs/huggingface_hub/package_reference/file_download) | The backend resolves each file with `follow_redirects=false` and gives validators only the signed `Location`. Validators hold no Hugging Face credential. *Confirm signed-URL lifetime and Range support in staging.* |
| Resolving by **commit SHA** pins content. | Same | Seals and leases always use the 40-hex commit, never a branch name. |

Validators always compute SHA-256 over the downloaded bytes. The hash computed by the validator, not any Hugging Face header, is the integrity check.

---

## 2. Tournament formats and submission modes

`format` describes what is being competed. `submission_mode` describes how the model reaches validators. One tournament has one of each.

| `format` | Example | `submission_mode` | Runtime | Weights in the zip | Seal | Base-weight allowlist |
|---|---|---|---|---|---|---|
| `rl` | Go2 LiDAR Maze, Leatherback, G1 | `inline` | `in_process` (checkpoint) | `best_policy/best_policy.pt` | no | n/a |
| `vla` | Franka tabletop (GR00T N1.7, pi0.5) | `huggingface` | `brain` | no | yes | public base checkpoint files |
| `wam` | World action models | `huggingface` | `brain` | no | yes | public base checkpoint files |
| `lwm` | Large world models | `huggingface` | `brain` | no | yes | public base checkpoint files |
| `task_generation` | Scene and task authoring | `inline` | task bundle | task bundle (size capped) | no | n/a |

Rules:

- `submission_mode` is chosen when the tournament is created and cannot change after the first submission.
- `huggingface` tournaments require `runtime: brain` in `task_config.yaml`. Any other runtime is a configuration error and validators refuse to evaluate.
- Unknown `format` values are accepted for display. Unknown `submission_mode` values are rejected by validators (fail closed).
- Per-format defaults (size limit, eval timeout, replicas) live in the tournament's `task_config.yaml` under `brain`. The existing `max_submission_gb` (default 40) caps the **sum of sealed weight sizes**.
- `inline` tournaments must reject a seal, and `huggingface` tournaments must reject an upload without one.

---

## 3. Threat model

| Attack | Control |
|---|---|
| Miner submits another miner's repo URL | Token owner (from `whoami-v2`) must equal the repo namespace. The repo is read with that token, so a thief without the owner's token cannot seal it. The sealed commit must contain `nepher-proof.json` naming this tournament and hotkey, and writing it needs push access. One HF account binds to one hotkey per tournament. |
| Miner copies another miner's weights into their own repo | Per-file SHA-256 collision check across all submissions in the tournament (see 4.3). Renaming or re-packing the same bytes does not help. Adding junk files does not help, because any collision with a non-base file rejects the seal. |
| Same miner submits the same weights twice | Same-hotkey duplicate fingerprint is rejected (`409 duplicate_weights_same_miner`). |
| Validator reads the weights and submits them as its own miner entry | Validators read only after the seal exists. A later seal that contains any of those files collides and is rejected. Every lease is audit-logged with the validator hotkey. |
| Miner makes the repo public or changes visibility | Monitor sees `private: false` and disqualifies. |
| Miner edits weights after submitting | Seals pin a commit. The monitor also checks the default branch: any sealed path whose SHA-256 differs (or is deleted) disqualifies. Validators re-hash every file and re-check custody before submitting a score. |
| Miner deletes the repo, squashes history, or revokes the token | The sealed commit or the token stops working. Definitive failures (401, 403, 404) disqualify. Transient failures (5xx, timeouts) retry and never change state. |
| Winner hides or alters weights between selection and announcement | Archive-before-announce. The backend copies and re-hashes the sealed revision before any announcement or payout. |
| Malicious validator forges a score for a disqualified agent | The backend rejects evaluation submissions for agents that are not `sealed`. Validators also refuse locally. |
| Over-broad token leaks the miner's other repos | Only fine-grained tokens scoped to exactly one repo with read-only permissions are accepted. The CLI checks this **before** sending the token. |
| Zip contains weights, bypassing the seal | `huggingface` tournaments reject any file under `weights/` in the archive. Validators re-check after unzip. |
| Path traversal in `agent.yaml` weight names | Names must be relative, contain no `..`, and stay inside `weights/`. |
| Replay of an old seal | The seal message carries a timestamp. Reject if older than 10 minutes. |
| Presigned URL abuse | URLs are only minted for registered validators, only for the current evaluation window, and only for the sealed commit. URLs must point to Hugging Face hosts (`huggingface.co`, `hf.co`, and the CDN hosts returned by Hugging Face). |

Known residual risk: a validator can see the weights it is evaluating, because it has to run them. This is the same trust level as today's inline archive. The fingerprint collision check and the audit log are the deterrents.

---

## 4. Canonical definitions

These definitions are implemented identically in `nepher_cli/tournament/custody.py` and `nepher_core/custody.py`. Both have the same test vectors.

### 4.1 Proof file

`nepher-proof.json`, UTF-8 JSON, committed to the **root of the Hugging Face repo** and also placed in the agent directory root (it is included in the code archive):

```json
{"version": 1, "tournament_id": "<tournament id>", "miner_hotkey": "<ss58 hotkey>"}
```

The backend compares the parsed JSON from the repo (at the sealed commit) with the parsed JSON in the archive and with the seal. Any mismatch is `proof_mismatch`.

### 4.2 Weights manifest

`agent.yaml` has a `weights:` mapping of relative path to lowercase SHA-256 hex. The Hugging Face repo root corresponds to `weights/` inside a submission, so `weights/a/b.safetensors` is repo path `a/b.safetensors`.

Every weight file must be stored in Hugging Face's large-file storage (LFS or Xet) so the Hub reports its SHA-256.

### 4.3 Fingerprint

Let `base` be the tournament's allowlist of public base-checkpoint SHA-256 values. Let `all` be the sorted, de-duplicated manifest digests, and `novel` the entries of `all` that are not in `base`. `claimed` is `novel` when that list is non-empty, and `all` when the submission is the unmodified public checkpoint.

```
fingerprint = sha256("nepher-weights-v1\n" + "\n".join(claimed))
```

Rules enforced by the backend:

1. The manifest must contain at least one file.
2. The unmodified public checkpoint is a valid submission. Its fingerprint covers every file, so the first submission claims that exact set. A later submission with the same fingerprint is `409 weights_already_submitted` for another hotkey and `409 duplicate_weights_same_miner` for the same hotkey. Renaming the files does not change the fingerprint.
3. When the submission also contains files outside the allowlist, the allowlisted digests are left out of the fingerprint and out of the per-file collision check. A fine-tune may include the public checkpoint even after that checkpoint was submitted on its own. Every digest in `novel` collides with any other submission in the tournament (any hotkey, any status except `superseded` for the same hotkey): a different hotkey is `409 weights_already_submitted`, and the same hotkey with the same fingerprint is `409 duplicate_weights_same_miner`. The same hotkey with a different fingerprint is allowed and **supersedes** the earlier submission.
4. Paths and sizes are not part of the fingerprint.

Test vectors:

```
weights = {model-00001.safetensors: "a"*64, model-00002.safetensors: "b"*64, config.json: "c"*64}
base    = {"c"*64}
fingerprint = d1997740cfac05556674280cf56da868615abc23da3b301e0f806bef6c30af97

# the same files, all of them on the allowlist (the unmodified public checkpoint)
base    = {"a"*64, "b"*64, "c"*64}
fingerprint = 9c7995808ddf861b631f6839a193dd893f2a32f816f3e6b1eec8804822c2d30d
```

### 4.4 Seal message and signature

The miner's hotkey signs this exact string (hex sr25519 signature, same helper as `file_info`):

```
nepher-seal-v1:{miner_hotkey}:{tournament_id}:{fingerprint}:{hf_repo}:{hf_revision}:{timestamp}
```

Test vector:

```
nepher-seal-v1:5Fhot:tid-1:d1997740cfac05556674280cf56da868615abc23da3b301e0f806bef6c30af97:alice/go-weights:ffffffffffffffffffffffffffffffffffffffff:1700000000
```

`timestamp` is Unix seconds. `hf_revision` is 40 lowercase hex characters.

---

## 5. Miner flow

Prerequisites: a Hugging Face account, one private model repo in the **user** namespace (not an organization), and a fine-grained token that grants **read access to that one repo only**.

1. Train. Put the full checkpoint under `./my-agent/weights/`. List every file and its SHA-256 in `agent.yaml`.
2. Push `weights/` to the private repo root (large files through LFS or Xet).
3. `npcli tournament proof --path ./my-agent --tournament-id <id> --wallet-name miner --wallet-hotkey default` writes `nepher-proof.json`. Copy that file to the repo root and push. Note the commit SHA.
4. `npcli tournament check --path ./my-agent --submission-mode huggingface` validates the layout offline.
5. Submit:

```
npcli tournament submit --path ./my-agent --tournament-id <id> \
    --wallet-name miner --wallet-hotkey default \
    --hf-repo alice/go-weights --hf-revision <40-hex commit> \
    --hf-token <fine-grained read-only token>
```

   The token can also come from `NEPHER_HF_READ_TOKEN`. The CLI does not read the generic `HF_TOKEN` variable, so a broad write token is never picked up by accident.

6. Do not push to the repo again. A new submission uses a new repo or a new commit with different weights.

Miner-facing rules to publish: the repo must stay private; weights cannot change after sealing; the same weights cannot be submitted twice; a new submission with different weights replaces the earlier one while the submit window is open.

---

## 6. State machine

`custody_status` on an agent (only for `huggingface` tournaments):

```
sealed --> archiving --> archived            (terminal, set after a verified copy)
sealed --> disqualified                      (terminal)
sealed --> superseded                        (terminal, same hotkey submitted new weights)
archiving --> archive_failed --> disqualified
```

Rules:

- Only `sealed` agents are listed to validators, accepted for leases, and accepted for evaluation results.
- `disqualified` is permanent and records a reason code and a timestamp. Existing scores are kept for audit and excluded from leaderboards, preliminary leaders, and winner selection.
- `archived` is the only state that allows the agent to be announced or paid.

---

## 7. Site: backend API contract

All new fields are additive. Existing clients keep working for `inline` tournaments.

### 7.1 Tournament

Additional fields on `GET /api/v1/tournaments/*` (including the minimized `subnet=true` payload that validators fetch):

| Field | Type | Notes |
|---|---|---|
| `format` | string | `rl`, `vla`, `wam`, `lwm`, `task_generation` |
| `submission_mode` | string | `inline` (default when absent) or `huggingface` |
| `base_weight_sha256` | string[] | Allowlist for 4.3. Admin editable until the first seal. Public. |

### 7.2 Upload verify (extended)

`POST /api/v1/agents/upload/verify` accepts an optional `seal` object. The existing fields and signature (`file_info`) stay as they are.

```json
{
  "miner_hotkey": "...", "public_key": "...", "file_info": "...", "signature": "...",
  "file_size": 123456, "tournament_id": "...",
  "seal": {
    "hf_repo": "alice/go-weights",
    "hf_revision": "<40 hex>",
    "weights": {"model-00001.safetensors": "<sha256>"},
    "weights_fingerprint": "<sha256>",
    "proof": {"version": 1, "tournament_id": "...", "miner_hotkey": "..."},
    "seal_message": "nepher-seal-v1:...",
    "seal_signature": "<hex>",
    "hf_read_token": "hf_..."
  }
}
```

Validation, in this order. Stop at the first failure:

1. Tournament exists, is in the submit window, and `submission_mode` matches (`huggingface` requires `seal`; `inline` forbids it).
2. `seal_message` is exactly the canonical string for the submitted fields. The signature verifies against `miner_hotkey`. The timestamp is within 10 minutes.
3. `file_size` is below the small-archive cap (suggested 200 MB).
4. `hf_revision` is 40-hex. `hf_repo` is `owner/name` in a **user** namespace.
5. Token check with `GET https://huggingface.co/api/whoami-v2`: token owner equals the repo owner. The token is fine-grained, has no global permissions, is scoped to exactly this repo, and holds read-only permissions.
6. Repo check: it is private, `main` HEAD equals `hf_revision` at this moment, and the revision resolves.
7. File check at `hf_revision` through the tree API: every manifest path exists, is large-file backed, and its reported SHA-256 and size match. No manifest path escapes the repo root.
8. Proof check: `nepher-proof.json` at the sealed revision equals the submitted proof, and the proof equals this tournament id and hotkey.
9. Sum of sizes is at most `max_submission_gb`.
10. Fingerprint recomputed by the server equals `weights_fingerprint`, using the tournament's `base_weight_sha256`.
11. Uniqueness rules from 4.3. One HF account maps to one hotkey in this tournament: a different hotkey using the same HF account is `409 hf_account_in_use`.

On success the response is the existing upload token plus `"custody": "verified"`. The token is bound to the fingerprint. Nothing is stored as an agent until the upload in 7.3 completes.

Errors use `{ "code": "...", "message": "..." }`:

| HTTP | `code` |
|---|---|
| 400 | `seal_required`, `seal_not_allowed`, `bad_seal_message`, `bad_revision`, `proof_mismatch`, `weights_mismatch`, `weights_not_lfs`, `weights_too_large`, `fingerprint_mismatch`, `repo_not_private`, `revision_not_head`, `token_scope_invalid`, `repo_not_user_owned` |
| 401 | `bad_signature`, `seal_expired` |
| 403 | `token_owner_mismatch` |
| 409 | `weights_already_submitted`, `duplicate_weights_same_miner`, `hf_account_in_use` |
| 502 | `huggingface_unavailable` (retryable, no state change) |

### 7.3 Upload

`POST /api/v1/agents/upload/{tournament_id}` carries the archive and an extra form field `weights_fingerprint`. The backend:

- verifies the token's bound fingerprint,
- unzips in a sandboxed temporary directory and rejects any file under `weights/`,
- reads `agent.yaml` and requires its `weights` mapping to equal the sealed manifest exactly,
- creates the agent with `custody_status = sealed`, stores the encrypted token, and marks any earlier `sealed` agent of the same hotkey as `superseded`.

The submission fee, if any, is charged here and refunded if the agent is later disqualified for reasons the platform defines.

### 7.4 Custody status

`GET /api/v1/agents/{agent_id}/custody` (validator-authenticated):

```json
{"agent_id": "...", "custody_status": "sealed", "weights_fingerprint": "...",
 "hf_revision": "<40 hex>", "checked_at": "2026-10-13T12:00:00Z", "reason": null}
```

The backend runs a fresh check (6.3 below) when `checked_at` is older than 5 minutes. The repo id is never returned.

### 7.5 Weight lease

`POST /api/v1/agents/{agent_id}/weight-lease`

Request, signed with the validator hotkey. The message is `weight-lease:{validator_hotkey}:{agent_id}:{timestamp}`:

```json
{"validator_hotkey": "...", "public_key": "...", "lease_info": "weight-lease:...", "signature": "...",
 "tournament_id": "..."}
```

Rules:

- The hotkey must be a registered validator in the metagraph (same check as evaluation submission).
- The agent must be `sealed`, and the tournament must be in an evaluation period.
- The backend re-checks the repo (privacy, commit reachability, sealed-path SHA-256 at HEAD). Failure disqualifies and answers `409 not_sealed`.
- For each manifest path, the backend calls `GET https://huggingface.co/{repo}/resolve/{revision}/{path}` with the stored token and `follow_redirects=false`. It reads the `Location` header, which must be an `https` URL on a Hugging Face host.
- Rate limit: 20 lease calls per validator per agent per hour. Each call is audit-logged (validator hotkey, agent id, time, outcome).

Response:

```json
{"agent_id": "...", "revision": "<40 hex>", "expires_at": "2026-10-13T13:00:00Z",
 "files": [{"path": "model-00001.safetensors", "sha256": "<sealed>", "size": 12345678901,
            "url": "https://.../signed", "url_expires_at": "2026-10-13T13:00:00Z"}]}
```

The response contains no Hugging Face token and no repo id.

`POST /api/v1/agents/{agent_id}/weight-lease/release` (same signature scheme) marks the lease finished. It is fire-and-forget for the validator.

### 7.6 Pending agents and evaluation results

- `GET /api/v1/agents/list/unevaluated` returns only `sealed` agents for `huggingface` tournaments.
- `POST /api/v1/evaluations/submit/*` returns `409 not_sealed` for any agent that is not `sealed` at that moment.
- Leaderboards, preliminary leaders, and winner selection exclude `disqualified` and `superseded` agents.

### 7.7 Background monitor

For every `sealed` agent, until it is `archived`, run every 5 minutes (jittered) and on every lease, status read, and evaluation submit:

1. Repo is reachable with the stored token (`whoami-v2` and tree call).
2. Repo is still `private`.
3. The sealed commit still resolves.
4. For each sealed path: SHA-256 at `main` HEAD equals the sealed value (a deleted or changed path is a violation).

Outcome mapping:

| Result | Action |
|---|---|
| 200 and checks pass | Keep `sealed` |
| 401, 403 (token revoked or access removed), 404 (repo or commit missing), `private: false`, SHA-256 differs | `disqualified` with reason `token_revoked`, `repo_unreachable`, `repo_public`, `commit_missing`, or `weights_changed` |
| 5xx, timeout, 429 | Retry with backoff. After 12 hours of continuous transient failure during an evaluation period, alert an admin. State stays `sealed`. |

Disqualification revokes outstanding leases, removes the agent from pending lists, and posts an admin notification.

### 7.8 Archive before announce

Admin flow after the private evaluation ends:

1. Admin selects the winner (and any other paid podium places) in the existing UI.
2. For each paid place, the backend sets `archiving`, then:
   - runs the full check from 7.7,
   - downloads each sealed file from Hugging Face using the stored token, streaming to Nepher object storage,
   - recomputes SHA-256 while streaming and compares to the seal and to the file size,
   - stores a receipt: agent id, repo, revision, per-file SHA-256, size, copy time, storage key.
3. If every file matches, set `archived`. Otherwise set `archive_failed`, then `disqualified` with the specific reason, and notify the admin. **No automatic promotion**: the admin selects a new candidate and repeats.
4. The announcement, the leaderboard winner banner, the `winner-hotkey` reward payload, and the on-chain payout path are all blocked until **every paid place** is `archived`. The API and the UI must both enforce this. The UI's announce button stays disabled and the API returns `409 archive_incomplete`.
5. Archived objects are private, encrypted at rest, and owned by Nepher. The stored token is deleted after archive.

Disk and cost guard: the archive job needs free space of at least the sealed total plus 10 percent. If not, it fails before downloading.

### 7.9 Reward payload

`GET /api/v1/tournaments/{id}/winner-hotkey` for `huggingface` tournaments adds, on each podium entry:

| Field | Value |
|---|---|
| `custody_status` | `archived` (the API returns an empty podium and `winner_approved = false` otherwise) |

Validators use this field as a second gate (section 10.3).

### 7.10 Public surfaces and privacy

Public APIs, the leaderboard, logs, and error messages never expose `hf_repo`, tokens, proof nonces, signed URLs, or lease contents. Admins can see the repo id. Tokens are encrypted at rest with a key held outside the database, redacted from all logs, and deleted when the agent becomes `disqualified`, `superseded`, or `archived`.

---

## 8. Site: frontend requirements

- Tournament page: show submission mode and format. For `huggingface` tournaments show the miner checklist from section 5 and the rules from the end of section 5.
- Submission table (miner view): `custody_status`, the disqualification reason if any, and the sealed fingerprint (short form). Never show the repo id to other users.
- Admin tournament page: custody column, per-agent monitor history, manual disqualify, and archive status with the hash receipt.
- Admin winner page: Select, Archive (progress per file), then Announce. Announce stays disabled until every paid place is `archived`.
- Tournament creation form: `format`, `submission_mode`, base-weight allowlist editor (frozen after the first seal), size limit.
- Miner account page: no Hugging Face login is required. Tokens are supplied per submission through the CLI.

---

## 9. This repo: miner side (`nepher-cli`)

- `npcli tournament proof` writes `nepher-proof.json` for a tournament and hotkey.
- `npcli tournament check --submission-mode huggingface` validates `agent.yaml`, `brain/`, `train/`, local weight hashes, and proof file.
- `npcli tournament submit` looks up the tournament, reads `submission_mode` and `base_weight_sha256`, and for `huggingface`:
  - refuses to continue if weights are not hashed in `agent.yaml` or the proof is wrong,
  - calls `whoami-v2` on Hugging Face **before** sending the token anywhere and refuses a token that is not read-only, fine-grained, and scoped to exactly the repo,
  - zips the agent **without** `weights/`,
  - signs the seal message and sends the seal in the verify request.
- `inline` tournaments follow the existing path with no change.
- If the tournament mode cannot be determined and the directory is a brain submission (`agent.yaml`), the CLI stops and asks for `--tournament-id`.

---

## 10. This repo: validator side (`nepher-subnet`)

### 10.1 Evaluation

For `huggingface` tournaments, `AgentEvaluator._evaluate_brain`:

1. Downloads and extracts the small archive. Rejects any file under `weights/`.
2. Reads custody status. Anything other than `sealed` aborts with a non-retryable error.
3. Layout-checks the archive (external weights allowed) and requires `agent.yaml` weights to equal the lease manifest exactly.
4. Rejects a manifest whose total size exceeds `max_submission_gb` before downloading.
5. Requests a lease and downloads each file straight from its signed URL with no `Authorization` header, streaming to disk with SHA-256 computed while writing. A short download resumes with `Range`. A 403 or 410 re-requests a fresh URL (bounded retries).
6. Fails on any size or SHA-256 mismatch, and deletes the partial weights.
7. Releases the lease, then runs the existing brain check, serve, smoke, and sandbox steps unchanged. The sandbox sees no URL and no token.
8. Re-reads custody before submitting a score. If it is no longer `sealed`, the validator discards the result.

The download runs on the validator host before the sandbox starts. `huggingface.co` and the CDN hosts are needed from the validator host only. The sandbox whitelist does not change.

### 10.2 Scheduling

`EvaluationOrchestrator` passes the tournament's `submission_mode` to the evaluator. A tournament with `inline` and an agent that carries a custody status is refused, and so is the reverse.

### 10.3 Reward gate

`WeightSetter._get_reward_recipients` drops the whole tournament's payout (the remainder burns on UID 0) unless every paid podium entry has `custody_status == "archived"`. The gate applies when the tournament is `huggingface` or when any podium entry carries a custody status. `inline` tournaments use the current logic unchanged.

---

## 11. Rollout and compatibility

1. Ship backend fields and endpoints behind `submission_mode`. Absent means `inline`. The Go2 tournament is never switched.
2. Ship `nepher-cli` and `nepher-subnet` changes. Old CLIs still work for `inline` tournaments. An old CLI sending a zip to a `huggingface` tournament gets `400 seal_required`.
3. Validators upgrade before the first `huggingface` tournament opens. Validators on old code never see `huggingface` agents if the backend lists them only to validators that send a capability header (`X-Validator-Custody: 1`). The recommended implementation is to add that header to the validator client and filter on it.
4. Create the first `huggingface` tournament in a staging environment against private test repos before any live phase.

---

## 12. Acceptance tests for the site

Run each against staging with real private repos.

1. Valid seal: accepted, agent is `sealed`, listed to validators.
2. Seal with a public repo: `repo_not_private`.
3. Seal with a token for a different account than the repo owner: `token_owner_mismatch`.
4. Seal with a classic read or write token: `token_scope_invalid`.
5. Seal with a token scoped to two repos, or a repo plus global permissions: `token_scope_invalid`.
6. Seal with a stale commit that is not `main` HEAD: `revision_not_head`.
7. Seal whose manifest hash differs from the Hub's SHA-256: `weights_mismatch`.
8. Proof missing, or naming another hotkey or tournament: `proof_mismatch`.
9. Second hotkey seals the same files from another repo: `weights_already_submitted`. Also with an extra junk file added.
10. Same hotkey seals the same weights again: `duplicate_weights_same_miner`.
11. Same hotkey seals different weights: accepted, earlier agent becomes `superseded`.
12. Unmodified public base checkpoint: accepted. A second hotkey sealing the same files, under any names: `weights_already_submitted`. A submission that keeps those files and adds one file outside the allowlist: accepted.
13. Two hotkeys with the same HF account: `hf_account_in_use`.
14. Archive contains `weights/x`: rejected.
15. After sealing: push a changed weight file to `main`: agent becomes `disqualified` within 5 minutes with `weights_changed`. A README-only push keeps `sealed`.
16. After sealing: flip the repo to public, delete the repo, revoke the token: each yields `disqualified` with the matching reason.
17. HF returns 503 for an hour: agent stays `sealed`.
18. Validator hotkey not in the metagraph requests a lease: rejected.
19. Lease response and every API response contain no token, repo id, or signed URL outside the lease.
20. Evaluation result for a `disqualified` agent: `409 not_sealed`.
21. Archive with an unchanged repo: `archived`, receipt stored, hashes equal.
22. Archive after the miner swaps a file: `archive_failed`, then `disqualified`, nothing announced.
23. Announce while any paid place is not `archived`: UI disabled, API `409 archive_incomplete`.
24. `winner-hotkey` for an unarchived winner: empty podium.

---

## 13. Items to confirm in staging

- Exact `whoami-v2` fields for fine-grained scope and permission names. The CLI and backend must use an allow-list: `repo.content.read` and `repo.access.read` only.
- Whether Xet-backed files report a SHA-256 `lfs.oid` in the tree API. If any file lacks one, the seal for it must fail closed (`weights_not_lfs`).
- Signed-URL lifetime and `Range` support. If the lifetime is shorter than the longest file download, the validator's refresh path (new lease call per file) covers it. Raise the per-hour lease limit if needed.
- Whether Hugging Face's `xet-read-token/{rev}` endpoint should replace signed redirect URLs. It would add the `hf_xet` client to validators, so it is out of scope for the first release.
