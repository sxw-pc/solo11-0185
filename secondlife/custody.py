def evaluate(origin, events):
    pack_id = origin['pack_id']
    chain = origin.get('owner_chain', [])
    competing = {e.get('claimed_owner') for e in events if e['kind'] == 'origin' and e['pack_id'] == pack_id}
    duplicate = len(competing) > 1 and not origin.get('transfer_documented', False)
    ownership = bool(chain) and chain[-1] == origin.get('claimed_owner') and not duplicate
    recalled = origin.get('recalled', False)
    return ownership, duplicate, recalled
