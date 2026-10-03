# Hotfix branches, main merge and GitHub authentication

`iris-auto-repair` is a separate trusted coordinator. The candidate API generates proposals; the coordinator holds WAS, GitHub and S3 credentials. Starting it authorizes `hotfix/iris/{requestId}` publication, a ready PR into **main**, and a merge commit through GitHub's PR API. Use `--draft-pr` to stop at a draft PR for manual review instead. Automatic merge requires the service's configured source branch to be `main`; a different branch stops with `TARGET_BRANCH_INVALID` before candidate generation.

The coordinator uses WAS deployment/diagnosis APIs and `GET /api/v1/services/{serviceId}/deployments/{deploymentId}/repair-context`. It selects the exact diagnosis ID returned by diagnosis creation and requests its original `diagnosis-result.v3`, service repository/branch, and short-lived source snapshot. Install this WAS endpoint before running the coordinator.

One incident generates one candidate with a stable request ID. The coordinator verifies artifact hashes and the candidate manifest, then stores `patch.diff`, `changes.json`, `manifest.json` and patched `source.tar.gz` in private S3. Content-addressed keys and SHA-256 checksums bind the bytes. Storage must finish before publication.

GitHub preimages are checked at the frozen failed commit. The candidate commit uses that commit as its parent and preserves other files through the original Git tree. Publication creates a deterministic hotfix ref, reuses the same ref/commit on retry and rejects a different commit. PR lookup includes closed PRs to recover a lost response after merge; a closed, unmerged PR stops the incident. Existing `iris/repair/` refs remain supported when resuming old journals.

Before merging, the coordinator verifies the PR's repository, head branch/commit and base branch, then requires main to still match the failed source commit. The merge request pins the candidate head SHA and uses `merge_method=merge`. It does not PATCH main's ref or force-push. A changed head/base stops with `SOURCE_HEAD_CHANGED`. GitHub checks, protection rules, required approvals and conflicts can block the merge (`PAUSED_ERROR / MERGE_BLOCKED`); restarting or watch mode retries the same PR within the incident deadline. Configure required checks on main to enforce your CI policy. This coordinator does not execute candidate code; validation stays `not_run`. GitHub's merge API pins the PR head, but offers no expected-old-base SHA parameter, so the pre-merge base check is not atomic with merging.

After merging, `MERGED` and `mergeCommitSha` are persisted. This records Git completion, not a successful deployment or resolved defect. Deployment follows the service's ordinary webhook process; the coordinator submits no separate redeployment. Draft mode ends in `PR_OPENED`.

## Source pinning

`repair-context.source` should include `archiveSha256` and `manifestSha256` frozen by WAS. With both present the coordinator verifies the downloaded snapshot against them (`sourcePinned: true`). Without them it still requires the source branch to equal the failed commit and each changed file preimage to match GitHub, and records `sourcePinned: false`. WAS should supply the hashes. The manifest uses sorted paths, content SHA-256, size, and mode `100755` when any executable bit is set, otherwise `100644`, through `source.manifest_digest`.

WAS returns `409 DIAGNOSIS_NOT_SUCCEEDED` unless diagnosis succeeded, `404` for a diagnosis from another deployment, and `409 SOURCE_SNAPSHOT_UNAVAILABLE` after the snapshot's 23-hour window. Builds made before snapshot hash storage was introduced return null hashes.

## GitHub credentials

All authentication modes use GitHub's HTTPS REST API for Git operations; no interactive `git login` or shell credential storage is needed. Credentials go only to `api.github.com`, remain in memory and are not journaled or sent to the candidate API. Fine-grained PAT strings are covered by secret redaction.

The default is **WAS authentication** (`GITHUB_AUTH_MODE=was`). WAS already receives GitHub user authorization during login, stores the user-to-installation links, and uses its App private key to create installation tokens. Its user OAuth token is used only during login and is not retained. The coordinator reuses `WAS_BASE_URL` and the service owner's `WAS_TOKEN`, so no GitHub token, App ID or private key is required in the coordinator.

```dotenv
GITHUB_AUTH_MODE=was
WAS_BASE_URL=https://your-was-service
WAS_TOKEN=your-existing-was-session-token
```

The coordinator calls `POST /api/v1/services/{serviceId}/repair-github-token` with `{"repository":"owner/repo"}`. WAS authenticates its existing session, verifies service ownership, checks that the requested repository matches the service's source repository, resolves the user's saved installation and rechecks repository access. It then requests a token limited to that repository with Contents and Pull requests write. The response includes `repository`, `token` and `expiresAt` under the normal `data` envelope and carries `Cache-Control: no-store`. Only the trusted coordinator receives it; the generation API and journals never receive it.

Tokens are cached in coordinator memory and renewed 60 seconds before expiry. GitHub 401 clears the cached token for the next explicit retry. An expired WAS session requires the ordinary WAS login flow; an installation without write permissions requires granting the App **Contents: read/write** and **Pull requests: read/write** and accepting the updated installation permissions. Existing WAS login/App-install routes provide that flow. Install the updated WAS token endpoint before using this mode.

Standalone authentication remains available with an explicit `GITHUB_AUTH_MODE=app` or `token`:

For a **GitHub App**, create/install the App for the target repositories with **Contents: read/write** and **Pull requests: read/write**. Download its RSA private key to a file outside the repository, then configure:

```dotenv
GITHUB_AUTH_MODE=app
GITHUB_APP_ID=your-app-id-or-client-id
GITHUB_APP_PRIVATE_KEY_FILE=/secure/path/github-app.pem
# Optional: omit to discover the installation for each repository.
GITHUB_APP_INSTALLATION_ID=
```

The coordinator signs an RS256 App JWT, discovers the repository installation when needed, and exchanges it for a token limited to that repository and the two write permissions. Tokens are cached per repository and refreshed 60 seconds before expiry. A 401 evicts the cached token for the next coordinator retry; uncertain writes are never automatically replayed by authentication. Incomplete App configuration fails without falling back to a PAT. See [GitHub App JWT authentication](https://docs.github.com/en/apps/creating-github-apps/authenticating-with-a-github-app/generating-a-json-web-token-jwt-for-a-github-app) and [installation token generation](https://docs.github.com/en/apps/creating-github-apps/authenticating-with-a-github-app/generating-an-installation-access-token-for-a-github-app).

For a **fine-grained PAT**, select only the service repository and grant Contents and Pull requests read/write:

```dotenv
GITHUB_AUTH_MODE=token
GITHUB_TOKEN=your-fine-grained-pat
```

The classic `ghp_` token format still works but emits a scope warning. Set `GITHUB_AUTH_MODE` explicitly to use standalone modes; the coordinator and auth-check command default to `was`.

Check authentication and repository/PR read access without writing Git objects:

```sh
uv run iris-github-auth --service-id 123 --repository owner/repository
```

The command prints repository, authentication mode, default branch and status, never credentials. Omit `--service-id` only in explicit standalone App/PAT mode. It rejects an explicit `permissions.push=false`; App exchanges additionally verify granted write permissions. A successful read check alone cannot prove PAT PR-write permission or whether protection rules allow merging. GitHub enforces those on actual publication/merge requests. See the [GitHub pull-request merge contract](https://docs.github.com/en/rest/pulls/pulls#merge-a-pull-request).

## Launch and resume

Configure WAS/fix URLs and credentials, the artifact bucket and normal AWS SDK credentials:

```dotenv
FIX_S3_BUCKET=your-private-repair-bucket
FIX_BASE_URL=https://your-fix-service
WAS_BASE_URL=https://your-was-service
WAS_TOKEN=
FIX_ALLOWED_SOURCE_HOSTS=your-source-bucket.s3.ap-northeast-2.amazonaws.com
```

Observe failed deployments, publish hotfix PRs and merge into main:

```sh
uv run iris-auto-repair --watch --service-id 123 \
  --allowed-path 'src/**' --allowed-path 'app/**'
```

Run or resume one incident:

```sh
uv run iris-auto-repair --run-id repair-service123-deploy456 \
  --service-id 123 --deployment-id 456 --allowed-path 'src/**'
```

Add `--draft-pr` for review-only publication. Defaults are one generation, 30 minutes per incident and USD 1 maximum generation cost; WAS diagnosis costs are separate. Settings, including draft/merge mode, cannot change on resume. Journals created before this change must be resumed with `--draft-pr`; they never gain automatic merge authorization by upgrade.

Keep `FIX_DATA_DIR` on persistent local storage. Journals contain source/artifacts with mode 0600 and use a process lock. Use one watcher per service; distributed coordination is unsupported. Candidate bytes survive S3 failure, and commit/ref/PR/merge outcomes can be reconciled after response loss without another generation. Watch mode skips handled incidents in `PR_OPENED`, `MERGED` or `STOPPED` and resumes active ones.

## Verification

Offline tests cover WAS session-to-installation authentication, owner/repository checks, token caching/renewal, PAT and App authentication, JWT signatures, scoped token exchange/cache/expiry, secret handling, preimage checks, hotfix-only ref writes, PR creation/reuse, head/base checks, blocked merges, persisted success and response-loss recovery. WAS/S3/GitHub are fixtures: these tests make no live uploads, pushes, PRs, merges or deployments.
