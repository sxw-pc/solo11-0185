import json
from pathlib import Path
from secondlife.health import evaluate

def test_health_snapshot_baseline():
    cell=json.loads((Path(__file__).parents[1]/'fixtures'/'test-ok.json').read_text())[0]
    assert evaluate(cell)[3]=='合格'
