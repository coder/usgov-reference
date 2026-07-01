#!/usr/bin/env python3
"""
setup-gitlab-ci-runners.py - provision the demo "Coder templates" GitLab CI
pipeline and the GitLab Runner authentication token for the <CLUSTER_NAME>
GovCloud demo.

What it does (idempotent; re-running reconciles in place):

  1. Coder: log in to https://dev.<BASE_DOMAIN> as the admin owner
     (CODER_ADMIN_EMAIL / CODER_ADMIN_PASSWORD), rotate a named API token
     ("gitlab-ci") at the server's maximum allowed lifetime, and capture it for
     the GitLab CI/CD variable. The token grants template-admin rights in the
     target org (the owner is template admin everywhere).

  2. GitLab (via gitlab-rails inside the gitlab-0 pod, the established admin
     pattern):
       a. DELETE the old project root/coder-templates if present (a clean
          destroy via Projects::DestroyService; this also removes its registry
          images and its project runner).
       b. CREATE <GITLAB_GROUP>/coder-templates in the <GITLAB_GROUP> group, seed
          it from the local seed directory (SEED_DIR; see configuration below),
          protect the default branch, and set the CODER_SESSION_TOKEN CI/CD
          variable (masked + protected). The seed commit carries [skip ci] so
          the pipeline is triggered explicitly only after the runner is back
          online.
       c. CREATE a GROUP runner authentication token (glrt-...) on the
          <GITLAB_GROUP> group, which serves <GITLAB_GROUP>/coder-templates.

  3. AWS Secrets Manager: upsert the runner authentication token into
     <CLUSTER_NAME>/gitlab/runner as {"runner-token": "...",
     "runner-registration-token": ""}, the source of truth that ESO syncs into
     the gitlab-runner namespace (deploy/gitlab-runner/externalsecret.yaml).

No secret value is ever printed or written to git. The Coder admin password is
read from ~/.config/<CLUSTER_NAME>/generated-secrets.env. The Coder token is
passed to gitlab-rails over stdin -> env (never argv). The runner token is
written to a 0600 file inside the pod and retrieved over a captured exec, then
removed.

After this script: re-sync the ESO ExternalSecret and (re)start the runner so it
re-registers with the new group token, then trigger a pipeline on the default
branch. See deploy/gitlab-runner/README.md.

Usage (from the repo root, with the demo kubeconfig + env):
    . ~/.config/<CLUSTER_NAME>/env
    export KUBECONFIG=$WORKSPACE_ROOT/<CLUSTER_NAME>/kubeconfig
    python3 scripts/setup-gitlab-ci-runners.py
"""
import json
import os
import subprocess
import sys
import tempfile
import urllib.error
import urllib.request

# --- Configuration -----------------------------------------------------------
NAMESPACE = "gitlab"
POD = "gitlab-0"
CONTAINER = "gitlab"  # the omnibus container (gitlab-0 also runs an istio sidecar)

CODER_URL = "https://dev.<BASE_DOMAIN>"
CODER_TOKEN_NAME = "gitlab-ci"

# Old project to delete (root's personal namespace), and the new project to
# create in the <GITLAB_GROUP> group.
OLD_PROJECT_PATH = "root/coder-templates"
GROUP_PATH = "<GITLAB_GROUP>"
PROJECT_PATH = "<GITLAB_GROUP>/coder-templates"
PROJECT_NAME = "coder-templates"
PROJECT_DESC = "GitLab CI builds UBI9 workspace images and pushes a Coder template."

# Actors. austen.platform is Owner of the <GITLAB_GROUP> group (and admin), so it can
# create + seed the group project. root owns the old personal-namespace project.
GROUP_ACTOR = "austen.platform"
DELETE_ACTOR = "root"

# Group runner serves every project in the <GITLAB_GROUP> group.
RUNNER_DESC = "<GITLAB_GROUP> coder-templates k8s runner (<CLUSTER_NAME>)"

REGION = "us-gov-west-1"
ASM_RUNNER = "<CLUSTER_NAME>/gitlab/runner"

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
# Populate deploy/gitlab-runner/coder-templates-seed/ with the project seed
# files (.gitlab-ci.yml, optional image build contexts, example Coder template)
# before running. The script aborts if this directory is missing.
SEED_DIR = os.path.join(REPO_ROOT, "deploy", "gitlab-runner",
                        "coder-templates-seed")

POD_SEED_DIR = "/tmp/coder-templates-seed"
POD_TOKEN_FILE = "/tmp/gl-runner-token"


# --- Secrets -----------------------------------------------------------------
def read_secret(*keys):
    """Read selected keys from generated-secrets.env without echoing values."""
    path = os.path.expanduser("~/.config/<CLUSTER_NAME>/generated-secrets.env")
    out = {}
    with open(path) as f:
        for line in f:
            line = line.strip()
            if "=" in line and not line.startswith("#"):
                k, v = line.split("=", 1)
                if k in keys:
                    out[k] = v
    missing = [k for k in keys if k not in out]
    if missing:
        print(f"missing secrets in generated-secrets.env: {missing}",
              file=sys.stderr)
        sys.exit(1)
    return out


# --- Coder API ---------------------------------------------------------------
def coder_request(method, path, token=None, body=None):
    headers = {}
    data = None
    if body is not None:
        headers["Content-Type"] = "application/json"
        data = json.dumps(body).encode()
    if token:
        headers["Coder-Session-Token"] = token
    req = urllib.request.Request(CODER_URL + path, data=data,
                                 headers=headers, method=method)
    try:
        r = urllib.request.urlopen(req)
        raw = r.read().decode()
        return r.status, (json.loads(raw) if raw else None)
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode()


def coder_login(email, password):
    status, body = coder_request(
        "POST", "/api/v2/users/login",
        body={"email": email, "password": password})
    if status != 201 or not isinstance(body, dict) or "session_token" not in body:
        print(f"Coder login failed (HTTP {status})", file=sys.stderr)
        sys.exit(1)
    return body["session_token"]


def coder_rotate_token(session):
    """Delete any existing CI token of the same name, then create a fresh one at
    the server's maximum allowed lifetime. Returns the new token string."""
    # Look up the server's max token lifetime (nanoseconds).
    status, cfg = coder_request(
        "GET", "/api/v2/users/me/keys/tokens/tokenconfig", token=session)
    max_lifetime = 0
    if status == 200 and isinstance(cfg, dict):
        max_lifetime = int(cfg.get("max_token_lifetime", 0))
    # Delete an existing token of the same name (rotate).
    status, tokens = coder_request(
        "GET", "/api/v2/users/me/keys/tokens", token=session)
    if status == 200 and isinstance(tokens, list):
        for t in tokens:
            if t.get("token_name") == CODER_TOKEN_NAME:
                coder_request("DELETE", f"/api/v2/users/me/keys/{t['id']}",
                              token=session)
                print(f"Coder token '{CODER_TOKEN_NAME}': rotated "
                      f"(deleted prior id {t['id'][:8]})")
    body = {"token_name": CODER_TOKEN_NAME, "scope": "all"}
    if max_lifetime > 0:
        body["lifetime"] = max_lifetime
    status, created = coder_request(
        "POST", "/api/v2/users/me/keys/tokens", token=session, body=body)
    if status != 201 or not isinstance(created, dict) or "key" not in created:
        print(f"Coder token create failed (HTTP {status}): {created}",
              file=sys.stderr)
        sys.exit(1)
    days = max_lifetime / (1e9 * 86400) if max_lifetime else 0
    print(f"Coder token '{CODER_TOKEN_NAME}': created "
          f"(scope=all, lifetime~{days:.0f}d)")
    return created["key"]


# --- kubectl helpers ---------------------------------------------------------
def kubectl(args, stdin_data=None, capture=True):
    return subprocess.run(
        ["kubectl", "-n", NAMESPACE, *args],
        input=stdin_data, text=True,
        capture_output=capture)


def rails(ruby, env_lines=None):
    """Run a Ruby script via gitlab-rails. The script is staged on stdin so no
    code or secret is on the command line. Optional env_lines (list of values)
    are read by a tiny shell shim into env vars before exec."""
    stage = kubectl(["exec", "-i", "-c", CONTAINER, POD, "--",
                     "sh", "-c", "cat > /tmp/_ci_setup.rb"], stdin_data=ruby)
    if stage.returncode != 0:
        print(stage.stderr, file=sys.stderr)
        sys.exit(1)
    if env_lines is None:
        shell = ("gitlab-rails runner /tmp/_ci_setup.rb; rc=$?; "
                 "rm -f /tmp/_ci_setup.rb; exit $rc")
        r = kubectl(["exec", "-i", "-c", CONTAINER, POD, "--", "sh", "-c", shell])
    else:
        # First line read into CODER_SESSION_TOKEN, passed via env (not argv).
        shell = ("read -r CST; "
                 "CODER_SESSION_TOKEN=\"$CST\" "
                 "gitlab-rails runner /tmp/_ci_setup.rb; rc=$?; "
                 "rm -f /tmp/_ci_setup.rb; exit $rc")
        r = kubectl(["exec", "-i", "-c", CONTAINER, POD, "--", "sh", "-c", shell],
                    stdin_data="\n".join(env_lines) + "\n")
    return r


# --- Ruby payloads -----------------------------------------------------------
RUBY_DELETE_OLD_PROJECT = r'''
actor = User.find_by(username: "%(actor)s") or abort("actor not found")
project = Project.find_by_full_path("%(old)s")
if project.nil?
  puts "delete %(old)s: already absent"
else
  id = project.id
  res = ::Projects::DestroyService.new(project, actor).execute
  puts "delete %(old)s: #{res ? "destroyed id=#{id}" : "FAILED id=#{id}"}"
end
# Verify the path is free for re-use.
puts "delete %(old)s: still_present=#{!Project.find_by_full_path("%(old)s").nil?}"
'''

RUBY_ENSURE_PROJECT = r'''
actor = User.find_by(username: "%(actor)s") or abort("actor not found")
group = Group.find_by_full_path("%(group)s") or abort("group %(group)s not found")
full  = "%(full)s"
project = Project.find_by_full_path(full)
if project.nil?
  res = ::Projects::CreateService.new(
    actor,
    name: "%(name)s", path: "%(name)s",
    namespace_id: group.id,
    organization_id: group.organization_id,
    description: "%(desc)s",
    visibility_level: Gitlab::VisibilityLevel::PRIVATE,
    initialize_with_readme: false
  ).execute
  project = res.is_a?(Project) ? res : (res[:project] if res.respond_to?(:[]))
  abort("project create failed: #{res.respond_to?(:[]) ? res[:message] : res.inspect}") unless project&.persisted?
  puts "project #{full}: CREATED id=#{project.id} namespace=#{project.namespace.full_path}"
else
  puts "project #{full}: exists id=#{project.id} namespace=#{project.namespace.full_path}"
end
'''

RUBY_SEED_PROTECT = r'''
actor = User.find_by(username: "%(actor)s") or abort("actor not found")
project = Project.find_by_full_path("%(full)s") or abort("project not found")

# Seed files from the staged directory. Only files that are new or whose content
# differs become commit actions, so a re-run with identical content is a true
# no-op (no empty commit). The commit message carries [skip ci] so seeding never
# triggers a pipeline on its own; the pipeline is triggered explicitly after the
# runner is back online.
branch = project.default_branch || "main"
seed = "%(seed)s"
actions = []
Dir.glob(File.join(seed, "**", "*"), File::FNM_DOTMATCH).each do |fp|
  next if File.directory?(fp)
  rel = fp.sub(/\A#{Regexp.escape(seed)}\/?/, "")
  next if rel.empty?
  content = File.binread(fp)
  blob = project.empty_repo? ? nil : project.repository.blob_at(branch, rel)
  if blob.nil?
    actions << { action: "create", file_path: rel, content: content }
  elsif blob.data.b != content.b
    actions << { action: "update", file_path: rel, content: content }
  end
end
actions.sort_by! { |a| a[:file_path] }
if actions.empty?
  puts "seed: no changes (already up to date)"
else
  start = project.empty_repo? ? nil : branch
  res = ::Files::MultiService.new(
    project, actor,
    start_branch: start, branch_name: branch,
    commit_message: "chore: seed coder-templates demo [skip ci] (Coder Agents)",
    actions: actions
  ).execute
  if res[:status] == :success
    puts "seed: committed #{actions.length} file(s) to #{branch}"
  else
    puts "seed: ERROR #{res[:message]}"
  end
end

# Ensure the default branch is set and protected (so masked + protected CI/CD
# variables are exposed to default-branch pipelines).
project.reload
db = project.default_branch || branch
project.change_head(db) if project.default_branch.nil?
unless project.protected_branches.exists?(name: db)
  ::ProtectedBranches::CreateService.new(
    project, actor,
    name: db,
    push_access_levels_attributes: [{ access_level: Gitlab::Access::MAINTAINER }],
    merge_access_levels_attributes: [{ access_level: Gitlab::Access::MAINTAINER }]
  ).execute
  puts "protected branch: #{db}"
else
  puts "protected branch: #{db} (already)"
end
'''

RUBY_SET_CI_VARIABLE = r'''
project = Project.find_by_full_path("%(full)s") or abort("project not found")
val = ENV["CODER_SESSION_TOKEN"].to_s
abort("CODER_SESSION_TOKEN empty") if val.empty?
key = "CODER_SESSION_TOKEN"
var = project.variables.find_or_initialize_by(key: key)
var.value = val
var.masked = true
var.protected = true
var.variable_type = "env_var"
var.save!
puts "ci variable #{key}: masked=#{var.masked} protected=#{var.protected}"
'''

RUBY_GROUP_RUNNER = r'''
actor = User.find_by(username: "%(actor)s") or abort("actor not found")
group = Group.find_by_full_path("%(group)s") or abort("group %(group)s not found")

# Find or create the GROUP runner authentication token. A group runner serves
# every project in the group, including %(full)s.
runner = group.runners.find_by(description: "%(rdesc)s")
if runner.nil?
  resp = ::Ci::Runners::CreateRunnerService.new(
    user: actor,
    params: {
      runner_type: "group_type", scope: group,
      description: "%(rdesc)s",
      tag_list: ["kubernetes", "coder"],
      run_untagged: true
    }
  ).execute
  abort("runner create failed: #{resp.message}") unless resp.success?
  runner = resp.payload[:runner]
  puts "group runner: CREATED id=#{runner.id} type=#{runner.runner_type} untagged=#{runner.run_untagged}"
else
  puts "group runner: exists id=#{runner.id} type=#{runner.runner_type}"
end

# Write the auth token to a 0600 file for out-of-band retrieval (never printed).
File.open("%(tokenfile)s", File::WRONLY | File::CREAT | File::TRUNC, 0o600) do |f|
  f.write(runner.token.to_s)
end
puts "runner token: staged (glrt prefix=#{runner.token.to_s.start_with?('glrt-')})"
'''


# --- AWS Secrets Manager -----------------------------------------------------
def asm_exists(name):
    r = subprocess.run(
        ["aws", "secretsmanager", "describe-secret", "--region", REGION,
         "--secret-id", name],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    return r.returncode == 0


def asm_put(name, payload):
    fd, path = tempfile.mkstemp(prefix="asm-", suffix=".json")
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "w") as f:
            json.dump(payload, f)
        ref = "file://" + path
        if asm_exists(name):
            subprocess.run(
                ["aws", "secretsmanager", "put-secret-value", "--region", REGION,
                 "--secret-id", name, "--secret-string", ref],
                check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
            return "updated"
        subprocess.run(
            ["aws", "secretsmanager", "create-secret", "--region", REGION,
             "--name", name,
             "--description", "<CLUSTER_NAME> GitLab Runner auth token (ESO).",
             "--secret-string", ref],
            check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        return "created"
    finally:
        os.unlink(path)


# --- Main --------------------------------------------------------------------
def main():
    secrets = read_secret("CODER_ADMIN_EMAIL", "CODER_ADMIN_PASSWORD")

    # 1. Coder token.
    session = coder_login(secrets["CODER_ADMIN_EMAIL"],
                          secrets["CODER_ADMIN_PASSWORD"])
    coder_token = coder_rotate_token(session)

    # 2. Stage the example project into the pod.
    if not os.path.isdir(SEED_DIR):
        print(f"seed dir not found: {SEED_DIR}", file=sys.stderr)
        sys.exit(1)
    kubectl(["exec", "-c", CONTAINER, POD, "--", "rm", "-rf", POD_SEED_DIR])
    cp = subprocess.run(
        ["kubectl", "-n", NAMESPACE, "cp", "-c", CONTAINER,
         SEED_DIR + "/.", f"{POD}:{POD_SEED_DIR}"],
        capture_output=True, text=True)
    if cp.returncode != 0:
        print("kubectl cp failed:\n" + cp.stderr, file=sys.stderr)
        sys.exit(1)
    print(f"staged example project -> {POD}:{POD_SEED_DIR}")

    # 3. Delete the old root/coder-templates project (clean destroy: removes its
    #    registry images and its project runner). Idempotent.
    r = rails(RUBY_DELETE_OLD_PROJECT % {
        "actor": DELETE_ACTOR, "old": OLD_PROJECT_PATH})
    sys.stdout.write(r.stdout)
    if r.returncode != 0:
        sys.stderr.write(r.stderr)
        sys.exit(r.returncode)

    # 4. Ensure the new project exists in the <GITLAB_GROUP> group.
    r = rails(RUBY_ENSURE_PROJECT % {
        "actor": GROUP_ACTOR, "group": GROUP_PATH, "name": PROJECT_NAME,
        "full": PROJECT_PATH, "desc": PROJECT_DESC})
    sys.stdout.write(r.stdout)
    if r.returncode != 0:
        sys.stderr.write(r.stderr)
        sys.exit(r.returncode)

    # 5. Set the masked + protected CODER_SESSION_TOKEN CI/CD variable BEFORE the
    #    pipeline is triggered, so the push-template job reads the token rotated
    #    in step 1.
    r = rails(RUBY_SET_CI_VARIABLE % {"full": PROJECT_PATH},
              env_lines=[coder_token])
    sys.stdout.write(r.stdout)
    if r.returncode != 0:
        sys.stderr.write(r.stderr)
        sys.exit(r.returncode)

    # 6. Seed files ([skip ci]) + protect the default branch.
    r = rails(RUBY_SEED_PROTECT % {
        "actor": GROUP_ACTOR, "full": PROJECT_PATH, "seed": POD_SEED_DIR})
    sys.stdout.write(r.stdout)
    if r.returncode != 0:
        sys.stderr.write(r.stderr)
        sys.exit(r.returncode)

    # 7. Create/find the GROUP runner authentication token on the <GITLAB_GROUP> group.
    r = rails(RUBY_GROUP_RUNNER % {
        "actor": GROUP_ACTOR, "group": GROUP_PATH, "full": PROJECT_PATH,
        "rdesc": RUNNER_DESC, "tokenfile": POD_TOKEN_FILE})
    sys.stdout.write(r.stdout)
    if r.returncode != 0:
        sys.stderr.write(r.stderr)
        sys.exit(r.returncode)

    # 8. Retrieve the runner token (captured, never echoed) -> ASM.
    got = kubectl(["exec", "-i", "-c", CONTAINER, POD, "--",
                   "sh", "-c", f"cat {POD_TOKEN_FILE}"])
    runner_token = (got.stdout or "").strip()
    kubectl(["exec", "-c", CONTAINER, POD, "--", "rm", "-f", POD_TOKEN_FILE])
    if not runner_token.startswith("glrt-"):
        print("failed to retrieve a glrt- runner token", file=sys.stderr)
        sys.exit(1)
    action = asm_put(ASM_RUNNER, {
        "runner-token": runner_token,
        "runner-registration-token": "",
    })
    print(f"ASM {ASM_RUNNER}: {action} (runner-token, {len(runner_token)} chars)")

    # Cleanup staged files.
    kubectl(["exec", "-c", CONTAINER, POD, "--", "rm", "-rf", POD_SEED_DIR])

    print("\nDone. Next (re-register the runner with the new group token):")
    print("  # Force ESO to re-sync the rotated token, then roll the runner.")
    print("  kubectl -n gitlab-runner delete secret gitlab-runner-auth --ignore-not-found")
    print("  kubectl -n gitlab-runner annotate externalsecret gitlab-runner-auth \\")
    print("    force-sync=$(date +%s) --overwrite")
    print("  helm upgrade --install gitlab-runner gitlab/gitlab-runner \\")
    print("    --version 0.89.1 --namespace gitlab-runner \\")
    print("    -f deploy/gitlab-runner/values.yaml")
    print("  # Then trigger a pipeline on the default branch (see README).")


if __name__ == "__main__":
    main()
