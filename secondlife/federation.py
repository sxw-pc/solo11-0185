from .source_identity import fingerprint

def distinct_sources(blobs):
    return list(dict.fromkeys(fingerprint(blob) for blob in blobs))
