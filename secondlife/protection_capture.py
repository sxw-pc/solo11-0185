from datetime import datetime

def instant(value):
    return datetime.fromisoformat(value.replace('Z', '+00:00'))

def evaluate(string):
    return (string.get('protection_ok', False)
            and string.get('protection_trip_ms', 999) <= string.get('max_trip_ms', 0)
            and len(set(string.get('signed_by', []))) >= 2
            and instant(string['signed_at']) < instant(string['permit_until']))
