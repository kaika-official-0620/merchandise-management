"""Shared date rules and transactional numbering for estimates."""
from datetime import date
import re


NUMBER_FORMAT = re.compile(r'(?P<series>(?:UM-[0-9]{6}-[0-9]+|(?:KM|MT)-[0-9]{8})-)(?P<suffix>[0-9]+)')


def _field(row, key):
    return row[key] if hasattr(row, 'keys') else row[0]


def _series_user_id(series):
    if series.startswith('UM-'):
        user_id = int(series.split('-')[2])
        if user_id <= 2**63 - 1:
            return user_id
    return None


def ensure_quote_number_schema(runtime):
    """Preserve numbers from existing quotes before the app accepts deletions."""
    postgres = bool(runtime.DATABASE_URL)
    marker = '%s' if postgres else '?'
    conn = runtime.get_db()
    cur = conn.cursor()
    try:
        if postgres:
            cur.execute('SELECT pg_advisory_xact_lock(hashtext(%s))', ('quote_number_high_water:schema',))
            cur.execute("SELECT to_regclass('quote_number_high_water') IS NOT NULL AS present")
            exists = _field(cur.fetchone(), 'present')
        else:
            cur.execute('BEGIN IMMEDIATE')
            cur.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='quote_number_high_water'")
            exists = cur.fetchone() is not None
        if not exists:
            cur.execute('CREATE TABLE quote_number_high_water (series TEXT PRIMARY KEY, last_suffix TEXT NOT NULL, user_id BIGINT)')
            if postgres:
                cur.execute('LOCK TABLE user_mitsumori IN SHARE MODE')
            cur.execute('SELECT document_no FROM user_mitsumori WHERE document_no IS NOT NULL')
            highest_by_series = {}
            for row in cur:
                matched = NUMBER_FORMAT.fullmatch(_field(row, 'document_no'))
                if matched:
                    series, suffix = matched.groups()
                    highest_by_series[series] = max(highest_by_series.get(series, 0), int(suffix))
            cur.executemany(f'INSERT INTO quote_number_high_water (series, last_suffix, user_id) VALUES ({marker}, {marker}, {marker})',
                            [(series, str(suffix), _series_user_id(series)) for series, suffix in highest_by_series.items()])
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        cur.close()
        conn.close()


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
    cur.execute(f'SELECT last_suffix FROM quote_number_high_water WHERE series={marker}', (series,))
    reservation = cur.fetchone()
    highest = int(_field(reservation, 'last_suffix')) if reservation else 0
    # Include imported records, which can contain a number above the saved floor.
    cur.execute(f'SELECT document_no FROM user_mitsumori WHERE document_no LIKE {marker}', (series + '%',))
    for row in cur.fetchall():
        number = _field(row, 'document_no')
        if number and number.startswith(series):
            suffix = number[len(series):]
            if suffix.isascii() and suffix.isdecimal():
                highest = max(highest, int(suffix))
    next_suffix = highest + 1
    cur.execute(f'''INSERT INTO quote_number_high_water (series, last_suffix, user_id) VALUES ({marker}, {marker}, {marker})
        ON CONFLICT (series) DO UPDATE SET last_suffix=excluded.last_suffix''',
                (series, str(next_suffix), int(user_id) if prefix == 'UM' else None))
    return f'{series}{next_suffix:0{width}d}'
