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

def delete_all(service, calendar_id, start, end, tz_name):
    deleted = 0
    page_token = None
    
    while True:
        result = service.events().list(
            calendarId=calendar_id, pageToken=page_token, maxResults=2500,
            singleEvents=True, showDeleted=False, timeMin=start.isoformat(), timeMax=end.isoformat()
        ).execute()
        
        for event in result.get('items', []):
            try:
                service.events().delete(calendarId=calendar_id, eventId=event['id']).execute()
                deleted += 1
                
                # Format time for logging
                s = event.get('start', {})
                if 'dateTime' in s:
                    dt = dt_parser.isoparse(s['dateTime']).astimezone(timezone.utc)
                    time_str = format_time(dt, tz_name)
                elif 'date' in s:
                    time_str = s['date']
                else:
                    time_str = 'unknown'
                
                log('DELETE', f"{event.get('summary', 'No Title')} at {time_str}")
                time.sleep(0.2)
            except Exception as e:
                if '410' not in str(e):
                    log('ERROR', f"Delete failed: {str(e)}")
        
        page_token = result.get('nextPageToken')
        if not page_token:
            break
    
    return deleted

def add_all(service, calendar_id, ics_cal, start, end, tz_name):
    added = 0
    events = list(recurring_ical_events.of(ics_cal).between(start, end))
    
    for component in events:
        summary = str(component.get('summary', 'No Title'))
        
        gcal_event = {
            'summary': summary,
            'description': str(component.get('description', '')),
            'location': str(component.get('location', ''))
        }
        
        # Don't set iCalUID to avoid 409 duplicate errors
        # Google Calendar will generate its own unique ID
        
        # Start time
        dtstart = component.get('dtstart')
        if dtstart:
            start_dt = dtstart.dt
            if isinstance(start_dt, datetime):
                gcal_event['start'] = {'dateTime': start_dt.isoformat()}
                if hasattr(start_dt.tzinfo, 'zone'):
                    gcal_event['start']['timeZone'] = start_dt.tzinfo.zone
                time_str = format_time(start_dt.astimezone(timezone.utc) if start_dt.tzinfo else start_dt.replace(tzinfo=timezone.utc), tz_name)
            else:
                gcal_event['start'] = {'date': start_dt.isoformat()}
                time_str = start_dt.isoformat()
        else:
            time_str = 'unknown'
        
        # End time
        dtend = component.get('dtend')
        if dtend:
            end_dt = dtend.dt
            if isinstance(end_dt, datetime):
                gcal_event['end'] = {'dateTime': end_dt.isoformat()}
                if hasattr(end_dt.tzinfo, 'zone'):
                    gcal_event['end']['timeZone'] = end_dt.tzinfo.zone
            else:
                gcal_event['end'] = {'date': end_dt.isoformat()}
        
        try:
            service.events().insert(calendarId=calendar_id, body=gcal_event).execute()
            added += 1
            log('ADD', f"{summary} at {time_str}")
            time.sleep(0.2)
        except Exception as e:
            log('ERROR', f"Add failed for {summary}: {str(e)}")
    
    return added

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
        
        # Delete all, then add all
        deleted = delete_all(service, calendar_id, start, end, tz_name)
        added = add_all(service, calendar_id, ics_cal, start, end, tz_name)
        
        log('SUCCESS', f"Done: {added} added, {deleted} deleted")
        return {'added': added, 'deleted': deleted}
        
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
