# Supply chain

How Token Bill is built, released and checked, and how you can verify a release yourself.
This implements SPEC §8.10.

The short version: Token Bill has **zero runtime dependencies** (pure standard library), releases
are built in GitHub Actions from the tagged commit with no secrets, every release is
**reproducible byte for byte**, carries **SLSA build provenance** and **PEP 740 attestations**,
and ships a **CycloneDX SBOM** that lists zero runtime components.

## Verifying a release

Replace `0.2.0` with the version you installed. All commands run on any machine; none of them
need an account or a token (except `gh`, which needs a GitHub login to read attestations).

### 1. Build provenance (GitHub attestations)

The wheel, the sdist and both SBOMs each have a Sigstore-signed SLSA provenance attestation
produced by `release.yml`. `SHA256SUMS` lists the hashes of all four files:

```bash
gh release download v0.2.0 --repo sedai77/tokenbill-llm-agent-cost-profiler -D tokenbill-0.2.0
cd tokenbill-0.2.0
sha256sum -c SHA256SUMS
gh attestation verify tokenbill-0.2.0-py3-none-any.whl \
  --repo sedai77/tokenbill-llm-agent-cost-profiler \
  --signer-workflow sedai77/tokenbill-llm-agent-cost-profiler/.github/workflows/release.yml
```

A passing check proves the file was built by this repository's release workflow from the tagged
commit, not uploaded by hand. You can check a wheel you got from PyPI the same way, since it is
the same bytes.

### 2. PEP 740 attestations (PyPI)

PyPI stores a publish attestation for each file, signed with the release workflow's identity
(PyPI shows it under "Verified details"). To check it from the command line:

```bash
uvx pypi-attestations verify pypi \
  --repository https://github.com/sedai77/tokenbill-llm-agent-cost-profiler \
  pypi:tokenbill-0.2.0-py3-none-any.whl
```

### 3. SBOM

`tokenbill-0.2.0.cdx.json` (CycloneDX 1.5) describes the wheel: `pkg:pypi/tokenbill@0.2.0`,
license MIT, the wheel's SHA-256 and SHA-512, and **no components**. Its dependency graph says
`dependsOn: []` and a `complete` composition states that the list is exhaustive.
`tokenbill-0.2.0.dev.cdx.json` separately lists the development toolchain from `uv.lock`
(pytest, ruff, hypothesis, coverage and what they pull in). Every entry there has
`scope: "excluded"`, meaning none of it is shipped or installed with Token Bill.

The SBOM is deterministic, so you can regenerate it and compare:

```bash
git clone https://github.com/sedai77/tokenbill-llm-agent-cost-profiler && cd tokenbill-llm-agent-cost-profiler
git checkout v0.2.0
SOURCE_DATE_EPOCH="$(git log -1 --format=%ct)" \
  python3 scripts/sbom.py --wheel ../tokenbill-0.2.0/tokenbill-0.2.0-py3-none-any.whl \
  | diff - ../tokenbill-0.2.0/tokenbill-0.2.0.cdx.json && echo "SBOM matches"
```

`scripts/sbom.py` does not just describe the wheel. Before writing anything it fails if
`pyproject.toml`, the wheel's `METADATA` or `uv.lock` declares any runtime dependency.

### 4. Reproducible build

The sdist and the wheel are built with `SOURCE_DATE_EPOCH` set to the tagged commit's committer
time, so anyone can rebuild them and get the same bytes:

```bash
git checkout v0.2.0
scripts/repro_check.sh ../tokenbill-0.2.0      # needs uv, git, python3
```

The script builds twice in fresh build environments and compares every SHA-256, first between
the two rebuilds and then against the directory you pass it. It pins hatchling to the version
recorded in the reference wheel (`Generator:` in its `WHEEL` file). If anything differs, it
prints which archive members differ and how (CRC, timestamp or mode). The release workflow runs
the same script against its own `dist/` **before** publishing, and CI runs it on every push.

### 5. Zero runtime dependencies

```bash
python3 scripts/check_zero_deps.py                              # the source tree
python3 scripts/check_zero_deps.py --wheel tokenbill-0.2.0-py3-none-any.whl
```

This runs four checks:

1. `[project].dependencies` is `[]` and not dynamic.
2. Every `import` in `tokenbill/` names the standard library or `tokenbill` itself. This covers
   imports inside functions and `importlib.import_module("…")` calls, and it includes shipped
   package-data scripts.
3. Importing every module pulls in nothing outside the standard library (`sys.modules` is
   compared before and after, against `sys.stdlib_module_names`).
4. The wheel's `METADATA` has no unconditional `Requires-Dist`.

CI runs it in an empty virtual environment on Python 3.10 and 3.13, once against the source tree
and once against the installed wheel (installed with `--no-deps`).

## How a release is built

`release.yml` runs on a pushed `v*` tag. It has four jobs, and each job gets only the permissions
it needs:

| job | permissions | what it does |
|---|---|---|
| `build` | `contents: read` | `uv build` with `SOURCE_DATE_EPOCH` = tagged commit time; `twine check --strict`; tag-vs-`__version__` guard; zero-dependency gate on the wheel; `scripts/repro_check.sh dist` (rebuild and diff); SBOMs; `SHA256SUMS` |
| `attest` | `id-token: write`, `attestations: write` | `actions/attest-build-provenance` over `dist/*` and the SBOMs |
| `pypi` | `id-token: write` (environment `pypi`) | `pypa/gh-action-pypi-publish` with Trusted Publishing (OIDC, no API token or secret) and `attestations: true` (PEP 740) |
| `github-release` | `contents: write` | `gh release create` with `dist/*`, the SBOMs and `SHA256SUMS` attached in one step, notes taken from `CHANGELOG.md` |

Artifacts pass between jobs through `actions/upload-artifact` / `download-artifact` (the
download checks the digest), so the attested and published bytes are the bytes that were built
and repro-checked. Nothing is published unless every earlier step passed.

To cut a release: bump `__version__` in `tokenbill/__init__.py`, move the CHANGELOG
"Unreleased" entries into a dated section, merge, and push the tag `v<version>`.

## Workflows

| workflow | trigger | purpose |
|---|---|---|
| `ci.yml` | push to `main`, pull requests | `lint` (ruff); `unit`: the full suite on Ubuntu × Python 3.10, 3.11, 3.12, 3.13 plus macOS and Windows × 3.12 (fail-fast off); `zero-deps`; `coverage`: the suite under `coverage`, a per-package table in the job summary and the report as an artifact (non-blocking; target ≥ 90% of each package's modules); `build`: sdist + wheel, `twine check`, SBOM dry run, rebuild and diff; `demo`: the offline demo, with no keys and no network |
| `ownership.yml` | pull requests | package branches change only their own files (`OWNERSHIP.toml`); owned by F-CORE |
| `release.yml` | `v*` tag | see above |
| `codeql.yml` | PRs, `main`, weekly | CodeQL `security-extended` queries for Python |
| `scorecard.yml` | `main`, weekly, branch-protection changes | OpenSSF Scorecard; results go to code scanning and the public Scorecard API (`publish_results`). Scorecard only accepts published results from a restricted workflow shape, so keep the job's steps as they are |
| `zizmor.yml` | PRs and pushes touching `.github/**` | static analysis of the workflows (template injection, permissions, credential persistence, unpinned or impostor actions, cache poisoning, Dependabot settings); findings fail the check |
| `pricing-verify.yml` | weekly, manual | checks the rate registry, including the Copilot extension verifier, three ways: **offline** against the packaged pricing snapshots, **live** against the published Claude pricing page and GitHub's Copilot pricing table, and for **promotions** ending within 14 days. It opens an issue for each problem (or comments on the open one with the same title) |
| `perf.yml` | nightly, manual | `scripts/perf_gates.py`: the `perf`-marked full-size budget tests with a per-gate table |
| `dependabot.yml` | weekly | updates to the pinned actions and to the uv-locked dev toolchain |

`pricing-verify.yml` calls the RATES module API (`tokenbill.rates.verify.verify_report`, which
runs `core.extensions.rate_verifiers()`, and `promotions_ending`) through `python -c`. This is the
same code path as `tokenbill pricing verify [--live]`, and an authoritative discrepancy exits 3.
Once the `pricing` CLI command is wired, those steps can become
`uv run tokenbill pricing verify` and `uv run tokenbill pricing verify --live`.

## Hardening rules for workflows

Every workflow follows these rules. `zizmor.yml` enforces most of them.

- **Pinned actions.** Every `uses:` names a full 40-character commit SHA followed by the
  release it came from: `uses: actions/checkout@3d3c42e… # v7.0.1`. Tags and branches are never
  used as refs.
- **No default token permissions.** Each workflow sets `permissions: {}` at the top level. Each
  job requests only what it uses, with a comment saying why. Write permissions exist only in
  `release.yml` (`id-token`, `attestations`, `contents` for the release), `codeql.yml` and
  `scorecard.yml` (`security-events`; Scorecard also has `id-token`), and in `pricing-verify.yml`,
  whose separate `issues` job gets `issues: write` and nothing else.
- **No persisted credentials.** Every `actions/checkout` sets `persist-credentials: false`.
- **No expression injection.** `${{ … }}` never appears inside a `run:` script. Values are
  passed through `env:` instead. No workflow uses `pull_request_target` or `workflow_run`.
- **No secrets.** PyPI uses Trusted Publishing, attestations use the job's OIDC identity, and
  the issue job uses the built-in `GITHUB_TOKEN`.
- **No caches in publishing workflows.** `release.yml` disables the uv cache, so a poisoned
  cache cannot reach a release.
- **Concurrency groups** on every workflow (F-CORE's `ownership.yml` has none yet).
  Superseded pull-request runs are cancelled. Release, scheduled and publishing runs are never
  cancelled.
- **Pinned tools.** Tools run through `uvx` are pinned (`twine==7.0.0`; zizmor `1.30.1` in
  `zizmor.yml`). The dev toolchain is locked in `uv.lock`, and a verifier rebuild pins
  hatchling to the version the release used.

## Pinning and update policy

- **Resolving a pin.** Look up the tag's commit with
  `git ls-remote --tags https://github.com/<owner>/<repo>`. For an annotated tag, use the peeled
  `^{}` line. Never copy a SHA from a README or a blog post. On every PR that touches
  `.github/**`, zizmor's online audits (`impostor-commit`, `ref-version-mismatch`) re-check that
  the SHA belongs to that repository and matches the `# vX.Y.Z` comment.
- **Updates.** Dependabot opens one grouped PR per ecosystem each week and rewrites the SHA and
  the comment together. A 7-day cooldown keeps a freshly published, possibly compromised release
  out until it has been public for a week. Review the action's release notes and diff before
  merging, as you would for any code change. zizmor and CI run on the PR.
- **Adding an action.** Before adding one, check whether `gh`, `uv` or a few lines of shell can
  do the job instead. If you still need it, pin it as above, give its job only the permissions
  it needs, and run `uvx zizmor==1.30.1 .github/` locally before opening the PR.
- **Python dependencies.** Runtime: none, enforced by `scripts/check_zero_deps.py` in CI and
  release. Adding one is a design change (SPEC §2) and needs the contract owner's sign-off.
  Development: `[project.optional-dependencies].dev`, locked in `uv.lock`, updated weekly by
  Dependabot.

## Repository settings (maintainer checklist)

Some protections are repository settings, not code:

- **Immutable releases** (Settings → General → Releases). The release job uploads every asset
  in one `gh release create`, so it works with this setting on.
- **Environment `pypi`**: restrict deployments to `v*` tags and, optionally, require a reviewer.
- **Rulesets / branch protection on `main`**: require the CI, ownership, CodeQL and zizmor checks
  and pull-request review, and block force pushes. This also raises the Scorecard
  Branch-Protection score.
- **Tag protection** for `v*` (ruleset): only maintainers may create release tags.
- **Code scanning**: use the advanced setup in `codeql.yml`, not the default setup.
- **Dependabot alerts and security updates** on, and **private vulnerability reporting** on (see
  `SECURITY.md`).

## Running the checks locally

```bash
python3 scripts/check_zero_deps.py                 # zero runtime dependencies
uv build && python3 scripts/sbom.py --wheel dist/tokenbill-*.whl
scripts/repro_check.sh                             # rebuild twice and diff
scripts/repro_check.sh dist                        # ... and against dist/
uv run --extra dev python scripts/perf_gates.py --list   # list the perf gates
uv run --extra dev python scripts/perf_gates.py          # run them (minutes; nightly in CI)
uvx zizmor==1.30.1 .github/                        # workflow static analysis
```

A signed container image is deferred (SPEC §20).
