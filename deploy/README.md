# deploy

## Docker

```bash
docker compose up --build            # ERP on :8000, review UI on :8001
docker compose run --rm evals        # 200 scenarios; exits non-zero on a safety failure
docker compose run --rm agent        # one invoice; needs a key in .env
```

One image, two entry points. The services share every dependency, so building
them separately would double build time and registry footprint to save nothing
— `command:` picks which one runs. The dataset is baked into the image rather
than mounted: it is a pure function of a seed and a config that both live in
version control, so an image and its data are one reproducible artefact.

## Kubernetes

```bash
kubectl apply -k deploy/k8s
kubectl -n proc-agent create secret generic proc-agent-secrets \
  --from-literal=ANTHROPIC_API_KEY=...
kubectl -n proc-agent port-forward svc/review 8001:8001
```

Four decisions worth defending:

**The ERP is a StatefulSet, the review UI is a Deployment.** The approval
database is the audit trail, so it needs a volume that survives a reschedule.
The review UI holds nothing — it is a client — so it scales horizontally and the
approval state stays where the state machine can guard it.

**`replicas: 1` on the ERP, and that is a real constraint, not an oversight.**
SQLite is a single-writer store; a second replica would serve stale approvals.
At this scale that is the right trade (see `decisions.md`), and the thing that
changes it is Postgres, not more replicas.

**Readiness and liveness hit the same endpoint with different thresholds.** A
slow start must not be mistaken for a hung process. The review UI's `/healthz`
reports whether the *ERP* is reachable rather than whether the process booted,
so a pod with no path to the ERP is pulled out of service instead of serving
broken pages.

**The NetworkPolicy is belt and braces, not the mechanism.** The agent must not
approve its own proposals — but approval and proposal live on the same Service
and port, so no network rule can tell them apart. The separation is enforced
where it can be: the agent's tool registry contains no approval capability, and
a test asserts it. A policy that *looked* like it enforced this would be worse
than none.

## What is missing, on purpose

No Ingress and no TLS: those belong to whatever cluster this lands in, and a
guessed `ingressClassName` is a merge conflict waiting to happen. No
HorizontalPodAutoscaler on the review UI — the load is a person clicking, and an
HPA with nothing to scale on is decoration. No ServiceAccount with RBAC, because
nothing here talks to the Kubernetes API.

These manifests have not been applied to a live cluster. They are validated
structurally by `tests/test_deploy.py` — every document parses,
every workload sets a non-root security context, drops all capabilities, and
declares both probes — which is a real check and not the same as a green
`kubectl apply`.
