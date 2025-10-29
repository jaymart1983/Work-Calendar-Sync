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
            'sync_interval': 900,
            'full_sync_hour': 0,  # Hour of day for full sync (0-23)
            'full_sync_timezone': 'UTC'  # Timezone for full sync scheduling
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
            # Preserve timezone info from ICS or use the datetime as-is
            start_dict = {'dateTime': start_dt.isoformat()}
            # Only add timeZone if we have timezone info
            if start_dt.tzinfo:
                # Get timezone name if available
                tz_name = str(start_dt.tzinfo)
                # Google Calendar accepts IANA timezone names or uses the offset in isoformat
                # Since isoformat includes offset, we can omit timeZone field
                # or try to get a proper IANA name
                if hasattr(start_dt.tzinfo, 'zone'):
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
            if end_dt.tzinfo:
                if hasattr(end_dt.tzinfo, 'zone'):
                    end_dict['timeZone'] = end_dt.tzinfo.zone
            gcal_event['end'] = end_dict
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
        
        # Detect ICS timezone from first timed event
        ics_timezone = None
        ics_offset = None
        for component in ics_cal.walk():
            if component.name == "VEVENT":
                dtstart = component.get('dtstart')
                if dtstart:
                    start_dt = dtstart.dt
                    if isinstance(start_dt, datetime) and start_dt.tzinfo:
                        if hasattr(start_dt.tzinfo, 'zone'):
                            ics_timezone = start_dt.tzinfo.zone
                        # Get offset
                        offset = start_dt.strftime('%z')
                        if offset:
                            ics_offset = f"{offset[:3]}:{offset[3:]}"
                        break
        
        # Authenticate
        service = get_google_calendar_service()
        log_event('SUCCESS', 'Google Calendar authenticated')
        
        # Get Google Calendar timezone
        gcal_info = service.calendars().get(calendarId=calendar_id).execute()
        gcal_timezone = gcal_info.get('timeZone', 'Unknown')
        
        # Calculate Google Calendar offset
        gcal_offset = None
        if gcal_timezone and gcal_timezone != 'Unknown':
            try:
                import pytz
                tz = pytz.timezone(gcal_timezone)
                # Get current offset (accounts for DST)
                offset = tz.localize(datetime.now()).strftime('%z')
                if offset:
                    gcal_offset = f"{offset[:3]}:{offset[3:]}"
            except Exception:
                pass
        
        # Get existing events - use UID + start time as key for recurring events
        existing_events = {}  # key: (iCalUID, start_time_str), value: event_id
        page_token = None
        while True:
            events_result = service.events().list(
                calendarId=calendar_id,
                pageToken=page_token,
                maxResults=2500
            ).execute()
            
            for event in events_result.get('items', []):
                if 'iCalUID' in event:
                    ical_uid = event['iCalUID']
                    # Get start time for unique identification
                    start = event.get('start', {})
                    start_key = start.get('date') or start.get('dateTime', '')
                    # Use UID + start time as composite key
                    key = (ical_uid, start_key)
                    existing_events[key] = event['id']
            
            page_token = events_result.get('nextPageToken')
            if not page_token:
                break
        
        log_event('INFO', f'Found {len(existing_events)} existing event instances')
        
        # Set date range for quick sync
        if quick_sync:
            today = date.today()
            end_date = today + timedelta(days=7)
            log_event('INFO', f'Quick sync: filtering events from {today} to {end_date}')
        
        # Track which ICS events we've seen
        ics_event_uids = set()
        
        # Process events
        added = 0
        updated = 0
        no_change = 0
        deleted = 0
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
                    
                    # Get start time for composite key
                    start = gcal_event.get('start', {})
                    start_key = start.get('date') or start.get('dateTime', '')
                    event_key = (ical_uid, start_key)
                    
                    # Track this UID as present in ICS feed (just the UID portion)
                    if ical_uid:
                        ics_event_uids.add(event_key)
                    
                    if event_key in existing_events:
                        # Get existing event to compare
                        existing_event = service.events().get(
                            calendarId=calendar_id,
                            eventId=existing_events[event_key]
                        ).execute()
                        
                        # Check if event actually changed (compare key fields, normalizing for comparison)
                        # Compare summary
                        summary_changed = str(existing_event.get('summary', '')) != str(gcal_event.get('summary', ''))
                        
                        # Compare description
                        desc_changed = str(existing_event.get('description', '')) != str(gcal_event.get('description', ''))
                        
                        # Compare location
                        loc_changed = str(existing_event.get('location', '')) != str(gcal_event.get('location', ''))
                        
                        # Compare start/end times - need to handle date vs dateTime properly
                        def normalize_datetime(dt_dict):
                            """Normalize a datetime dict for comparison by converting to UTC."""
                            from dateutil import parser
                            
                            if 'date' in dt_dict:
                                return ('date', dt_dict['date'])
                            elif 'dateTime' in dt_dict:
                                # Parse the datetime string (handles timezone offsets)
                                dt_str = dt_dict['dateTime']
                                try:
                                    dt = parser.isoparse(dt_str)
                                    # Convert to UTC for comparison
                                    if dt.tzinfo:
                                        dt_utc = dt.astimezone(timezone.utc)
                                    else:
                                        # Treat naive datetime as UTC
                                        dt_utc = dt.replace(tzinfo=timezone.utc)
                                    # Return just the UTC time for comparison
                                    return ('dateTime', dt_utc.strftime('%Y-%m-%dT%H:%M:%S'))
                                except Exception as e:
                                    # Fallback to string comparison if parsing fails
                                    return ('dateTime', dt_str)
                            return (None, None)
                        
                        existing_start = existing_event.get('start', {})
                        new_start = gcal_event.get('start', {})
                        start_changed = normalize_datetime(existing_start) != normalize_datetime(new_start)
                        
                        existing_end = existing_event.get('end', {})
                        new_end = gcal_event.get('end', {})
                        end_changed = normalize_datetime(existing_end) != normalize_datetime(new_end)
                        
                        has_changes = summary_changed or desc_changed or loc_changed or start_changed or end_changed
                        
                        # Determine event type and time info
                        is_all_day = 'date' in gcal_event.get('start', {})
                        event_type = 'all-day' if is_all_day else 'timed'
                        
                        if is_all_day:
                            event_date_str = gcal_event.get('start', {}).get('date', 'Unknown date')
                        else:
                            datetime_str = gcal_event.get('start', {}).get('dateTime', '')
                            if datetime_str:
                                # Extract date and time
                                event_date_str = datetime_str.split('T')[0] if 'T' in datetime_str else datetime_str
                                time_part = datetime_str.split('T')[1].split(':')[0:2] if 'T' in datetime_str else []
                                if time_part:
                                    event_type = f"{':'.join(time_part)}"
                            else:
                                event_date_str = 'Unknown date'
                        
                        if has_changes:
                            # Build change details
                            changes = []
                            if summary_changed:
                                changes.append('summary')
                            if desc_changed:
                                changes.append('description')
                            if loc_changed:
                                changes.append('location')
                            if start_changed:
                                changes.append(f'start: {normalize_datetime(existing_start)} -> {normalize_datetime(new_start)}')
                            if end_changed:
                                changes.append(f'end: {normalize_datetime(existing_end)} -> {normalize_datetime(new_end)}')
                            
                            change_detail = ', '.join(changes)
                            
                            # Update the event
                            service.events().update(
                                calendarId=calendar_id,
                                eventId=existing_events[event_key],
                                body=gcal_event
                            ).execute()
                            updated += 1
                            log_event('UPDATE', f'Updated: {gcal_event["summary"]} ({event_date_str}, {event_type}) - Changed: {change_detail}')
                            # Rate limit: 1 request per second to avoid API quota
                            sleep(1.1)
                        else:
                            # No changes needed - don't log to reduce noise
                            no_change += 1
                    else:
                        try:
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
                        except Exception as insert_error:
                            # Handle 409 duplicate error - event already exists but wasn't in our lookup
                            if 'already exists' in str(insert_error).lower():
                                # Treat as no change - event exists and is presumably correct
                                no_change += 1
                            else:
                                # Re-raise other errors
                                raise
                
                except Exception as e:
                    errors += 1
                    log_event('ERROR', f'Failed to process event: {e}')
                    # Back off on errors to avoid hammering the API
                    sleep(2)
        
        # Delete events that exist in Google Calendar but not in ICS feed
        log_event('INFO', f'Checking for events to delete...')
        for event_key, gcal_event_id in existing_events.items():
            if event_key not in ics_event_uids:
                try:
                    # Get event details for logging
                    event_to_delete = service.events().get(
                        calendarId=calendar_id,
                        eventId=gcal_event_id
                    ).execute()
                    
                    event_summary = event_to_delete.get('summary', 'Unknown event')
                    event_start = event_to_delete.get('start', {})
                    event_date = event_start.get('date') or event_start.get('dateTime', '')
                    event_date_str = event_date.split('T')[0] if event_date else 'Unknown date'
                    
                    # Delete the event
                    service.events().delete(
                        calendarId=calendar_id,
                        eventId=gcal_event_id
                    ).execute()
                    
                    deleted += 1
                    log_event('DELETE', f'Deleted: {event_summary} ({event_date_str})')
                    # Rate limit
                    sleep(1.1)
                    
                except Exception as e:
                    errors += 1
                    log_event('ERROR', f'Failed to delete event: {e}')
                    sleep(2)
        
        log_message = f'Sync completed: {added} added, {updated} updated, {deleted} deleted, {no_change} no change, {errors} errors'
        if quick_sync:
            log_message += f', {skipped} skipped (outside 7-day window)'
        log_event('SUCCESS', log_message, {
            'added': added,
            'updated': updated,
            'deleted': deleted,
            'no_change': no_change,
            'errors': errors,
            'skipped': skipped if quick_sync else 0
        })
        
        # Update config with detected timezone info
        config = load_config()
        config_updated = False
        if ics_timezone and not config.get('ics_timezone'):
            config['ics_timezone'] = ics_timezone
            config_updated = True
        if ics_offset and not config.get('ics_offset'):
            config['ics_offset'] = ics_offset
            config_updated = True
        if gcal_timezone and not config.get('gcal_timezone'):
            config['gcal_timezone'] = gcal_timezone
            config_updated = True
        if gcal_offset and not config.get('gcal_offset'):
            config['gcal_offset'] = gcal_offset
            config_updated = True
        
        if config_updated:
            save_config(config)
            log_event('INFO', f'Detected timezones - ICS: {ics_timezone} ({ics_offset}), Google Calendar: {gcal_timezone} ({gcal_offset})')
        
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
            
            # Get full sync configuration
            full_sync_hour = config.get('full_sync_hour', 0)
            full_sync_tz = config.get('full_sync_timezone', 'UTC')
            
            # Log the schedule once at startup
            if last_full_sync_day is None:
                log_event('INFO', f'Quick sync (7 days) runs every interval, Full sync at {full_sync_hour:02d}:00 {full_sync_tz}')
            
            # Get current time in configured timezone
            try:
                tz = pytz.timezone(full_sync_tz)
                current_time = dt.now(tz)
            except Exception:
                # Fallback to UTC if timezone is invalid
                log_event('WARNING', f'Invalid timezone {full_sync_tz}, using UTC')
                tz = pytz.UTC
                current_time = dt.now(tz)
            
            current_day = current_time.date()
            
            # Do full sync if it's a new day and we haven't done one yet today
            # and it's within the configured hour window
            should_full_sync = (
                last_full_sync_day != current_day and
                full_sync_hour <= current_time.hour < (full_sync_hour + 1)
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
