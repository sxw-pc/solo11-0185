from secondlife.auction import winners

def test_local_highest_baseline():
    result=winners({'LOT1':[{'bidder':'A','amount':80},{'bidder':'B','amount':70}]})
    assert result['LOT1']=={'bidder':'A','amount':80}
