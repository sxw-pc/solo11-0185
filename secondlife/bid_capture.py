"""离线加密报价文件的采录原语。

报价以“加密文件原字节”离线送达，见证点解密后得到报价事件。本模块只负责：
- 用买方在保证金核额登记时留下的密钥验签（HMAC-SHA256，仅依赖标准库）；
- 规范化报价内容，识别同号补传，保留每个标的的金额最高报价。

逐包最高只是采录原语：跨标的共享保证金时它不构成可执行分配，
全局分配在 ``secondlife.auction`` 中完成。
"""
import hashlib
import hmac


def highest(rows):
    """对一个标的选金额最高报价；金额并列时按买方编号取确定次序。"""
    return max(rows, key=lambda row: (row['amount'], row['bidder']))


def signing_message(bid):
    """报价签名覆盖的规范化字节：买方、标的、金额、报价编号。"""
    fields = (str(bid['bidder']), str(bid['lot_id']),
              str(bid['amount']), str(bid['bid_no']))
    return '|'.join(fields).encode('utf-8')


def verify_signature(bid, secret):
    """验签：恒定时长比较；密钥缺失或签名缺失均判失效。"""
    if not secret or not bid.get('signature'):
        return False
    digest = hmac.new(str(secret).encode('utf-8'),
                      signing_message(bid), hashlib.sha256).hexdigest()
    return hmac.compare_digest(digest, str(bid['signature']))


def parse_bid(event, secrets, witnessed_after=None):
    """把解密后的报价事件采录为一条带核验结论的报价记录。

    secrets: 买方编号 -> 核额登记密钥。
    witnessed_after: 可选的截止见证时刻（datetime），晚于该时刻为迟到件，
    迟到件照样留原字节与见证时间，但不得参与之后的分配。
    """
    bid = {
        'event_id': event['id'],
        'bid_no': event['bid_no'],
        'bidder': event['bidder'],
        'lot_id': event['lot_id'],
        'amount': event['amount'],
        'cipher_sha': event.get('cipher_sha'),
        'witnessed_at': event.get('witnessed_at'),
        'signature_valid': verify_signature(event, secrets.get(event['bidder'])),
        'late': False,
    }
    if witnessed_after is not None and bid['witnessed_at'] is not None:
        from datetime import datetime
        seen = datetime.fromisoformat(bid['witnessed_at'].replace('Z', '+00:00'))
        bid['late'] = seen > witnessed_after
    return bid


def dedupe(bids):
    """同号报价补传不得出第二单：按 bid_no 只保留首次见证的一条。

    返回 (唯一报价列表, 被识别为补传的编号列表)。同一编号内容不同仍只算补传，
    原件差异由 source_files 的 sha 留痕。
    """
    seen, unique, duplicates = set(), [], []
    for bid in sorted(bids, key=lambda b: (b['witnessed_at'] or '', b['event_id'])):
        if bid['bid_no'] in seen:
            duplicates.append(bid['bid_no'])
            continue
        seen.add(bid['bid_no'])
        unique.append(bid)
    return unique, duplicates
