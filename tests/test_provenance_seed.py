from secondlife.federation import distinct_sources

def test_reserialized_json_has_two_raw_hashes():
    a=b'[{"id":"T1"}]'
    b=b'[ { "id" : "T1" } ]'
    assert len(distinct_sources([a,b]))==2
