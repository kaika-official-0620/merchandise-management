"""Shared date rules and transactional numbering for estimates."""
from datetime import date


def validate_quote_dates(issue_date, valid_until):
    def parse(raw, label):
        value = str(raw or '')
        try:
            parsed = date.fromisoformat(value)
        except ValueError:
            raise ValueError(f'{label}を日付で入力してください。') from None
        if value != parsed.isoformat():
            raise ValueError(f'{label}を日付で入力してください。')
        return parsed

    issued = parse(issue_date, '発行日')
    if valid_until and parse(valid_until, '有効期限') < issued:
        raise ValueError('有効期限は発行日以降の日付を入力してください。')


def allocate_quote_number(conn, cur, now, *, postgres, user_id=None, prefix='UM'):
    """Read and reserve the next suffix in the transaction that writes the quote."""
    if prefix == 'UM':
        series = f'UM-{now:%Y%m}-{int(user_id)}-'
        width = 4
    else:
        series = f'{prefix}-{now:%Y%m%d}-'
        width = 3
    if postgres:
        cur.execute('SELECT pg_advisory_xact_lock(hashtext(%s))', ('user_mitsumori:' + series,))
    elif not conn.in_transaction:
        cur.execute('BEGIN IMMEDIATE')

    marker = '%s' if postgres else '?'
    cur.execute(f'SELECT document_no FROM user_mitsumori WHERE document_no LIKE {marker}', (series + '%',))
    highest = 0
    for row in cur.fetchall():
        number = row['document_no'] if hasattr(row, 'keys') else row[0]
        if number and number.startswith(series):
            suffix = number[len(series):]
            if suffix.isascii() and suffix.isdecimal():
                highest = max(highest, int(suffix))
    return f'{series}{highest + 1:0{width}d}'
