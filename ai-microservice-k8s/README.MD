# AI Microservice — Kubernetes Deployment

Kubernetes equivalent of `docker-compose.yml`. Structured with **Kustomize**:
a shared `base/` plus two overlays so you can point the same manifests at a
laptop cluster or a managed cloud cluster without duplicating YAML.

```
k8s/
├── base/
│   ├── namespace.yaml
│   ├── configmap.yaml            # REDIS_URL, VALID_API_KEYS
│   ├── secret.yaml                # template — see below
│   ├── redis-pvc.yaml             # replaces the redis-data docker volume
│   ├── redis-deployment.yaml      # redis:7-alpine, same flags as compose
│   ├── redis-service.yaml         # DNS name: redis-service
│   ├── ai-service-deployment.yaml # your Dockerfile image, 2 replicas
│   ├── ai-service-service.yaml
│   ├── hpa.yaml                   # autoscale 2→6 pods on CPU/memory
│   ├── redis-commander.yaml       # optional debug UI, apply separately
│   └── kustomization.yaml
└── overlays/
    ├── local/                     # minikube / kind — NodePort :30080
    └── cloud/                     # EKS/GKE/AKS — Ingress
```

## 1. Build & push the image

Your existing `Dockerfile` is used as-is — nothing about it needs to change.

```bash
# Local cluster (kind/minikube): build directly into the cluster's image store
docker build -t ai-microservice:latest .
kind load docker-image ai-microservice:latest        # kind
# or: minikube image load ai-microservice:latest      # minikube

# Cloud cluster: push to a registry the cluster can pull from
docker build -t your-registry/ai-microservice:latest .
docker push your-registry/ai-microservice:latest
```

Then update the `image:` field in `base/ai-service-deployment.yaml` to match
whatever you actually tagged/pushed.

## 2. Create the Anthropic API key secret

Don't edit `secret.yaml` with a real key. Create it imperatively instead:

```bash
kubectl create namespace ai-microservice
kubectl create secret generic ai-service-secrets \
  --namespace ai-microservice \
  --from-literal=ANTHROPIC_API_KEY=sk-ant-your-actual-key-here
```

(If you'd rather apply it declaratively, base64-encode the key and drop it
into `secret.yaml`, then remove `secret.yaml` from `base/kustomization.yaml`'s
resource list so it isn't applied twice.)

## 3. Deploy

```bash
# Local (minikube/kind)
kubectl apply -k overlays/local

# Cloud (EKS/GKE/AKS) — requires an ingress controller installed
# (e.g. `kubectl apply -f https://.../ingress-nginx/deploy.yaml`)
kubectl apply -k overlays/cloud
```

## 4. Verify

```bash
kubectl -n ai-microservice get pods
kubectl -n ai-microservice get svc

# Local: hit it via NodePort
curl http://$(minikube ip):30080/health          # minikube
curl http://localhost:30080/health                # kind with port mapping

# Cloud: hit it via the Ingress host, or port-forward for a quick check
kubectl -n ai-microservice port-forward svc/ai-service 8000:8000
curl http://localhost:8000/health
```

## 5. Optional: Redis Commander debug UI

Equivalent to `docker compose --profile debug up`:

```bash
kubectl apply -f base/redis-commander.yaml
kubectl -n ai-microservice port-forward svc/redis-commander 8081:8081
# open http://localhost:8081
```

## Notes / differences from docker-compose

- **Volumes for `aiguardrail-pipeline` / `aillm-observability`**: compose
  bind-mounts these from your host. Host-path mounts don't make sense across
  a multi-node cluster. If your app actually imports code from those repos
  at runtime (not just the commented-out `COPY` lines in the Dockerfile),
  bake them into the image at build time instead.
- **Healthcheck**: compose's `HEALTHCHECK` becomes K8s `readinessProbe` /
  `livenessProbe` / `startupProbe` hitting the same `/health` endpoint.
- **Restart policy**: `restart: unless-stopped` → Deployments already
  restart failed pods by default (`restartPolicy: Always`).
- **Scaling**: `ai-service` runs 2 replicas by default (1 on the `local`
  overlay) plus an `HorizontalPodAutoscaler` that scales up to 6 under load —
  compose has no equivalent since it's single-node.
- **Networking**: compose's bridge network + service-name DNS maps directly
  to K8s ClusterIP Services + built-in cluster DNS (`redis-service`,
  `ai-service`).
