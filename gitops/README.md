# gitops/

Argo CD app-of-apps scaffold for a Coder-on-EKS GovCloud deployment.
`<CLUSTER_NAME>` is the cluster and AWS resource name prefix throughout.

## What this tree does

Declares all Argo CD `Application` and `AppProject` objects for the full
platform stack. Argo CD reads this tree from your in-boundary GitLab mirror
(`<GITLAB_GROUP>/<CLUSTER_NAME>`) and reconciles each Application against the
cluster. No cluster state changes until you explicitly sync an Application.

## Tree layout

    gitops/
      bootstrap/
        argocd/          Argo CD Helm install values and install guide
        root-app.yaml    App-of-apps root Application (apply once by hand)
        projects/
          platform.yaml  AppProject: platform infra and secrets
          apps.yaml      AppProject: coder, keycloak, gitlab
          argocd.yaml    AppProject: argocd self-management
      apps/
        platform/        ingress-nginx, aws-lb-ctrl, external-secrets,
                         gp3 StorageClass, workspace RBAC, Namespaces
        coder/           Coder Helm release and provisioner Deployments
        keycloak/        Keycloak Kustomize source
        gitlab/          GitLab StatefulSet and gitlab-runner
        observability/   kube-prometheus-stack, Loki, Promtail, extras
        mesh/            Istio service mesh (placeholder; see note below)

## Bootstrap sequence

1. Mirror all required images into your private ECR registry.
   Run `scripts/mirror-images.sh` with the image list in `scripts/images.txt`.
2. Create the GitLab project `<GITLAB_GROUP>/<CLUSTER_NAME>` in your
   in-cluster GitLab instance and mint a `read_repository` deploy token.
3. Store the deploy token in AWS Secrets Manager at
   `<CLUSTER_NAME>/argocd/gitlab-repo`. An ExternalSecret in the `argocd`
   namespace will surface it as a repo credential Secret for Argo CD.
4. Install Argo CD via Helm using `bootstrap/argocd/values.yaml`.
   See `bootstrap/argocd/README.md` for the exact command and required
   `application.resourceTrackingMethod: annotation` setting.
5. Apply the AppProjects: `kubectl apply -f gitops/bootstrap/projects/`.
6. Apply the root app: `kubectl apply -n argocd -f gitops/bootstrap/root-app.yaml`.
7. Before syncing any Application, run `argocd app diff <name>` and confirm
   the diff shows only Argo tracking metadata, not spec changes.

## Sync safety posture

Every Application in this tree is configured for **manual sync only**. No
`automated` block is present on any Application, which means:

- `prune` is OFF: Argo will never delete live resources.
- `selfHeal` is OFF: Argo will not revert out-of-band changes.
- `orphanedResources.warn: true`: drift is surfaced as a warning, not acted on.
- No `metadata.finalizers`: deleting an Application object never cascades
  to live workloads.

Always run `argocd app diff <name>` before syncing. A safe diff shows only
the `argocd.argoproj.io/tracking-id` annotation being added; any spec diff
means the committed source does not match the live state and must be
reconciled in git first.

## Why external-secrets operator and CRs are separate Applications

`platform/external-secrets.yaml` owns the ESO Helm release and its CRDs.
A separate `secrets/secretstore-externalsecrets.yaml` Application owns the
`ClusterSecretStore` and `ExternalSecret` CRs. Keeping them separate prevents
two Applications from claiming the same CRD types. The ESO operator must be
healthy and its CRDs present before the CRs Application is synced.

## Annotation resource tracking (mandatory)

`application.resourceTrackingMethod: annotation` must be set at Argo CD
install time, before any Helm release is adopted. Annotation tracking prevents
Argo from mutating immutable `app.kubernetes.io/instance` label selectors in
Deployment and StatefulSet specs. It is set in `bootstrap/argocd/values.yaml`.

## Istio placeholder

`apps/mesh/istio.yaml` is a placeholder Application for the Istio service
mesh. It must remain **unsynced** until an Istio adoption design is completed.
See that file's header for the required design decisions and adoption blockers.

## ApplicationSets

App-of-apps is the recommended starting pattern. Migrating to ApplicationSets
is the natural evolution once per-app policy variation stabilizes and uniform
generator-driven configuration is preferred.
