from .bid_capture import highest

def winners(lots):
    return {lot:highest(rows) for lot,rows in lots.items()}
