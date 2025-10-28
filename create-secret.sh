#!/bin/bash
# Script to create Kubernetes secret for Google Calendar credentials
# This should be run ONCE after deploying the application

set -e

NAMESPACE="calendar-sync"
SECRET_NAME="google-credentials"
CREDENTIALS_FILE="./secrets/credentials.json"

echo "Creating Kubernetes secret for Google Calendar credentials..."

# Check if credentials file exists
if [ ! -f "$CREDENTIALS_FILE" ]; then
    echo "Error: Credentials file not found at $CREDENTIALS_FILE"
    echo "Please place your credentials.json file in the secrets/ directory"
    exit 1
fi

# Check if namespace exists
if ! kubectl get namespace "$NAMESPACE" > /dev/null 2>&1; then
    echo "Error: Namespace $NAMESPACE does not exist"
    echo "Please deploy the application first: kubectl apply -f kubernetes-deployment-final.yaml"
    exit 1
fi

# Delete existing secret if it exists
if kubectl get secret "$SECRET_NAME" -n "$NAMESPACE" > /dev/null 2>&1; then
    echo "Deleting existing secret..."
    kubectl delete secret "$SECRET_NAME" -n "$NAMESPACE"
fi

# Create the secret
echo "Creating secret from $CREDENTIALS_FILE..."
kubectl create secret generic "$SECRET_NAME" \
    --from-file=credentials.json="$CREDENTIALS_FILE" \
    -n "$NAMESPACE"

echo "✓ Secret created successfully!"
echo ""
echo "Now restart the deployment to pick up the new secret:"
echo "  kubectl rollout restart deployment/calendar-sync -n $NAMESPACE"
