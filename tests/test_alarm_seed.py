import json
from pathlib import Path
from secondlife.alarm_capture import observed, confirmed

def test_alarm_baseline():
    row=json.loads((Path(__file__).parents[1]/'fixtures'/'string-alarm.json').read_text())[0]
    assert observed(row) is True
    assert confirmed(row) is True
