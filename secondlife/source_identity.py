import hashlib
import json

def fingerprint(raw):
    """文件字节身份：对原字节做 SHA-256。重排版（空白、键序）即得到新身份。"""
    return hashlib.sha256(raw).hexdigest()

def canonical_bytes(payload):
    """规范化序列化（排序键、紧凑分隔符、UTF-8）。

    仅用于断言等价比较。原文件签名永远针对原字节，
    不得用规范化后的字节去验证原签。
    """
    return json.dumps(payload,ensure_ascii=False,sort_keys=True,separators=(',',':')).encode('utf-8')

def assertion_digest(payload):
    """业务断言身份：规范化断言的 SHA-256（含 issuer 与机构内事件号）。"""
    return hashlib.sha256(canonical_bytes(payload)).hexdigest()
