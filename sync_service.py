#!/usr/bin/env python3
"""Simple ICS to Google Calendar Sync - Delete all in timeframe, then add all ICS events"""

import json
import os
import time
import sys
from datetime import datetime, timezone, timedelta
from threading import Lock
import requests
from icalendar import Calendar
import recurring_ical_events
from dateutil import parser as dt_parser
import pytz
from google.oauth2.service_account import Credentials as ServiceAccountCredentials
from google_auth_oauthlib.flow import InstalledAppFlow
from google.auth.transport.requests import Request
from googleapiclient.discovery import build
import pickle

SCOPES = ['https://www.googleapis.com/auth/calendar']
BASE_DIR = os.environ.get('APP_BASE_DIR', '/app')
DATA_DIR = os.path.join(BASE_DIR, 'data')
SECRETS_DIR = os.path.join(BASE_DIR, 'secrets')
CONFIG_FILE = os.path.join(DATA_DIR, 'config.json')
TOKEN_FILE = os.path.join(DATA_DIR, 'token.pickle')
LOG_FILE = os.path.join(DATA_DIR, 'sync_logs.json')
CREDENTIALS_FILE = os.path.join(SECRETS_DIR, 'credentials.json')

log_buffer = []
log_lock = Lock()
sync_lock = Lock()
sync_in_progress = False

def log(level, message):
    entry = {'timestamp': datetime.now().isoformat(), 'level': level, 'message': message}
    with log_lock:
        log_buffer.append(entry)
        if len(log_buffer) > 1000:
            log_buffer.pop(0)
    print(f"[{entry['timestamp']}] {level}: {message}")
    try:
        with open(LOG_FILE, 'a') as f:
            f.write(json.dumps(entry) + '\n')
    except:
        pass

def get_logs(limit=100):
    with log_lock:
        return log_buffer[-limit:]

def load_config():
    if not os.path.exists(CONFIG_FILE):
        return {'ics_url': '', 'calendar_id': 'primary', 'sync_interval': 60, 'full_sync_hour': 0, 'full_sync_timezone': 'UTC'}
    try:
        with open(CONFIG_FILE, 'r') as f:
            return json.load(f)
    except:
        return {}

def save_config(config):
    os.makedirs(os.path.dirname(CONFIG_FILE), exist_ok=True)
    with open(CONFIG_FILE, 'w') as f:
        json.dump(config, f, indent=2)
    log('INFO', 'Configuration updated')

def get_google_service():
    if os.path.exists(CREDENTIALS_FILE):
        try:
            with open(CREDENTIALS_FILE, 'r') as f:
                if json.load(f).get('type') == 'service_account':
                    log('INFO', 'Using service account')
                    return build('calendar', 'v3', credentials=ServiceAccountCredentials.from_service_account_file(CREDENTIALS_FILE, scopes=SCOPES))
        except:
            pass
    
    creds = None
    if os.path.exists(TOKEN_FILE):
        with open(TOKEN_FILE, 'rb') as token:
            creds = pickle.load(token)
    
    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            creds.refresh(Request())
        else:
            creds = InstalledAppFlow.from_client_secrets_file(CREDENTIALS_FILE, SCOPES).run_local_server(port=8095)
        os.makedirs(os.path.dirname(TOKEN_FILE), exist_ok=True)
        with open(TOKEN_FILE, 'wb') as token:
            pickle.dump(creds, token)
    
    return build('calendar', 'v3', credentials=creds)

def format_time(dt_utc, tz_name):
    try:
        return dt_utc.astimezone(pytz.timezone(tz_name)).strftime('%Y-%m-%d %H:%M:%S %Z')
    except:
        return dt_utc.strftime('%Y-%m-%d %H:%M:%S UTC')

def get_event_key(summary, start_dt, end_dt):
    """Create unique key for event matching using summary + start + end times."""
    # Normalize to UTC for consistent comparison
    if isinstance(start_dt, datetime):
        start_utc = start_dt.astimezone(timezone.utc) if start_dt.tzinfo else start_dt.replace(tzinfo=timezone.utc)
        start_str = start_utc.strftime('%Y-%m-%d %H:%M')
    else:
        start_str = start_dt.isoformat()
    
    if isinstance(end_dt, datetime):
        end_utc = end_dt.astimezone(timezone.utc) if end_dt.tzinfo else end_dt.replace(tzinfo=timezone.utc)
        end_str = end_utc.strftime('%Y-%m-%d %H:%M')
    else:
        end_str = end_dt.isoformat()
    
    return f"{summary}|{start_str}|{end_str}"

def get_gcal_events(service, calendar_id, start, end):
    """Fetch all Google Calendar events and create lookup by key."""
    events = {}
    page_token = None
    
    while True:
        result = service.events().list(
            calendarId=calendar_id, pageToken=page_token, maxResults=2500,
            singleEvents=True, showDeleted=False, timeMin=start.isoformat(), timeMax=end.isoformat()
        ).execute()
        
        for event in result.get('items', []):
            summary = event.get('summary', 'No Title')
            
            # Parse start
            s = event.get('start', {})
            if 'dateTime' in s:
                start_dt = dt_parser.isoparse(s['dateTime'])
            elif 'date' in s:
                start_dt = dt_parser.isoparse(s['date']).date()
            else:
                continue
            
            # Parse end
            e = event.get('end', {})
            if 'dateTime' in e:
                end_dt = dt_parser.isoparse(e['dateTime'])
            elif 'date' in e:
                end_dt = dt_parser.isoparse(e['date']).date()
            else:
                end_dt = start_dt
            
            key = get_event_key(summary, start_dt, end_dt)
            events[key] = {'id': event['id'], 'summary': summary, 'start': start_dt, 'end': end_dt}
        
        page_token = result.get('nextPageToken')
        if not page_token:
            break
    
    return events

def get_ics_events(ics_cal, start, end):
    """Get all ICS events and create lookup by key."""
    events = {}
    expanded = list(recurring_ical_events.of(ics_cal).between(start, end))
    
    for component in expanded:
        summary = str(component.get('summary', 'No Title'))
        
        # Parse start
        dtstart = component.get('dtstart')
        if not dtstart:
            continue
        start_dt = dtstart.dt
        
        # Parse end
        dtend = component.get('dtend')
        end_dt = dtend.dt if dtend else start_dt
        
        key = get_event_key(summary, start_dt, end_dt)
        events[key] = {'summary': summary, 'start_dt': start_dt, 'end_dt': end_dt, 'component': component}
    
    return events

def add_event(service, calendar_id, ics_event, tz_name):
    """Add a single ICS event to Google Calendar."""
    component = ics_event['component']
    summary = ics_event['summary']
    start_dt = ics_event['start_dt']
    end_dt = ics_event['end_dt']
    
    gcal_event = {
        'summary': summary,
        'description': str(component.get('description', '')),
        'location': str(component.get('location', ''))
    }
    
    # Start time
    if isinstance(start_dt, datetime):
        gcal_event['start'] = {'dateTime': start_dt.isoformat()}
        if hasattr(start_dt.tzinfo, 'zone'):
            gcal_event['start']['timeZone'] = start_dt.tzinfo.zone
        time_str = format_time(start_dt.astimezone(timezone.utc) if start_dt.tzinfo else start_dt.replace(tzinfo=timezone.utc), tz_name)
    else:
        gcal_event['start'] = {'date': start_dt.isoformat()}
        time_str = start_dt.isoformat()
    
    # End time
    if isinstance(end_dt, datetime):
        gcal_event['end'] = {'dateTime': end_dt.isoformat()}
        if hasattr(end_dt.tzinfo, 'zone'):
            gcal_event['end']['timeZone'] = end_dt.tzinfo.zone
    else:
        gcal_event['end'] = {'date': end_dt.isoformat()}
    
    service.events().insert(calendarId=calendar_id, body=gcal_event).execute()
    log('ADD', f"{summary} at {time_str}")

def delete_event(service, calendar_id, gcal_event, tz_name):
    """Delete a single Google Calendar event."""
    service.events().delete(calendarId=calendar_id, eventId=gcal_event['id']).execute()
    
    start_dt = gcal_event['start']
    if isinstance(start_dt, datetime):
        time_str = format_time(start_dt.astimezone(timezone.utc) if start_dt.tzinfo else start_dt.replace(tzinfo=timezone.utc), tz_name)
    else:
        time_str = start_dt.isoformat()
    
    log('DELETE', f"{gcal_event['summary']} at {time_str}")

def sync_calendar(ics_url, calendar_id, quick_sync=True):
    global sync_in_progress
    
    with sync_lock:
        if sync_in_progress:
            log('WARNING', 'Sync already in progress')
            return {'added': 0, 'deleted': 0, 'skipped': True}
        sync_in_progress = True
    
    try:
        log('INFO', f"Starting {'Quick (7d)' if quick_sync else 'Full (30d)'} sync")
        
        # Fetch ICS
        ics_cal = Calendar.from_ical(requests.get(ics_url, timeout=30).content)
        log('SUCCESS', 'ICS fetched')
        
        # Auth Google
        service = get_google_service()
        log('SUCCESS', 'Google authenticated')
        
        # Get timezone
        cal_info = service.calendars().get(calendarId=calendar_id).execute()
        tz_name = cal_info.get('timeZone', 'UTC')
        tz = pytz.timezone(tz_name)
        
        # Date range
        today = datetime.now(tz).date()
        if quick_sync:
            start = tz.localize(datetime.combine(today, datetime.min.time())).astimezone(timezone.utc)
            end = tz.localize(datetime.combine(today + timedelta(days=7), datetime.max.time())).astimezone(timezone.utc)
        else:
            start = tz.localize(datetime.combine(today - timedelta(days=30), datetime.min.time())).astimezone(timezone.utc)
            end = tz.localize(datetime.combine(today + timedelta(days=30), datetime.max.time())).astimezone(timezone.utc)
        
        log('INFO', f"Range: {start.date()} to {end.date()} ({tz_name})")
        
        # Get events from both sources
        log('INFO', 'Fetching Google Calendar events...')
        gcal_events = get_gcal_events(service, calendar_id, start, end)
        log('INFO', f'Found {len(gcal_events)} Google Calendar events')
        
        log('INFO', 'Fetching ICS events...')
        ics_events = get_ics_events(ics_cal, start, end)
        log('INFO', f'Found {len(ics_events)} ICS events')
        
        # Debug: Log events on 10/31
        for key, evt in ics_events.items():
            start_dt = evt['start_dt']
            if isinstance(start_dt, datetime):
                local_dt = start_dt.astimezone(pytz.timezone(tz_name))
                if local_dt.date().isoformat() == '2025-10-31':
                    log('DEBUG', f"ICS 10/31 event: {evt['summary']} at {local_dt.strftime('%H:%M')} {tz_name}")
            elif hasattr(start_dt, 'isoformat') and start_dt.isoformat() == '2025-10-31':
                log('DEBUG', f"ICS 10/31 all-day: {evt['summary']}")
        
        # Compare and find differences
        gcal_keys = set(gcal_events.keys())
        ics_keys = set(ics_events.keys())
        
        to_delete = gcal_keys - ics_keys  # In Google but not in ICS
        to_add = ics_keys - gcal_keys     # In ICS but not in Google
        unchanged = gcal_keys & ics_keys   # In both
        
        log('INFO', f'To delete: {len(to_delete)}, To add: {len(to_add)}, Unchanged: {len(unchanged)}')
        
        # Delete events no longer in ICS
        deleted = 0
        for key in to_delete:
            try:
                delete_event(service, calendar_id, gcal_events[key], tz_name)
                deleted += 1
                time.sleep(0.2)
            except Exception as e:
                if '410' not in str(e):
                    log('ERROR', f"Delete failed: {str(e)}")
        
        # Add new events from ICS
        added = 0
        for key in to_add:
            try:
                add_event(service, calendar_id, ics_events[key], tz_name)
                added += 1
                time.sleep(0.2)
            except Exception as e:
                log('ERROR', f"Add failed: {str(e)}")
        
        log('SUCCESS', f"Done: {added} added, {deleted} deleted, {len(unchanged)} unchanged")
        return {'added': added, 'deleted': deleted, 'unchanged': len(unchanged)}
        
    except Exception as e:
        log('ERROR', f"Sync failed: {e}")
        raise
    finally:
        with sync_lock:
            sync_in_progress = False

def sync_loop():
    log('INFO', 'Sync service started')
    last_full_sync_day = None
    
    while True:
        try:
            config = load_config()
            if not config.get('ics_url'):
                log('WARNING', 'No ICS URL configured')
                time.sleep(60)
                continue
            
            tz = pytz.timezone(config.get('full_sync_timezone', 'UTC'))
            now = datetime.now(tz)
            should_full = (last_full_sync_day != now.date() and 
                          config.get('full_sync_hour', 0) <= now.hour < config.get('full_sync_hour', 0) + 1)
            
            if should_full:
                sync_calendar(config['ics_url'], config.get('calendar_id', 'primary'), quick_sync=False)
                last_full_sync_day = now.date()
            else:
                sync_calendar(config['ics_url'], config.get('calendar_id', 'primary'), quick_sync=True)
            
            interval = config.get('sync_interval', 60)
            log('INFO', f"Next sync in {interval}s")
            time.sleep(interval)
        
        except KeyboardInterrupt:
            log('INFO', 'Shutting down')
            sys.exit(0)
        except Exception as e:
            log('ERROR', f"Loop error: {e}")
            time.sleep(60)

if __name__ == '__main__':
    sync_loop()
