"""Explicit tax-inclusive STEP4 breakdowns; never infer tax from legacy zeroes."""
from collections import defaultdict


TAX_CATEGORIES = {
    'unknown': '未確認',
    '10': '10%（標準税率）',
    '8': '8%（軽減税率）',
    'exempt': '非課税',
    'outside': '対象外',
}
STEP4_SOURCE = 'step4_client_outgoing'


def normalize_tax_category(value):
    category = str(value or 'unknown').strip()
    if category not in TAX_CATEGORIES:
        raise ValueError('税区分は10%、8%、非課税、対象外、未確認から選択してください。')
    return category


def inclusive_breakdown(items):
    """Round once per invoice/rate, using integer yen and floor (not per line)."""
    amounts = defaultdict(int)
    categories = set()
    for item in items:
        category = normalize_tax_category(item.get('tax_category'))
        amount = int(item.get('amount', item.get('client_payment_amount', 0)) or 0)
        if amount < 0:
            raise ValueError('顧客向け支払額は0円以上で入力してください。')
        amounts[category] += amount
        categories.add(category)
    return {
        'amounts': dict(amounts),
        'categories': categories,
        'total': sum(amounts.values()),
        'tax_8': amounts['8'] * 8 // 108,
        'tax_10': amounts['10'] * 10 // 110,
        'confirmed': bool(categories) and 'unknown' not in categories,
    }


def invoice_tax_context(invoice, items):
    invoice = dict(invoice or {})
    if invoice.get('source_workflow_step') != STEP4_SOURCE:
        return None
    total = int(invoice.get('total_amount') or 0)
    legacy_message = '税区分・税額内訳は未確認です。支払合計は保存済みの金額です。'
    unconfirmed = {'confirmed': False, 'legacy': True, 'rows': [{'label': '税額内訳', 'amount': None}],
                   'message': legacy_message, 'total': total}
    if int(invoice.get('tax_breakdown_version') or 0) != 1:
        return unconfirmed
    try:
        result = inclusive_breakdown([dict(item) for item in items])
    except (ValueError, TypeError):
        return unconfirmed
    if (invoice.get('tax_rounding_method') != 'floor' or result['total'] != total or
            result['tax_8'] != int(invoice.get('tax_amount_8') or 0) or
            result['tax_10'] != int(invoice.get('tax_amount_10') or 0)):
        return {**unconfirmed, 'message': '税額内訳の保存内容に確認が必要です。支払合計は保存済みの金額です。'}
    rows = []
    for rate in ('10', '8'):
        if rate in result['categories']:
            rows.append({'label': rate + '%対象（税込）', 'amount': result['amounts'][rate]})
            rows.append({'label': 'うち消費税等（' + rate + '%）', 'amount': result['tax_' + rate]})
    for category in ('exempt', 'outside', 'unknown'):
        if category in result['categories']:
            rows.append({'label': TAX_CATEGORIES[category] + 'の金額', 'amount': result['amounts'][category]})
    message = '支払額に含まれる消費税等を、書類全体の税率別合計から計算しています（1円未満切捨て）。'
    if not result['confirmed']:
        message += ' 未確認の明細には税率を適用していません。'
    return {'confirmed': result['confirmed'], 'legacy': False, 'rows': rows, 'message': message, 'total': total}


def invoice_tax_category_label(invoice, item):
    invoice, item = dict(invoice or {}), dict(item or {})
    if invoice.get('source_workflow_step') == STEP4_SOURCE:
        if int(invoice.get('tax_breakdown_version') or 0) != 1:
            return '未確認'
        return TAX_CATEGORIES.get(str(item.get('tax_category') or ''), '未確認')
    return str(item.get('tax_category') or '10') + '%'
