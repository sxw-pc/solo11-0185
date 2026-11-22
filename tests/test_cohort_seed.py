from secondlife.publication import release

def test_three_pack_cohort_baseline():
    rows=[{'pack_id':k,'capacity_ah':v} for k,v in [('A',70),('B',80),('C',90)]]
    assert release(rows)=={'count':3,'capacity_sum':240}
