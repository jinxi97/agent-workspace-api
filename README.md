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
