from secondlife.recovery import total

def test_net_weights_baseline():
    rows=[{'gross_kg':110,'tare_kg':10},{'gross_kg':90,'tare_kg':10}]
    assert total(rows)==180
