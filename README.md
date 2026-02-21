# FastAPI Docker + GKE Deployment

## 1) Build and run locally with Docker

```bash
docker build -t fastapi-app:local .
docker run --rm -p 8080:8080 fastapi-app:local
```

Test:

```bash
curl http://localhost:8080/
curl http://localhost:8080/healthz
```

## 2) Push image to Artifact Registry

Set variables:

```bash
export PROJECT_ID="your-gcp-project-id"
export REGION="us-central1"
export REPOSITORY="your-artifact-repo"
export IMAGE="fastapi-app"
export TAG="v1"
```

Build and push:

```bash
gcloud auth configure-docker ${REGION}-docker.pkg.dev
docker build -t ${REGION}-docker.pkg.dev/${PROJECT_ID}/${REPOSITORY}/${IMAGE}:${TAG} .
docker push ${REGION}-docker.pkg.dev/${PROJECT_ID}/${REPOSITORY}/${IMAGE}:${TAG}
```

## 3) Deploy to GKE

Get cluster credentials:

```bash
gcloud container clusters get-credentials YOUR_CLUSTER_NAME --region ${REGION} --project ${PROJECT_ID}
```

Update image in `k8s/deployment.yaml`:

```text
REGION-docker.pkg.dev/PROJECT_ID/REPOSITORY/fastapi-app:TAG
```

Apply manifests:

```bash
kubectl apply -f k8s/deployment.yaml
kubectl apply -f k8s/service.yaml
```

Check status:

```bash
kubectl get pods
kubectl get svc fastapi-app
kubectl describe svc fastapi-app
```

When `EXTERNAL-IP` is assigned:

```bash
curl http://EXTERNAL_IP/
curl http://EXTERNAL_IP/healthz
```

## Sandbox file listing endpoint (internal mode)

This app includes:

```text
GET /sandbox/files?path=.&max_depth=2
```

It uses internal mode with `api_url` for the sandbox client, based on:
[agentic-sandbox-client Python example](https://github.com/kubernetes-sigs/agent-sandbox/tree/main/clients/python/agentic-sandbox-client).

Set these env vars if needed:

```bash
export SANDBOX_TEMPLATE_NAME="python-runtime-template"
export SANDBOX_NAMESPACE="default"
export SANDBOX_API_URL="http://sandbox-controller-manager-controller-manager.sandbox-system.svc.cluster.local:8080"
```

Example:

```bash
curl "http://localhost:8080/sandbox/files?path=.&max_depth=2"
```

## Create pod snapshot trigger endpoint

This app includes:

```text
POST /snapshots/triggers
```

Defaults:

```bash
export SNAPSHOT_NAMESPACE="pod-snapshots-ns"
```

Request example:

```bash
curl -X POST "http://localhost:8080/snapshots/triggers" \
  -H "Content-Type: application/json" \
  -d '{
    "workspace_id": "REPLACE_WITH_WORKSPACE_ID"
  }'
```

Optional fields:
- None. This endpoint now only accepts `workspace_id`.

## Check snapshot status endpoint

This app includes:

```text
GET /snapshots/status?trigger_name=<trigger-name>
```

Examples:

```bash
curl "http://localhost:8080/snapshots/status?trigger_name=python-sandbox-warmpool-abc-snapshot-1234"
```

Response includes:
- `ready`: boolean for restore readiness
- `snapshot_name`: created snapshot name (or `null` if not available yet)

## Restore sandbox from snapshot endpoint

This app includes:

```text
POST /snapshots/restore
```

Request example:

```bash
curl -X POST "http://localhost:8080/snapshots/restore" \
  -H "Content-Type: application/json" \
  -d '{
    "snapshot_name": "fbe843d5-0165-49b2-8fa8-409de21f5354"
  }'
```

This creates a `SandboxClaim` in `SNAPSHOT_NAMESPACE` with:
- label `app=agent-sandbox-workload`
- annotation `podsnapshot.gke.io/ps-name=<snapshot_name>`
- `spec.sandboxTemplateRef.name=${SANDBOX_TEMPLATE_NAME}`

On success, it returns only `workspace_id`, which can be used with:
- `POST /workspaces/{workspace_id}/exec`
- `DELETE /workspaces/{workspace_id}`
