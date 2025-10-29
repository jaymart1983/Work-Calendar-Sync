#!/usr/bin/env python3
"""
ICS to Google Calendar Sync Service - Refactored with recurring event support
Background service that syncs calendars based on configuration
"""

import json
import os
import time
import sys
from datetime import datetime, timezone, date, timedelta
from threading import Thread, Lock
import requests
from time import sleep
from icalendar import Calendar
import recurring_ical_events
from dateutil import parser as dt_parser
from google.oauth2.credentials import Credentials
from google.oauth2.service_account import Credentials as ServiceAccountCredentials
from google_auth_oauthlib.flow import InstalledAppFlow
from google.auth.transport.requests import Request
from googleapiclient.discovery import build
import pickle

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

# Sync lock to prevent concurrent syncs
sync_lock = Lock()
sync_in_progress = False

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
            'sync_interval': 60,
            'full_sync_hour': 0,
            'full_sync_timezone': 'UTC'
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

def normalize_start_time_to_utc(start_dict):
    """Normalize a start time dict to UTC string for consistent comparison."""
    if 'date' in start_dict:
        return start_dict['date']
    elif 'dateTime' in start_dict:
        try:
            dt = dt_parser.isoparse(start_dict['dateTime'])
            if dt.tzinfo:
                dt_utc = dt.astimezone(timezone.utc)
            else:
                dt_utc = dt.replace(tzinfo=timezone.utc)
            return dt_utc.strftime('%Y-%m-%dT%H:%M:%S')
        except Exception:
            return start_dict['dateTime']
    return ''

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
            start_dict = {'dateTime': start_dt.isoformat()}
            if start_dt.tzinfo and hasattr(start_dt.tzinfo, 'zone'):
                start_dict['timeZone'] = start_dt.tzinfo.zone
            gcal_event['start'] = start_dict
        else:
            gcal_event['start'] = {'date': start_dt.isoformat()}
    
    # Handle end time
    dtend = event.get('dtend')
    if dtend:
        end_dt = dtend.dt
        if isinstance(end_dt, datetime):
            end_dict = {'dateTime': end_dt.isoformat()}
            if end_dt.tzinfo and hasattr(end_dt.tzinfo, 'zone'):
                end_dict['timeZone'] = end_dt.tzinfo.zone
            gcal_event['end'] = end_dict
        else:
            gcal_event['end'] = {'date': end_dt.isoformat()}
    
    if event.get('uid'):
        gcal_event['iCalUID'] = str(event.get('uid'))
    
    return gcal_event

def sync_calendar(ics_url, calendar_id, quick_sync=True):
    """Perform calendar sync with locking to prevent concurrent syncs."""
    global sync_in_progress
    
    with sync_lock:
        if sync_in_progress:
            log_event('WARNING', 'Sync already in progress, skipping this run')
            return {'added': 0, 'updated': 0, 'deleted': 0, 'errors': 0, 'skipped_due_to_lock': True}
        sync_in_progress = True
    
    try:
        return _do_sync(ics_url, calendar_id, quick_sync)
    finally:
        with sync_lock:
            sync_in_progress = False

def _do_sync(ics_url, calendar_id, quick_sync):
    """Internal sync implementation with recurring event support."""
    sync_type = 'Quick sync (7 days)' if quick_sync else 'Full sync (all events)'
    log_event('INFO', f'Starting {sync_type}', {'ics_url': ics_url, 'calendar_id': calendar_id})
    
    try:
        # Fetch ICS
        ics_cal = fetch_ics_calendar(ics_url)
        log_event('SUCCESS', 'ICS calendar fetched successfully')
        
        # Authenticate
        service = get_google_calendar_service()
        log_event('SUCCESS', 'Google Calendar authenticated')
        
        # Define date range
        if quick_sync:
            today = date.today()
            start_date = datetime.combine(today, datetime.min.time()).replace(tzinfo=timezone.utc)
            end_date = datetime.combine(today + timedelta(days=7), datetime.max.time()).replace(tzinfo=timezone.utc)
            log_event('INFO', f'Quick sync: filtering events from {today} to {today + timedelta(days=7)}')
        else:
            # For full sync, use a wide range (past 30 days to future 365 days)
            today = date.today()
            start_date = datetime.combine(today - timedelta(days=30), datetime.min.time()).replace(tzinfo=timezone.utc)
            end_date = datetime.combine(today + timedelta(days=365), datetime.max.time()).replace(tzinfo=timezone.utc)
            log_event('INFO', 'Full sync: processing all events in range')
        
        # Expand recurring events from ICS using recurring-ical-events
        ics_events = recurring_ical_events.of(ics_cal).between(start_date, end_date)
        log_event('INFO', f'Found {len(list(ics_events))} ICS event instances (after expansion)')
        
        # Re-get events (generator is consumed)
        ics_events = list(recurring_ical_events.of(ics_cal).between(start_date, end_date))
        
        # Get existing Google Calendar events
        existing_events = {}  # key: (iCalUID, UTC_start_time), value: event_id
        page_token = None
        while True:
            events_result = service.events().list(
                calendarId=calendar_id,
                pageToken=page_token,
                maxResults=2500,
                singleEvents=True,
                showDeleted=True,
                timeMin=start_date.isoformat(),
                timeMax=end_date.isoformat()
            ).execute()
            
            for event in events_result.get('items', []):
                if 'iCalUID' in event:
                    ical_uid = event['iCalUID']
                    start = event.get('start', {})
                    start_key = normalize_start_time_to_utc(start)
                    key = (ical_uid, start_key)
                    # Only track confirmed events; cancelled/deleted events should be recreated
                    if event.get('status') == 'confirmed':
                        existing_events[key] = event['id']
                    else:
                        log_event('DEBUG', f'Skipping cancelled/deleted event: {event.get("summary")} at {start_key}')
            
            page_token = events_result.get('nextPageToken')
            if not page_token:
                break
        
        log_event('INFO', f'Found {len(existing_events)} existing Google Calendar event instances')
        
        # Track ICS events
        ics_event_keys = set()
        added = 0
        updated = 0
        no_change = 0
        errors = 0
        
        # Process each expanded ICS event
        for component in ics_events:
            try:
                gcal_event = convert_ics_event_to_gcal(component)
                ical_uid = gcal_event.get('iCalUID')
                start = gcal_event.get('start', {})
                start_key = normalize_start_time_to_utc(start)
                event_key = (ical_uid, start_key)
                ics_event_keys.add(event_key)
                
                event_summary = gcal_event.get('summary', 'No Title')
                log_event('DEBUG', f'Processing: {event_summary} at {start_key}, key in existing: {event_key in existing_events}')
                
                if event_key in existing_events:
                    # Event exists - check if update needed
                    existing_event = service.events().get(
                        calendarId=calendar_id,
                        eventId=existing_events[event_key]
                    ).execute()
                    
                    # Simple comparison - update if status is cancelled or summary changed
                    needs_update = (
                        existing_event.get('status') != 'confirmed' or
                        existing_event.get('summary') != gcal_event.get('summary')
                    )
                    
                    if needs_update:
                        gcal_event['status'] = 'confirmed'
                        service.events().update(
                            calendarId=calendar_id,
                            eventId=existing_events[event_key],
                            body=gcal_event
                        ).execute()
                        updated += 1
                        log_event('UPDATE', f'Updated: {gcal_event["summary"]}')
                        sleep(0.5)
                    else:
                        no_change += 1
                else:
                    # New event - add it
                    service.events().insert(
                        calendarId=calendar_id,
                        body=gcal_event
                    ).execute()
                    added += 1
                    log_event('ADD', f'Added: {gcal_event["summary"]} at {start_key}')
                    sleep(0.5)
                    
            except Exception as e:
                if '409' in str(e) or 'already exists' in str(e).lower():
                    # Event already exists (race condition), count as no_change
                    no_change += 1
                else:
                    errors += 1
                    log_event('ERROR', f'Failed to process event: {str(e)}')
                sleep(0.5)
        
        # Delete events in Google Calendar that aren't in ICS
        deleted = 0
        for event_key, gcal_event_id in existing_events.items():
            if event_key not in ics_event_keys:
                try:
                    service.events().delete(
                        calendarId=calendar_id,
                        eventId=gcal_event_id
                    ).execute()
                    deleted += 1
                    log_event('DELETE', f'Deleted event: {event_key[0]}')
                    sleep(0.5)
                except Exception as e:
                    if '410' not in str(e):
                        errors += 1
                        log_event('ERROR', f'Failed to delete event: {str(e)}')
        
        log_event('SUCCESS', f'Sync completed: {added} added, {updated} updated, {deleted} deleted, {no_change} no change, {errors} errors')
        return {'added': added, 'updated': updated, 'deleted': deleted, 'errors': errors}
        
    except Exception as e:
        log_event('ERROR', f'Sync failed: {e}')
        raise

def sync_loop():
    """Main sync loop."""
    from datetime import datetime as dt
    import pytz
    
    log_event('INFO', 'Sync service started with smart scheduling')
    last_full_sync_day = None
    
    while True:
        try:
            config = load_config()
            
            if not config.get('ics_url'):
                log_event('WARNING', 'No ICS URL configured, waiting...')
                time.sleep(60)
                continue
            
            full_sync_hour = config.get('full_sync_hour', 0)
            full_sync_tz = config.get('full_sync_timezone', 'UTC')
            
            if last_full_sync_day is None:
                log_event('INFO', f'Quick sync (7 days) runs every interval, Full sync at {full_sync_hour:02d}:00 {full_sync_tz}')
            
            try:
                tz = pytz.timezone(full_sync_tz)
                current_time = dt.now(tz)
            except Exception:
                log_event('WARNING', f'Invalid timezone {full_sync_tz}, using UTC')
                tz = pytz.UTC
                current_time = dt.now(tz)
            
            current_day = current_time.date()
            should_full_sync = (
                last_full_sync_day != current_day and
                full_sync_hour <= current_time.hour < (full_sync_hour + 1)
            )
            
            if should_full_sync:
                log_event('INFO', 'Performing daily full sync')
                sync_calendar(config['ics_url'], config.get('calendar_id', 'primary'), quick_sync=False)
                last_full_sync_day = current_day
            else:
                sync_calendar(config['ics_url'], config.get('calendar_id', 'primary'), quick_sync=True)
            
            interval = config.get('sync_interval', 60)
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
