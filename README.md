# ICS to Google Calendar Sync

A web-based service that syncs ICS calendar feeds to Google Calendar with automatic updates every 15 minutes (configurable).

## Features

- 🌐 **Web UI** - Easy-to-use interface for configuration and monitoring
- 📊 **Live Logs** - Real-time log viewer with filtering and auto-refresh
- 🔄 **Automatic Sync** - Configurable sync interval (default: 15 minutes)
- 🐳 **Docker Ready** - Containerized for easy deployment
- ☸️ **Kubernetes Native** - Includes manifests for K8s deployment
- 🔐 **Service Account Support** - Works with Google service accounts for production

## Quick Start

### Local Development

1. **Install dependencies:**
   ```bash
   pip install -r requirements.txt
   ```

2. **Set up Google Calendar API:**
   - Go to [Google Cloud Console](https://console.cloud.google.com/)
   - Create a new project
   - Enable Google Calendar API
   - Create OAuth 2.0 credentials and download `credentials.json`
   - Place `credentials.json` in `/app/secrets/` (or update paths in the code)

3. **Create required directories:**
   ```bash
   mkdir -p /app/data /app/secrets
   ```

4. **Run the application:**
   ```bash
   python web_app.py
   ```

5. **Access the web UI:**
   Open http://localhost:8080 in your browser

### Docker

1. **Build the image:**
   ```bash
   docker build -t calendar-sync:latest .
   ```

2. **Run the container:**
   ```bash
   docker run -d \
     -p 8080:8080 \
     -v $(pwd)/credentials.json:/app/secrets/credentials.json:ro \
     -v calendar-sync-data:/app/data \
     --name calendar-sync \
     calendar-sync:latest
   ```

### Kubernetes

1. **Update the Kubernetes manifest:**
   - Edit `kubernetes-deployment.yaml`
   - Base64 encode your credentials: `cat credentials.json | base64`
   - Replace `<BASE64_ENCODED_CREDENTIALS>` with your encoded credentials
   - Update the ingress hostname
   - Update the Docker image name

2. **Deploy to Kubernetes:**
   ```bash
   kubectl apply -f kubernetes-deployment.yaml
   ```

3. **Check deployment status:**
   ```bash
   kubectl get pods -n calendar-sync
   kubectl logs -n calendar-sync deployment/calendar-sync
   ```

## Configuration

### Web UI Configuration

1. Navigate to the **Configuration** page
2. Enter your ICS calendar URL
3. Enter your Google Calendar ID (use `primary` for your main calendar)
4. Set the sync interval in seconds (900 = 15 minutes)
5. Click **Save Configuration**

### Google Calendar Setup

#### For Local Development (OAuth)

1. Create OAuth 2.0 Client ID in Google Cloud Console
2. Download `credentials.json`
3. First run will open a browser for authentication
4. Token is saved for subsequent runs

#### For Production/Kubernetes (Service Account)

1. Create a Service Account in Google Cloud Console
2. Download the JSON key file
3. Share your target Google Calendar with the service account email
4. Give "Make changes to events" permission
5. Mount the JSON file as `/app/secrets/credentials.json`

### Finding Your ICS URL

- **Google Calendar:** Settings → Integrate Calendar → Secret address in iCalendar format
- **Outlook/Office 365:** Calendar Settings → Shared Calendars → Publish → ICS link
- **Apple Calendar:** Calendar → Publish Calendar
- **Other services:** Look for "Export as ICS" or "Calendar subscription URL"

## Log Levels

The system uses the following log levels:

- **INFO** - General information
- **SUCCESS** - Successful operations
- **ADD** - New events added
- **UPDATE** - Existing events updated
- **WARNING** - Non-critical issues
- **ERROR** - Critical errors

## API Endpoints

- `GET /` - Dashboard
- `GET /config` - Configuration page
- `GET /logs` - Log viewer
- `GET /api/logs?limit=100` - Get logs (JSON)
- `GET /api/config` - Get current configuration (JSON)
- `POST /api/config` - Update configuration (JSON)
- `POST /api/sync/trigger` - Manually trigger sync
- `GET /health` - Health check endpoint

## File Structure

```
.
├── sync_service.py           # Core sync logic
├── web_app.py                # Flask web application
├── templates/                # HTML templates
│   ├── base.html
│   ├── index.html
│   ├── config.html
│   └── logs.html
├── requirements.txt          # Python dependencies
├── Dockerfile               # Docker container definition
├── kubernetes-deployment.yaml # Kubernetes manifests
└── README.md                # This file
```

## Persistent Data

The application stores data in `/app/data/`:
- `config.json` - Configuration settings
- `token.pickle` - OAuth authentication token
- `sync_logs.json` - Persistent log file

## Troubleshooting

### Authentication Issues

- Ensure credentials.json is properly mounted
- For service accounts, verify calendar sharing permissions
- Check logs for authentication errors

### Sync Not Working

- Verify ICS URL is accessible
- Check calendar ID is correct
- Review logs for specific errors
- Try manual sync from dashboard

### Kubernetes Issues

- Check pod logs: `kubectl logs -n calendar-sync deployment/calendar-sync`
- Verify secrets are mounted: `kubectl describe pod -n calendar-sync`
- Check persistent volume claims: `kubectl get pvc -n calendar-sync`

## Security Notes

- Keep `credentials.json` secure - never commit to version control
- Use Kubernetes secrets for production deployments
- Change the `SECRET_KEY` environment variable in production
- Consider using cert-manager for TLS in Kubernetes

## License

MIT License - Feel free to use and modify as needed.
