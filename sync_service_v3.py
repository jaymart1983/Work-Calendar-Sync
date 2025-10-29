#!/usr/bin/env python3
"""
ICS to Google Calendar Sync Service - Table-based comparison approach
Background service that syncs calendars using structured event tables
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
import pytz
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

def parse_datetime_to_utc(dt_obj):
    """Parse a datetime or date object to UTC datetime."""
    if isinstance(dt_obj, datetime):
        if dt_obj.tzinfo:
            return dt_obj.astimezone(timezone.utc)
        else:
            return dt_obj.replace(tzinfo=timezone.utc)
    else:
        # It's a date object - return midnight UTC
        return datetime.combine(dt_obj, datetime.min.time()).replace(tzinfo=timezone.utc)

def format_time_in_tz(dt_utc, tz_name):
    """Format UTC datetime in a specific timezone for display."""
    try:
        tz = pytz.timezone(tz_name)
        local_dt = dt_utc.astimezone(tz)
        return local_dt.strftime('%Y-%m-%d %H:%M:%S %Z')
    except:
        return dt_utc.strftime('%Y-%m-%d %H:%M:%S UTC')

def build_ics_event_table(ics_cal, start_date, end_date):
    """Build standardized table of ICS events.
    
    Returns list of dicts with: uid, summary, start_utc, end_utc, tz_name, tz_offset, is_all_day, ics_component
    """
    events = []
    expanded = recurring_ical_events.of(ics_cal).between(start_date, end_date)
    
    for component in expanded:
        uid = str(component.get('uid', ''))
        if not uid:
            continue
            
        summary = str(component.get('summary', 'No Title'))
        
        # Parse start
        dtstart = component.get('dtstart')
        if not dtstart:
            continue
        start_dt = dtstart.dt
        start_utc = parse_datetime_to_utc(start_dt)
        
        # Parse end
        dtend = component.get('dtend')
        if dtend:
            end_dt = dtend.dt
            end_utc = parse_datetime_to_utc(end_dt)
        else:
            end_utc = start_utc + timedelta(hours=1)  # Default 1 hour
        
        # Get timezone info
        if isinstance(start_dt, datetime) and start_dt.tzinfo:
            tz_name = getattr(start_dt.tzinfo, 'zone', 'UTC')
            tz_offset = start_dt.strftime('%z')
        else:
            tz_name = 'UTC'
            tz_offset = '+0000'
        
        is_all_day = not isinstance(start_dt, datetime)
        
        # Create unique key: UID + start time in UTC
        key = f"{uid}_{start_utc.isoformat()}"
        
        events.append({
            'key': key,
            'uid': uid,
            'summary': summary,
            'start_utc': start_utc,
            'end_utc': end_utc,
            'tz_name': tz_name,
            'tz_offset': tz_offset,
            'is_all_day': is_all_day,
            'ics_component': component
        })
    
    return events

def build_gcal_event_table(service, calendar_id, start_date, end_date):
    """Build standardized table of Google Calendar events.
    
    Returns list of dicts with: uid, summary, start_utc, end_utc, tz_name, tz_offset, is_all_day, event_id
    """
    events = []
    page_token = None
    
    while True:
        events_result = service.events().list(
            calendarId=calendar_id,
            pageToken=page_token,
            maxResults=2500,
            singleEvents=True,
            showDeleted=False,  # Only get active events
            timeMin=start_date.isoformat(),
            timeMax=end_date.isoformat()
        ).execute()
        
        for event in events_result.get('items', []):
            if 'iCalUID' not in event or event.get('status') != 'confirmed':
                continue
            
            uid = event['iCalUID']
            summary = event.get('summary', 'No Title')
            event_id = event['id']
            
            # Parse start
            start = event.get('start', {})
            if 'dateTime' in start:
                start_dt = dt_parser.isoparse(start['dateTime'])
                start_utc = start_dt.astimezone(timezone.utc)
                tz_name = start.get('timeZone', 'UTC')
                tz_offset = start_dt.strftime('%z')
                is_all_day = False
            elif 'date' in start:
                start_date_obj = dt_parser.isoparse(start['date']).date()
                start_utc = datetime.combine(start_date_obj, datetime.min.time()).replace(tzinfo=timezone.utc)
                tz_name = 'UTC'
                tz_offset = '+0000'
                is_all_day = True
            else:
                continue
            
            # Parse end
            end = event.get('end', {})
            if 'dateTime' in end:
                end_dt = dt_parser.isoparse(end['dateTime'])
                end_utc = end_dt.astimezone(timezone.utc)
            elif 'date' in end:
                end_date_obj = dt_parser.isoparse(end['date']).date()
                end_utc = datetime.combine(end_date_obj, datetime.min.time()).replace(tzinfo=timezone.utc)
            else:
                end_utc = start_utc + timedelta(hours=1)
            
            # Create unique key: UID + start time in UTC
            key = f"{uid}_{start_utc.isoformat()}"
            
            events.append({
                'key': key,
                'uid': uid,
                'summary': summary,
                'start_utc': start_utc,
                'end_utc': end_utc,
                'tz_name': tz_name,
                'tz_offset': tz_offset,
                'is_all_day': is_all_day,
                'event_id': event_id
            })
        
        page_token = events_result.get('nextPageToken')
        if not page_token:
            break
    
    return events

def convert_ics_event_to_gcal(ics_event_row):
    """Convert ICS event table row to Google Calendar event format."""
    component = ics_event_row['ics_component']
    
    gcal_event = {
        'summary': ics_event_row['summary'],
        'description': str(component.get('description', '')),
        'location': str(component.get('location', '')),
        'iCalUID': ics_event_row['uid']
    }
    
    # Convert start/end based on whether it's all-day
    if ics_event_row['is_all_day']:
        gcal_event['start'] = {'date': ics_event_row['start_utc'].date().isoformat()}
        gcal_event['end'] = {'date': ics_event_row['end_utc'].date().isoformat()}
    else:
        # Use original timezone from ICS
        start_dt = component.get('dtstart').dt
        end_dt = component.get('dtend').dt if component.get('dtend') else start_dt + timedelta(hours=1)
        
        gcal_event['start'] = {'dateTime': start_dt.isoformat()}
        if hasattr(start_dt.tzinfo, 'zone'):
            gcal_event['start']['timeZone'] = start_dt.tzinfo.zone
        
        gcal_event['end'] = {'dateTime': end_dt.isoformat()}
        if hasattr(end_dt.tzinfo, 'zone'):
            gcal_event['end']['timeZone'] = end_dt.tzinfo.zone
    
    return gcal_event

def sync_calendar(ics_url, calendar_id, quick_sync=True):
    """Perform calendar sync with locking to prevent concurrent syncs."""
    global sync_in_progress
    
    with sync_lock:
        if sync_in_progress:
            log_event('WARNING', 'Sync already in progress, skipping this run')
            return {'added': 0, 'deleted': 0, 'errors': 0, 'skipped_due_to_lock': True}
        sync_in_progress = True
    
    try:
        return _do_sync(ics_url, calendar_id, quick_sync)
    finally:
        with sync_lock:
            sync_in_progress = False

def _do_sync(ics_url, calendar_id, quick_sync):
    """Internal sync implementation with table-based comparison."""
    sync_type = 'Quick sync (7 days)' if quick_sync else 'Full sync (all events)'
    log_event('INFO', f'Starting {sync_type}')
    
    try:
        # Fetch ICS
        ics_cal = fetch_ics_calendar(ics_url)
        log_event('SUCCESS', 'ICS calendar fetched successfully')
        
        # Authenticate
        service = get_google_calendar_service()
        log_event('SUCCESS', 'Google Calendar authenticated')
        
        # Get calendar timezone
        gcal_info = service.calendars().get(calendarId=calendar_id).execute()
        cal_tz_name = gcal_info.get('timeZone', 'UTC')
        try:
            cal_tz = pytz.timezone(cal_tz_name)
        except Exception:
            cal_tz = pytz.UTC
        
        # Define date range
        if quick_sync:
            now_in_cal_tz = datetime.now(cal_tz)
            today_in_cal_tz = now_in_cal_tz.date()
            start_date = cal_tz.localize(datetime.combine(today_in_cal_tz, datetime.min.time())).astimezone(timezone.utc)
            end_date = cal_tz.localize(datetime.combine(today_in_cal_tz + timedelta(days=7), datetime.max.time())).astimezone(timezone.utc)
            log_event('INFO', f'Quick sync: {today_in_cal_tz} to {today_in_cal_tz + timedelta(days=7)} ({cal_tz_name})')
        else:
            today = date.today()
            start_date = datetime.combine(today - timedelta(days=30), datetime.min.time()).replace(tzinfo=timezone.utc)
            end_date = datetime.combine(today + timedelta(days=365), datetime.max.time()).replace(tzinfo=timezone.utc)
            log_event('INFO', 'Full sync: past 30 days to future 365 days')
        
        # Step 1: Build ICS event table
        log_event('INFO', 'Building ICS event table...')
        ics_events = build_ics_event_table(ics_cal, start_date, end_date)
        ics_keys = {evt['key'] for evt in ics_events}
        log_event('INFO', f'ICS table: {len(ics_events)} events')
        
        # Step 2: Build Google Calendar event table
        log_event('INFO', 'Building Google Calendar event table...')
        gcal_events = build_gcal_event_table(service, calendar_id, start_date, end_date)
        gcal_keys = {evt['key'] for evt in gcal_events}
        gcal_lookup = {evt['key']: evt for evt in gcal_events}
        log_event('INFO', f'Google Calendar table: {len(gcal_events)} events')
        
        # Step 3: Find events to delete (in GCal but not in ICS)
        keys_to_delete = gcal_keys - ics_keys
        log_event('INFO', f'Events to delete: {len(keys_to_delete)}')
        
        # Step 4: Delete events
        deleted = 0
        errors = 0
        for key in keys_to_delete:
            gcal_evt = gcal_lookup[key]
            try:
                service.events().delete(
                    calendarId=calendar_id,
                    eventId=gcal_evt['event_id']
                ).execute()
                deleted += 1
                display_time = format_time_in_tz(gcal_evt['start_utc'], cal_tz_name)
                log_event('DELETE', f"Deleted: {gcal_evt['summary']} at {display_time}")
                sleep(0.3)
            except Exception as e:
                if '410' not in str(e):  # Already deleted
                    errors += 1
                    log_event('ERROR', f"Failed to delete {gcal_evt['summary']}: {str(e)}")
        
        # Step 5: Find events to add (in ICS but not in GCal)
        keys_to_add = ics_keys - gcal_keys
        log_event('INFO', f'Events to add: {len(keys_to_add)}')
        
        # Step 6: Add events
        added = 0
        ics_lookup = {evt['key']: evt for evt in ics_events}
        for key in keys_to_add:
            ics_evt = ics_lookup[key]
            try:
                gcal_event = convert_ics_event_to_gcal(ics_evt)
                service.events().insert(
                    calendarId=calendar_id,
                    body=gcal_event
                ).execute()
                added += 1
                display_time = format_time_in_tz(ics_evt['start_utc'], cal_tz_name)
                log_event('ADD', f"Added: {ics_evt['summary']} at {display_time}")
                sleep(0.3)
            except Exception as e:
                errors += 1
                log_event('ERROR', f"Failed to add {ics_evt['summary']}: {str(e)}")
        
        # Step 7: Summary
        log_event('SUCCESS', f'Sync completed: {added} added, {deleted} deleted, {errors} errors')
        
        return {'added': added, 'deleted': deleted, 'errors': errors}
        
    except Exception as e:
        log_event('ERROR', f'Sync failed: {e}')
        raise

def sync_loop():
    """Main sync loop."""
    log_event('INFO', 'Sync service started')
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
            
            try:
                tz = pytz.timezone(full_sync_tz)
                current_time = datetime.now(tz)
            except Exception:
                log_event('WARNING', f'Invalid timezone {full_sync_tz}, using UTC')
                tz = pytz.UTC
                current_time = datetime.now(tz)
            
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
