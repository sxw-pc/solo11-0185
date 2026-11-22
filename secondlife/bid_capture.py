def highest(rows):
    return max(rows, key=lambda row:(row['amount'],row['bidder']))
