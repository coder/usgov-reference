# java-engineer

Java and JVM engineering workspace for the GovCloud demo. Runs in a Kubernetes
pod on EKS as plain compute for the Coder Agents path: the AI runs server-side,
so this template has no AI Gateway wiring, no LLM keys, and no agent harness.

See `../_shared/README.md` for the shared EKS pattern and invariants.

## Tooling

Provisioned best-effort at startup via `sudo apt-get` on the `enterprise-base`
image (tolerant: a failed install never fails the build):

- `default-jdk` (OpenJDK 21 on Ubuntu noble)
- `maven`
- `gradle`
- `ripgrep`, `jq`
- `code-server` (VS Code in the browser, subdomain app)

For a fully air-gapped demo, bake these into a custom workspace image and set
`workspace_image`; the startup apt block then becomes a no-op. See the WS-25
handoff for the root mirror/build TODO.

## Parameters

| Parameter | Type | Mutable | Default | Options |
|---|---|---|---|---|
| `cpu` | number | yes | `4` | 2, 4, 8 |
| `memory` (GB) | number | yes | `8` | 4, 8, 16 |
| `disk_size` (GB) | number | no | `20` | 10, 20, 50 |
| `git_repo` | string | no | `""` | optional repo to clone on start |

## Variables

| Variable | Default | Purpose |
|---|---|---|
| `namespace` | `coder-workspaces` | namespace for workspace pods |
| `workspace_image` | ECR-mirrored `enterprise-base` | workspace container image |
| `use_kubeconfig` | `false` | use a host kubeconfig instead of in-cluster config |

## Git auth

`data "coder_external_auth" "gitlab"` (`id = "gitlab"`) makes GitLab login
required. After the owner completes the in-boundary OAuth flow, the agent git
credential helper injects a short-lived token for clone/fetch/push to
`gitlab.<BASE_DOMAIN>`. No PATs or SSH keys live in the workspace.

## Routing description

> Java/JVM engineering workspace: OpenJDK 21, Maven, Gradle. Use for Java and
> Kotlin services, Spring Boot apps, and JVM build tooling.

Applied to the template by the root operator with `coder templates edit
java-engineer --description "..."` so Coder Agents auto-selects it for JVM work.
Push/build/test/cleanup commands are in
`docs/swarm/workstreams/WS-25-templates.md`.
