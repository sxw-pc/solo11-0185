import json
from pathlib import Path
from secondlife.custody import evaluate

def test_custody_baseline():
    origin=json.loads((Path(__file__).parents[1]/'fixtures'/'origin-ok.json').read_text())[0]
    assert evaluate(origin,[origin])==(True,False,False)
