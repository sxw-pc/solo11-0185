from .cohort import aggregate

def release(rows, minimum=3):
    result=aggregate(rows)
    return result if result['count']>=minimum else {'suppressed':True}
