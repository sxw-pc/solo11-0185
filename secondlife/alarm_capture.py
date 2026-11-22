def observed(string):
    return string.get('alarm', False)

def confirmed(string):
    return observed(string) and string.get('alarm_sensor_valid') and string.get('alarm_witnessed')
