# Kubernetes Deployment Guide

## Prerequisites

- Kubernetes cluster access
- `kubectl` configured
- GitHub account
- Google Cloud service account credentials

## Step 1: Push to GitHub

```bash
# Add all files
git add .

# Commit
git commit -m "Initial commit: ICS to Google Calendar sync service"

# Create GitHub repository and push
# (Create repo on GitHub first, then run these commands)
git remote add origin https://github.com/YOUR_USERNAME/Work-Calendar-Sync.git
git branch -M main
git push -u origin main
```

## Step 2: GitHub Container Registry Setup

The GitHub Actions workflow will automatically build and push Docker images to GitHub Container Registry (GHCR) when you push to the main branch.

1. The workflow uses `GITHUB_TOKEN` which is automatically available
2. Images will be pushed to: `ghcr.io/YOUR_USERNAME/work-calendar-sync:latest`
3. Each push creates a new image with tags for:
   - `latest` (for main branch)
   - Commit SHA
   - Version tags (if you tag releases)

## Step 3: Prepare Google Service Account

For Kubernetes deployment, use a service account instead of OAuth:

1. Create service account in Google Cloud Console
2. Download the JSON key file
3. Save it as `secrets/credentials.json` in your project directory
4. **DO NOT** commit this file to git (it's in .gitignore)

## Step 4: Update Kubernetes Manifest

Edit `kubernetes-deployment-final.yaml`:

1. Update the ingress hostname to your domain
2. Change `SECRET_KEY` to a random string
3. Review resource limits based on your cluster

## Step 5: Deploy to Kubernetes

```bash
# Apply the deployment
kubectl apply -f kubernetes-deployment-final.yaml

# Create the secret from your credentials file
./create-secret.sh

# Check deployment status
kubectl get pods -n calendar-sync

# View logs
kubectl logs -n calendar-sync deployment/calendar-sync -f

# Check service
kubectl get svc -n calendar-sync

# Check ingress
kubectl get ingress -n calendar-sync
```

## Step 6: Configure the Application

1. Access the web UI via your ingress hostname
2. Navigate to Configuration page
3. Enter your ICS calendar URL
4. Enter Google Calendar ID (or 'primary')
5. Set sync interval (default: 900 seconds / 15 minutes)
6. Save configuration

## Updating the Application

To update the application:

```bash
# Make your code changes locally
git add .
git commit -m "Description of changes"
git push

# Wait for GitHub Actions to build the image (check Actions tab)

# Force Kubernetes to pull the new image
kubectl rollout restart deployment/calendar-sync -n calendar-sync

# Watch the rollout
kubectl rollout status deployment/calendar-sync -n calendar-sync
```

## Alternative: Manual Image Build

If you prefer to build and push images manually:

```bash
# Build the image
docker build -t ghcr.io/YOUR_USERNAME/work-calendar-sync:latest .

# Login to GHCR
echo $GITHUB_TOKEN | docker login ghcr.io -u YOUR_USERNAME --password-stdin

# Push the image
docker push ghcr.io/YOUR_USERNAME/work-calendar-sync:latest
```

## Troubleshooting

### View Logs
```bash
kubectl logs -n calendar-sync deployment/calendar-sync --tail=100
```

### Check Pod Status
```bash
kubectl describe pod -n calendar-sync -l app=calendar-sync
```

### Exec into Pod
```bash
kubectl exec -it -n calendar-sync deployment/calendar-sync -- /bin/sh
```

### Delete and Redeploy
```bash
kubectl delete -f kubernetes-deployment-updated.yaml
kubectl apply -f kubernetes-deployment-updated.yaml
```

## Service Account Permissions

Don't forget to:
1. Share your Google Calendar with the service account email
2. Give it "Make changes to events" permission

## Security Notes

- Keep `credentials.json` secure
- Store base64 credentials in Kubernetes secrets only
- Use TLS/HTTPS in production (uncomment TLS section in ingress)
- Change the SECRET_KEY to a random value
- Consider using sealed secrets or external secret managers for production
