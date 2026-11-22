import hashlib

def fingerprint(raw):
    return hashlib.sha256(raw).hexdigest()
