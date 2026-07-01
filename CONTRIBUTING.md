# Contributing to usgov-reference

This document describes how changes flow from the private live-env repo into
this public reference repository, the rules that govern what may appear here,
and how to validate your changes before opening a pull request.

---

## Promotion model

Changes in this repo originate in a private live-env repository where the
real deployment runs. The promotion path is:

```
private live-env repo
        |
        |  operator reviews change,
        |  strips all real identifiers,
        |  replaces with <PLACEHOLDER> tokens
        |
        v
  feature branch in usgov-reference
        |
        |  CI gates pass (gitleaks + check-identifiers + yamllint)
        |  peer review approves
        |
        v
    main branch (public)
```

**Never** copy-paste content from the live-env repo without first auditing
every line for real identifiers. The CI gates catch most leakage, but they
are a safety net, not a substitute for careful review.

---

## Minimal-base-delta rule

When promoting a file, change **only** what is required to sanitize it.
Preserve structure, comments, and naming conventions from the source.
This minimizes diff noise and makes it straightforward to apply future
upstream changes.

Concretely:
- Replace `<real-value>` with `<PLACEHOLDER>` in-place.
- Do not reorder sections, rename keys, or refactor logic during promotion.
- Keep comments that explain non-obvious behavior; remove comments that
  reference the live environment by name.

Refactoring and restructuring belong in separate PRs that do not touch
real identifiers.

---

## Placeholder style

Use `<UPPER_SNAKE_CASE>` angle-bracket tokens for every value that must
be supplied by the fork operator. Examples:

| Real value category | Placeholder token |
|---|---|
| AWS account ID | `<ACCOUNT_ID>` |
| Base domain | `<BASE_DOMAIN>` |
| Route53 zone ID | `<ROUTE53_ZONE_ID>` |
| ACM certificate UUID | `<ACM_CERT_UUID>` |
| Cluster/resource name prefix | `<CLUSTER_NAME>` |
| RDS endpoint host | `<RDS_ENDPOINT_HOST>` |
| VPC ID | `<VPC_ID>` |
| Subnet ID | `<SUBNET_ID>` |
| Security group ID | `<SG_ID>` |
| OIDC provider ID | `<OIDC_PROVIDER_ID>` |
| Any IAM role ARN | `arn:aws-us-gov:iam::<ACCOUNT_ID>:role/<ROLE_NAME>` |

Keep the surrounding ARN structure intact so readers understand the format.
If a region appears in a context that is NOT environment-specific (e.g., a
generic example or a known AWS service constraint), it may appear as a
literal string.

---

## Guard mechanism

The public CI runs `scripts/check-identifiers.sh`, which uses generic regex
patterns to detect real identifier shapes. It carries no literal live-env
values. Pattern classes it detects:

- 12-digit AWS account IDs in ARN or ECR hostname contexts.
- AWS VPC and networking resource IDs (`vpc-`, `subnet-`, `sg-`, etc.).
- ACM certificate UUIDs (UUID in an ARN `certificate/` segment).
- Optional literal base domain (set `FORBIDDEN_BASE_DOMAIN` env var).

The literal denylist of specific live-env values lives in the private
upstream repository and runs in the promotion pipeline before any file is
published here. User or entity UUIDs in application code
are out of scope for the public scanner; they are covered by the private
denylist.

If a new identifier class needs to be flagged in the public scanner, modify
`scripts/check-identifiers.sh` and add documentation in
`scripts/forbidden-strings.txt`.

---

## Testing ladder

Run these checks locally in order before opening a pull request:

### 1. Identifier scan (required, fast)

```bash
bash scripts/check-identifiers.sh
```

Exit 0 = clean. Fix all `FAIL:` lines before proceeding.

### 2. Gitleaks (recommended locally)

```bash
# Install: https://github.com/gitleaks/gitleaks#installing
gitleaks detect --source . --no-git
```

### 3. YAML and shell lint (recommended)

```bash
# Lint all YAML files
yamllint .

# Lint shell scripts
shellcheck scripts/*.sh
```

### 4. Dry-run render (for Helm values changes)

```bash
helm template <RELEASE_NAME> <CHART> -f values/env.example.yaml --dry-run
```

Replace `<RELEASE_NAME>` and `<CHART>` with the Helm release name and
chart reference for the component you changed (these are positional
arguments, not file placeholders). The goal is to confirm the rendered
output has no malformed YAML caused by unfilled `<PLACEHOLDER>` tokens.

---

## CI gate description

The `.github/workflows/ci.yml` workflow runs three jobs on every pull request
and push to `main`:

### Job: `gitleaks`

Runs [gitleaks](https://github.com/gitleaks/gitleaks) over the full
repository to detect generic secret patterns (API keys, bearer tokens,
private keys, etc.).

A failure here means a secret-shaped string was introduced. Inspect the
output, remove the string, and if it is a known-safe example value, add a
`gitleaks:allow` inline annotation and document why.

### Job: `check-identifiers`

Calls `scripts/check-identifiers.sh` with the repository root as the
argument. The script applies extended regex patterns to detect real AWS
identifier shapes across the entire tree.

A failure here means a real environment-specific identifier was introduced.
Replace it with the corresponding `<PLACEHOLDER>` token.

### Job: `yamllint`

Runs [yamllint](https://yamllint.readthedocs.io/) over all YAML files in
the repository to catch syntax and style issues before merge.

A failure here means a YAML file has a syntax error or style violation.
Fix the reported issues and re-run locally with `yamllint .`.

---

## Pull request checklist

- [ ] All real identifiers replaced with `<PLACEHOLDER>` tokens.
- [ ] `bash scripts/check-identifiers.sh` exits 0.
- [ ] `gitleaks detect` exits 0 (or all findings are documented false positives).
- [ ] `yamllint .` exits 0.
- [ ] PR description explains what was promoted and from which source component.
- [ ] `values/env.example.yaml` updated if any new configurable value was introduced.
