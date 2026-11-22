from datetime import datetime

def instant(value):
    return datetime.fromisoformat(value.replace('Z', '+00:00'))

def retest_agrees(t):
    return (t.get('operator') != t.get('retest_operator')
            and abs(t.get('capacity_ah', 0) - t.get('retest_capacity_ah', -999)) <= 5
            and abs(t.get('resistance_mohm', 999) - t.get('retest_resistance_mohm', -999)) <= 2
            and abs(t.get('temp_c', 999) - t.get('retest_temp_c', -999)) <= 5)

def evaluate(t):
    calibrated = t.get('calibrated', False) and instant(t['calibration_start']) <= instant(t['tested_at']) < instant(t['calibration_end'])
    healthy = t.get('capacity_ah', 0) >= 70 and t.get('resistance_mohm', 999) <= 10 and t.get('temp_c', 999) <= 45
    independent = retest_agrees(t)
    grade = '合格' if calibrated and healthy and independent and t.get('sample_count', 0) >= 2 else '待核或报废'
    return calibrated, healthy, independent, grade
