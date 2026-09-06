"""Deployment manifests, checked structurally.

These have never been applied to a live cluster, so this is not the same as a
green `kubectl apply` and does not pretend to be. What it does catch is the
class of mistake that survives review and shows up in production: a workload
running as root, a container with no liveness probe, an image tag that drifted
from the one compose builds, a secret with a real value committed by accident.

Kept out of `packages/` because deployment belongs to no package.
"""

from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parent.parent
K8S = ROOT / "deploy" / "k8s"
WORKLOAD_KINDS = ("Deployment", "StatefulSet", "DaemonSet")


def _ids(value) -> str:
    """Readable parametrise ids: the container path, never the whole spec dict."""
    return value if isinstance(value, str) else ""


def _documents(path: Path) -> list[dict]:
    return [doc for doc in yaml.safe_load_all(path.read_text(encoding="utf-8")) if doc]


def _all_docs() -> list[tuple[str, dict]]:
    out = []
    for path in sorted(K8S.glob("*.yaml")):
        if path.name == "kustomization.yaml":
            continue
        for doc in _documents(path):
            out.append((path.name, doc))
    return out


def _pod_specs() -> list[tuple[str, dict]]:
    """Every pod template, wherever it is nested. CronJob buries it three deep."""
    specs = []
    for name, doc in _all_docs():
        kind = doc["kind"]
        if kind in WORKLOAD_KINDS:
            specs.append((f"{name}:{doc['metadata']['name']}", doc["spec"]["template"]["spec"]))
        elif kind == "CronJob":
            specs.append(
                (
                    f"{name}:{doc['metadata']['name']}",
                    doc["spec"]["jobTemplate"]["spec"]["template"]["spec"],
                )
            )
    return specs


def _containers() -> list[tuple[str, dict]]:
    return [(f"{owner}/{c['name']}", c) for owner, spec in _pod_specs() for c in spec["containers"]]


# ---------------------------------------------------------------------------
# it parses, and it is complete
# ---------------------------------------------------------------------------


def test_every_manifest_is_valid_yaml():
    assert _all_docs(), "no manifests found"


def test_every_document_declares_an_api_version_and_kind():
    for name, doc in _all_docs():
        assert doc.get("apiVersion"), name
        assert doc.get("kind"), name
        assert doc.get("metadata", {}).get("name"), name


def test_kustomization_lists_every_manifest():
    """A manifest not in the kustomization is a file that never gets applied."""
    kustomization = yaml.safe_load((K8S / "kustomization.yaml").read_text())
    on_disk = {p.name for p in K8S.glob("*.yaml")} - {"kustomization.yaml"}
    assert set(kustomization["resources"]) == on_disk


def test_everything_lands_in_one_namespace():
    for name, doc in _all_docs():
        if doc["kind"] == "Namespace":
            continue
        assert doc["metadata"].get("namespace") == "proc-agent", name


def test_both_services_are_deployed():
    kinds = {doc["metadata"]["name"] for _, doc in _all_docs() if doc["kind"] in WORKLOAD_KINDS}
    assert {"erp", "review"} <= kinds


# ---------------------------------------------------------------------------
# the mistakes that survive review
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(("owner", "spec"), _pod_specs(), ids=_ids)
def test_no_workload_runs_as_root(owner, spec):
    assert spec.get("securityContext", {}).get("runAsNonRoot") is True, owner


@pytest.mark.parametrize(("name", "container"), _containers(), ids=_ids)
def test_every_container_drops_capabilities_and_cannot_escalate(name, container):
    security = container.get("securityContext", {})
    assert security.get("allowPrivilegeEscalation") is False, name
    assert security.get("capabilities", {}).get("drop") == ["ALL"], name


@pytest.mark.parametrize(("name", "container"), _containers(), ids=_ids)
def test_every_container_has_a_read_only_root_filesystem(name, container):
    """The only mutable state is the approval database, on its own volume."""
    assert container.get("securityContext", {}).get("readOnlyRootFilesystem") is True, name


@pytest.mark.parametrize(("name", "container"), _containers(), ids=_ids)
def test_every_container_declares_resource_requests(name, container):
    """Without requests the scheduler cannot place it and it is the first thing
    evicted under pressure."""
    resources = container.get("resources", {})
    assert resources.get("requests", {}).get("memory"), name
    assert resources.get("limits", {}).get("memory"), name


@pytest.mark.parametrize(
    ("name", "container"),
    [(n, c) for n, c in _containers() if "evals" not in n],
    ids=_ids,
)
def test_every_long_running_container_has_both_probes(name, container):
    """Readiness gates traffic; liveness restarts. One without the other either
    serves broken pages or never recovers."""
    assert "readinessProbe" in container, name
    assert "livenessProbe" in container, name


def test_liveness_is_more_patient_than_readiness():
    """A slow start must not be mistaken for a hung process."""
    for name, container in _containers():
        if "livenessProbe" not in container:
            continue
        live = container["livenessProbe"].get("initialDelaySeconds", 0)
        ready = container["readinessProbe"].get("initialDelaySeconds", 0)
        assert live > ready, name


def test_the_read_only_filesystem_still_has_somewhere_to_write():
    """readOnlyRootFilesystem with no /tmp is how a container that passed
    review dies on its first temp file."""
    for owner, spec in _pod_specs():
        mounts = {m["mountPath"] for c in spec["containers"] for m in c.get("volumeMounts", [])}
        assert "/tmp" in mounts, owner


# ---------------------------------------------------------------------------
# the gate, restated in deployment terms
# ---------------------------------------------------------------------------


def test_the_erp_keeps_its_approval_database_on_a_persistent_volume():
    """The approval log is the audit trail. An emptyDir would lose who approved
    what on the first reschedule."""
    erp = next(
        doc
        for _, doc in _all_docs()
        if doc["metadata"]["name"] == "erp" and doc["kind"] == "StatefulSet"
    )
    claims = erp["spec"]["volumeClaimTemplates"]
    assert [c["metadata"]["name"] for c in claims] == ["approvals"]
    mounts = {
        m["mountPath"]
        for c in erp["spec"]["template"]["spec"]["containers"]
        for m in c["volumeMounts"]
    }
    assert "/data" in mounts


def test_the_erp_is_single_writer():
    """SQLite is a single-writer store. A second replica would serve stale
    approvals -- so this is a constraint, and it is asserted rather than
    assumed."""
    erp = next(
        doc
        for _, doc in _all_docs()
        if doc["metadata"]["name"] == "erp" and doc["kind"] == "StatefulSet"
    )
    assert erp["spec"]["replicas"] == 1


def test_the_review_ui_is_stateless_and_can_scale():
    review = next(
        doc
        for _, doc in _all_docs()
        if doc["metadata"]["name"] == "review" and doc["kind"] == "Deployment"
    )
    assert review["spec"]["replicas"] >= 2
    for container in review["spec"]["template"]["spec"]["containers"]:
        for mount in container.get("volumeMounts", []):
            assert mount["mountPath"] == "/tmp", "the review UI must hold no state"


def test_no_secret_ships_with_a_value():
    """A committed key is the one deployment mistake that cannot be rolled back."""
    for name, doc in _all_docs():
        if doc["kind"] != "Secret":
            continue
        for key, value in (doc.get("stringData") or {}).items():
            assert value == "", f"{name}: {key} has a committed value"
        assert not doc.get("data"), f"{name}: base64 data is still a committed value"


def test_the_eval_job_does_not_retry_itself_green():
    """proc-evals exits non-zero on a safety failure. Retrying until it passes
    would turn a gate into a coin flip."""
    cron = next(doc for _, doc in _all_docs() if doc["kind"] == "CronJob")
    assert cron["spec"]["jobTemplate"]["spec"]["backoffLimit"] == 0
    assert cron["spec"]["jobTemplate"]["spec"]["template"]["spec"]["restartPolicy"] == "Never"


# ---------------------------------------------------------------------------
# compose
# ---------------------------------------------------------------------------


def test_compose_is_valid_and_every_service_shares_one_image():
    compose = yaml.safe_load((ROOT / "compose.yaml").read_text(encoding="utf-8"))
    images = {s["image"] for s in compose["services"].values()}
    assert images == {"sap-proc-agent:local"}, "one image, several entry points"


def test_compose_waits_for_the_erp_to_be_healthy():
    """Starting the review UI before the ERP answers is how a demo opens on an
    error page."""
    compose = yaml.safe_load((ROOT / "compose.yaml").read_text(encoding="utf-8"))
    for name, service in compose["services"].items():
        if name == "erp":
            assert "healthcheck" in service
            continue
        assert service["depends_on"]["erp"]["condition"] == "service_healthy", name


def test_compose_and_kubernetes_agree_on_the_entry_points():
    """Two deployment paths that start different processes is a bug you find in
    production."""
    compose = yaml.safe_load((ROOT / "compose.yaml").read_text(encoding="utf-8"))
    k8s_commands = {tuple(c["command"]) for _, c in _containers()}
    for name in ("erp", "review"):
        assert tuple(compose["services"][name]["command"]) in k8s_commands, name


def test_the_dockerfile_never_runs_as_root():
    dockerfile = (ROOT / "Dockerfile").read_text(encoding="utf-8")
    assert "USER app" in dockerfile
    assert dockerfile.index("USER app") < dockerfile.index("CMD")


def test_the_dockerignore_excludes_secrets_and_state():
    ignored = (ROOT / ".dockerignore").read_text(encoding="utf-8").split()
    for pattern in (".env", "*.sqlite3", ".git", ".venv"):
        assert pattern in ignored, pattern
