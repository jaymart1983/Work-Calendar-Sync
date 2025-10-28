#!/usr/bin/env python3
"""
ICS to Google Calendar Sync Service
Background service that syncs calendars based on configuration
"""

import json
import os
import time
import sys
from datetime import datetime, timezone
from threading import Thread, Lock
import requests
from time import sleep
from icalendar import Calendar
from google.oauth2.credentials import Credentials
from google.oauth2.service_account import Credentials as ServiceAccountCredentials
from google_auth_oauthlib.flow import InstalledAppFlow
from google.auth.transport.requests import Request
from googleapiclient.discovery import build
import pickle

import os

SCOPES = ['https://www.googleapis.com/auth/calendar']

# Use environment variable or default to /app for production
BASE_DIR = os.environ.get('APP_BASE_DIR', '/app')
DATA_DIR = os.path.join(BASE_DIR, 'data')
SECRETS_DIR = os.path.join(BASE_DIR, 'secrets')

CONFIG_FILE = os.path.join(DATA_DIR, 'config.json')
TOKEN_FILE = os.path.join(DATA_DIR, 'token.pickle')
LOG_FILE = os.path.join(DATA_DIR, 'sync_logs.json')
CREDENTIALS_FILE = os.path.join(SECRETS_DIR, 'credentials.json')

# In-memory log buffer (last 1000 entries)
log_buffer = []
log_lock = Lock()

def log_event(level, message, details=None):
    """Add a log entry with timestamp."""
    entry = {
        'timestamp': datetime.now().isoformat(),
        'level': level,
        'message': message,
    }
    if details:
        entry['details'] = details
    
    with log_lock:
        log_buffer.append(entry)
        if len(log_buffer) > 1000:
            log_buffer.pop(0)
    
    # Also print to console
    print(f"[{entry['timestamp']}] {level}: {message}")
    
    # Persist to file
    try:
        with open(LOG_FILE, 'a') as f:
            f.write(json.dumps(entry) + '\n')
    except Exception as e:
        print(f"Failed to write log: {e}")

def get_logs(limit=100):
    """Get recent logs."""
    with log_lock:
        return log_buffer[-limit:]

def load_config():
    """Load configuration from file."""
    if not os.path.exists(CONFIG_FILE):
        return {
            'ics_url': '',
            'calendar_id': 'primary',
            'sync_interval': 900
        }
    
    try:
        with open(CONFIG_FILE, 'r') as f:
            return json.load(f)
    except Exception as e:
        log_event('ERROR', f'Failed to load config: {e}')
        return {}

def save_config(config):
    """Save configuration to file."""
    os.makedirs(os.path.dirname(CONFIG_FILE), exist_ok=True)
    with open(CONFIG_FILE, 'w') as f:
        json.dump(config, f, indent=2)
    log_event('INFO', 'Configuration updated')

def get_google_calendar_service():
    """Authenticate and return Google Calendar service."""
    # Try service account first (recommended for Kubernetes)
    if os.path.exists(CREDENTIALS_FILE):
        try:
            with open(CREDENTIALS_FILE, 'r') as f:
                cred_data = json.load(f)
                if cred_data.get('type') == 'service_account':
                    log_event('INFO', 'Using service account credentials')
                    creds = ServiceAccountCredentials.from_service_account_file(
                        CREDENTIALS_FILE, scopes=SCOPES)
                    return build('calendar', 'v3', credentials=creds)
        except Exception as e:
            log_event('WARNING', f'Service account auth failed: {e}')
    
    # Fall back to OAuth flow
    creds = None
    
    if os.path.exists(TOKEN_FILE):
        with open(TOKEN_FILE, 'rb') as token:
            creds = pickle.load(token)
    
    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            creds.refresh(Request())
        else:
            if not os.path.exists(CREDENTIALS_FILE):
                raise Exception('No credentials file found')
            flow = InstalledAppFlow.from_client_secrets_file(
                CREDENTIALS_FILE, SCOPES)
            creds = flow.run_local_server(port=8095)
        
        os.makedirs(os.path.dirname(TOKEN_FILE), exist_ok=True)
        with open(TOKEN_FILE, 'wb') as token:
            pickle.dump(creds, token)
    
    return build('calendar', 'v3', credentials=creds)

def fetch_ics_calendar(ics_url):
    """Fetch and parse ICS calendar from URL."""
    response = requests.get(ics_url, timeout=30)
    response.raise_for_status()
    return Calendar.from_ical(response.content)

def convert_ics_event_to_gcal(event):
    """Convert ICS event to Google Calendar event format."""
    gcal_event = {
        'summary': str(event.get('summary', 'No Title')),
        'description': str(event.get('description', '')),
        'location': str(event.get('location', '')),
    }
    
    # Handle start time
    dtstart = event.get('dtstart')
    if dtstart:
        start_dt = dtstart.dt
        if isinstance(start_dt, datetime):
            gcal_event['start'] = {
                'dateTime': start_dt.isoformat(),
                'timeZone': 'UTC' if start_dt.tzinfo else None
            }
        else:
            gcal_event['start'] = {'date': start_dt.isoformat()}
    
    # Handle end time
    dtend = event.get('dtend')
    if dtend:
        end_dt = dtend.dt
        if isinstance(end_dt, datetime):
            gcal_event['end'] = {
                'dateTime': end_dt.isoformat(),
                'timeZone': 'UTC' if end_dt.tzinfo else None
            }
        else:
            gcal_event['end'] = {'date': end_dt.isoformat()}
    
    if event.get('uid'):
        gcal_event['iCalUID'] = str(event.get('uid'))
    
    return gcal_event

def sync_calendar(ics_url, calendar_id):
    """Perform calendar sync."""
    log_event('INFO', 'Starting sync', {'ics_url': ics_url, 'calendar_id': calendar_id})
    
    try:
        # Fetch ICS
        ics_cal = fetch_ics_calendar(ics_url)
        log_event('SUCCESS', 'ICS calendar fetched successfully')
        
        # Authenticate
        service = get_google_calendar_service()
        log_event('SUCCESS', 'Google Calendar authenticated')
        
        # Get existing events
        existing_events = {}
        page_token = None
        while True:
            events_result = service.events().list(
                calendarId=calendar_id,
                pageToken=page_token,
                maxResults=2500
            ).execute()
            
            for event in events_result.get('items', []):
                if 'iCalUID' in event:
                    existing_events[event['iCalUID']] = event['id']
            
            page_token = events_result.get('nextPageToken')
            if not page_token:
                break
        
        log_event('INFO', f'Found {len(existing_events)} existing events')
        
        # Process events
        added = 0
        updated = 0
        errors = 0
        
        for component in ics_cal.walk():
            if component.name == "VEVENT":
                try:
                    gcal_event = convert_ics_event_to_gcal(component)
                    ical_uid = gcal_event.get('iCalUID')
                    
                    if ical_uid and ical_uid in existing_events:
                        service.events().update(
                            calendarId=calendar_id,
                            eventId=existing_events[ical_uid],
                            body=gcal_event
                        ).execute()
                        updated += 1
                        log_event('UPDATE', f'Updated event: {gcal_event["summary"]}')
                        # Rate limit: 1 request per second to avoid API quota
                        sleep(1.1)
                    else:
                        service.events().insert(
                            calendarId=calendar_id,
                            body=gcal_event
                        ).execute()
                        added += 1
                        log_event('ADD', f'Added event: {gcal_event["summary"]}')
                        # Rate limit: 1 request per second to avoid API quota
                        sleep(1.1)
                
                except Exception as e:
                    errors += 1
                    log_event('ERROR', f'Failed to process event: {e}')
                    # Back off on errors to avoid hammering the API
                    sleep(2)
        
        log_event('SUCCESS', 'Sync completed', {
            'added': added,
            'updated': updated,
            'errors': errors
        })
        
        return {'added': added, 'updated': updated, 'errors': errors}
    
    except Exception as e:
        log_event('ERROR', f'Sync failed: {e}')
        raise

def sync_loop():
    """Main sync loop."""
    log_event('INFO', 'Sync service started')
    
    while True:
        try:
            config = load_config()
            
            if not config.get('ics_url'):
                log_event('WARNING', 'No ICS URL configured, waiting...')
                time.sleep(60)
                continue
            
            sync_calendar(config['ics_url'], config.get('calendar_id', 'primary'))
            
            interval = config.get('sync_interval', 900)
            log_event('INFO', f'Next sync in {interval} seconds')
            time.sleep(interval)
        
        except KeyboardInterrupt:
            log_event('INFO', 'Shutting down')
            sys.exit(0)
        
        except Exception as e:
            log_event('ERROR', f'Sync loop error: {e}')
            time.sleep(60)

if __name__ == '__main__':
    sync_loop()
