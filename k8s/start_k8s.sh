#!/usr/bin/env bash
set -euo pipefail

k3d cluster create doccen --api-port 6666 -p "80:80@loadbalancer"

# --- Secret ----------------------------------------------------------
# deploys.yaml references doc-center-secrets via secretKeyRef. Without it the
# Pods get stuck in CreateContainerConfigError, so check for it up front.
if ! kubectl get secret doc-center-secrets >/dev/null 2>&1; then
  echo "Secret 'doc-center-secrets' not found."
  echo "Create it first (values go from your terminal straight into the cluster,"
  echo "never touching a file):"
  echo
  cat <<'HINT'
  MYSQL_PW='<strong password>'
  kubectl create secret generic doc-center-secrets \
    --from-literal=mysql-root-password="<strong password>" \
    --from-literal=mysql-password="$MYSQL_PW" \
    --from-literal=secret-key="$(python3 -c 'import secrets;print(secrets.token_urlsafe(48))')" \
    --from-literal=google-client-secret="<regenerated in Google Cloud Console>" \
    --from-literal=database-url="mysql+pymysql://flaskuser:${MYSQL_PW}@mysql/cloud_docs"
HINT
  echo
  echo "See k8s/secret.example.yaml for details."
  exit 1
fi

# --- ConfigMaps (monitoring configuration) ---------------------------
kubectl create configmap prometheus-config  --from-file=../prometheus/prometheus.yml
kubectl create configmap grafana-datasource --from-file=../grafana/datasource.yaml
kubectl create configmap grafana-dashboard  --from-file=../grafana/dashboard.yaml --from-file=../grafana/dashboards/

kubectl apply -f deploys.yaml
kubectl apply -f services.yaml
kubectl apply -f ingress.yaml

kubectl get pods -w
