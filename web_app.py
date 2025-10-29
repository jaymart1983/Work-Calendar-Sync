#!/usr/bin/env python3
"""
Web UI for ICS to Google Calendar Sync Service
"""

from flask import Flask, render_template, request, jsonify, redirect, url_for
from threading import Thread
import sync_service
import os

app = Flask(__name__)
app.config['SECRET_KEY'] = os.environ.get('SECRET_KEY', 'dev-secret-key-change-in-production')

# Start sync service in background thread
sync_thread = None

def start_sync_service():
    """Start the sync service in a background thread."""
    global sync_thread
    if sync_thread is None or not sync_thread.is_alive():
        sync_thread = Thread(target=sync_service.sync_loop, daemon=True)
        sync_thread.start()

@app.route('/')
def index():
    """Main dashboard page."""
    config = sync_service.load_config()
    return render_template('index.html', config=config)

@app.route('/config', methods=['GET', 'POST'])
def config():
    """Configuration page."""
    if request.method == 'POST':
        config_data = {
            'ics_url': request.form.get('ics_url', ''),
            'calendar_id': request.form.get('calendar_id', 'primary'),
            'sync_interval': int(request.form.get('sync_interval', 900)),
            'full_sync_hour': int(request.form.get('full_sync_hour', 0)),
            'full_sync_timezone': request.form.get('full_sync_timezone', 'UTC')
        }
        # Preserve existing timezone detection fields
        existing_config = sync_service.load_config()
        for key in ['ics_timezone', 'ics_offset', 'gcal_timezone', 'gcal_offset']:
            if key in existing_config:
                config_data[key] = existing_config[key]
        
        sync_service.save_config(config_data)
        return redirect(url_for('index'))
    
    current_config = sync_service.load_config()
    return render_template('config.html', config=current_config)

@app.route('/logs')
def logs():
    """Logs viewer page."""
    return render_template('logs.html')

@app.route('/api/logs')
def api_logs():
    """API endpoint to get logs."""
    limit = request.args.get('limit', 100, type=int)
    logs = sync_service.get_logs(limit)
    return jsonify(logs)

@app.route('/api/config', methods=['GET', 'POST'])
def api_config():
    """API endpoint for configuration."""
    if request.method == 'POST':
        config_data = request.json
        sync_service.save_config(config_data)
        return jsonify({'status': 'success'})
    
    return jsonify(sync_service.load_config())

@app.route('/api/sync/trigger', methods=['POST'])
def api_trigger_sync():
    """Manually trigger a sync."""
    try:
        config = sync_service.load_config()
        if not config.get('ics_url'):
            return jsonify({'status': 'error', 'message': 'No ICS URL configured'}), 400
        
        # Get quick_sync parameter from query string (default: False for full sync)
        quick_sync = request.args.get('quick_sync', 'false').lower() == 'true'
        
        result = sync_service.sync_calendar(
            config['ics_url'],
            config.get('calendar_id', 'primary'),
            quick_sync=quick_sync
        )
        return jsonify({'status': 'success', 'result': result})
    except Exception as e:
        return jsonify({'status': 'error', 'message': str(e)}), 500

@app.route('/api/sync/schedule', methods=['GET'])
def api_sync_schedule():
    """Get next sync times."""
    import pytz
    from datetime import datetime as dt, timedelta
    
    config = sync_service.load_config()
    sync_interval = config.get('sync_interval', 900)
    full_sync_hour = config.get('full_sync_hour', 0)
    full_sync_tz = config.get('full_sync_timezone', 'UTC')
    
    # Calculate next quick sync
    logs = sync_service.get_logs(limit=200)
    
    # Find the most recent "Next sync in" or sync completion message
    last_sync_end_time = None
    service_start_time = None
    
    for log in reversed(logs):
        msg = log.get('message', '')
        timestamp = dt.fromisoformat(log['timestamp'])
        
        # Service start is a good indicator
        if 'Sync service started' in msg:
            service_start_time = timestamp
        
        # Next sync message is most accurate
        if 'Next sync in' in msg and last_sync_end_time is None:
            last_sync_end_time = timestamp
            break
    
    # Calculate next sync
    if last_sync_end_time:
        next_quick_sync = last_sync_end_time + timedelta(seconds=sync_interval)
    elif service_start_time:
        # Service just started, assume sync happens soon
        next_quick_sync = service_start_time + timedelta(seconds=min(sync_interval, 60))
    else:
        # No info available, estimate
        next_quick_sync = dt.now() + timedelta(seconds=sync_interval)
    
    # If calculated time is in the past, reset to now + small delay
    if next_quick_sync < dt.now():
        next_quick_sync = dt.now() + timedelta(seconds=5)
    
    # Calculate next full sync
    try:
        tz = pytz.timezone(full_sync_tz)
    except Exception:
        tz = pytz.UTC
    
    now = dt.now(tz)
    today_full_sync = now.replace(hour=full_sync_hour, minute=0, second=0, microsecond=0)
    
    if now >= today_full_sync:
        # Already passed today, next is tomorrow
        next_full_sync = today_full_sync + timedelta(days=1)
    else:
        next_full_sync = today_full_sync
    
    return jsonify({
        'next_quick_sync': next_quick_sync.isoformat(),
        'next_full_sync': next_full_sync.isoformat(),
        'sync_interval': sync_interval
    })

@app.route('/health')
def health():
    """Health check endpoint for Kubernetes."""
    return jsonify({'status': 'healthy'})

if __name__ == '__main__':
    # Create data directory if it doesn't exist
    base_dir = os.environ.get('APP_BASE_DIR', '/app')
    data_dir = os.path.join(base_dir, 'data')
    os.makedirs(data_dir, exist_ok=True)
    
    # Start sync service
    start_sync_service()
    
    # Start web server
    app.run(host='0.0.0.0', port=8080, debug=False)
