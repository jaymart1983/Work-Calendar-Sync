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

def is_event_in_date_range(event, start_date, end_date):
    """Check if event falls within the given date range."""
    dtstart = event.get('dtstart')
    if not dtstart:
        return False
    
    event_start = dtstart.dt
    # Handle both date and datetime objects
    if isinstance(event_start, datetime):
        event_date = event_start.date()
    else:
        event_date = event_start
    
    return start_date <= event_date <= end_date

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

def sync_calendar(ics_url, calendar_id, quick_sync=True):
    """Perform calendar sync.
    
    Args:
        ics_url: URL to ICS calendar
        calendar_id: Google Calendar ID
        quick_sync: If True, only sync events within next 7 days. If False, sync all events.
    """
    from datetime import date, timedelta
    
    sync_type = 'Quick sync (7 days)' if quick_sync else 'Full sync (all events)'
    log_event('INFO', f'Starting {sync_type}', {'ics_url': ics_url, 'calendar_id': calendar_id})
    
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
        
        # Set date range for quick sync
        if quick_sync:
            today = date.today()
            end_date = today + timedelta(days=7)
            log_event('INFO', f'Quick sync: filtering events from {today} to {end_date}')
        
        # Process events
        added = 0
        updated = 0
        no_change = 0
        errors = 0
        skipped = 0
        
        for component in ics_cal.walk():
            if component.name == "VEVENT":
                # Skip events outside date range in quick sync mode
                if quick_sync and not is_event_in_date_range(component, today, end_date):
                    skipped += 1
                    continue
                try:
                    gcal_event = convert_ics_event_to_gcal(component)
                    ical_uid = gcal_event.get('iCalUID')
                    
                    if ical_uid and ical_uid in existing_events:
                        # Get existing event to compare
                        existing_event = service.events().get(
                            calendarId=calendar_id,
                            eventId=existing_events[ical_uid]
                        ).execute()
                        
                        # Check if event actually changed (compare key fields)
                        has_changes = (
                            existing_event.get('summary') != gcal_event.get('summary') or
                            existing_event.get('description') != gcal_event.get('description') or
                            existing_event.get('location') != gcal_event.get('location') or
                            existing_event.get('start') != gcal_event.get('start') or
                            existing_event.get('end') != gcal_event.get('end')
                        )
                        
                        event_date = gcal_event.get('start', {}).get('date') or gcal_event.get('start', {}).get('dateTime', '')
                        event_date_str = event_date.split('T')[0] if event_date else 'Unknown date'
                        
                        if has_changes:
                            # Update the event
                            service.events().update(
                                calendarId=calendar_id,
                                eventId=existing_events[ical_uid],
                                body=gcal_event
                            ).execute()
                            updated += 1
                            log_event('UPDATE', f'Updated: {gcal_event["summary"]} ({event_date_str})')
                            # Rate limit: 1 request per second to avoid API quota
                            sleep(1.1)
                        else:
                            # No changes needed
                            no_change += 1
                    else:
                        service.events().insert(
                            calendarId=calendar_id,
                            body=gcal_event
                        ).execute()
                        added += 1
                        # Get event date for logging
                        event_date = gcal_event.get('start', {}).get('date') or gcal_event.get('start', {}).get('dateTime', '')
                        event_date_str = event_date.split('T')[0] if event_date else 'Unknown date'
                        log_event('ADD', f'Added: {gcal_event["summary"]} ({event_date_str})')
                        # Rate limit: 1 request per second to avoid API quota
                        sleep(1.1)
                
                except Exception as e:
                    errors += 1
                    log_event('ERROR', f'Failed to process event: {e}')
                    # Back off on errors to avoid hammering the API
                    sleep(2)
        
        log_message = f'Sync completed: {added} added, {updated} updated, {no_change} no change, {errors} errors'
        if quick_sync:
            log_message += f', {skipped} skipped (outside 7-day window)'
        log_event('SUCCESS', log_message, {
            'added': added,
            'updated': updated,
            'no_change': no_change,
            'errors': errors,
            'skipped': skipped if quick_sync else 0
        })
        
        return {'added': added, 'updated': updated, 'errors': errors}
    
    except Exception as e:
        log_event('ERROR', f'Sync failed: {e}')
        raise

def sync_loop():
    """Main sync loop."""
    from datetime import datetime as dt
    
    log_event('INFO', 'Sync service started with smart scheduling')
    log_event('INFO', 'Quick sync (7 days) runs every interval, Full sync at midnight')
    
    last_full_sync_day = None
    
    while True:
        try:
            config = load_config()
            
            if not config.get('ics_url'):
                log_event('WARNING', 'No ICS URL configured, waiting...')
                time.sleep(60)
                continue
            
            # Determine if we should do a full sync (once per day at midnight-ish)
            current_time = dt.now()
            current_day = current_time.date()
            
            # Do full sync if it's a new day and we haven't done one yet today
            # and it's past midnight (between 00:00 and 01:00)
            should_full_sync = (
                last_full_sync_day != current_day and
                0 <= current_time.hour < 1
            )
            
            if should_full_sync:
                log_event('INFO', 'Performing daily full sync')
                sync_calendar(config['ics_url'], config.get('calendar_id', 'primary'), quick_sync=False)
                last_full_sync_day = current_day
            else:
                # Quick sync - only next 7 days
                sync_calendar(config['ics_url'], config.get('calendar_id', 'primary'), quick_sync=True)
            
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
