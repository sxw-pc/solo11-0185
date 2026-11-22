def aggregate(rows):
    return {'count':len(rows),'capacity_sum':sum(row['capacity_ah'] for row in rows)}
