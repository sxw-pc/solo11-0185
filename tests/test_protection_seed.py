import json
from pathlib import Path
from secondlife.protection_capture import evaluate

def test_protection_baseline():
    row=json.loads((Path(__file__).parents[1]/'fixtures'/'string-ok.json').read_text())[0]
    assert evaluate(row) is True
