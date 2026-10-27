"""Read-only home sorting/filtering. Never infer a contract or a task deadline."""
from datetime import datetime, timedelta, timezone

JST = timezone(timedelta(hours=9))


def _date(value):
    try:
        result = datetime.fromisoformat(value)
        return result if result.tzinfo is not None else None
    except (TypeError, ValueError):
        return None


def local_time(value):
    value = _date(value)
    return value.astimezone(JST).strftime('%Y/%m/%d %H:%M') if value else '未確認'


def organize_tasks(tasks, items, now):
    by_id = {item['physical_item_id']: item for item in items}
    instant = _date(now)
    result = []
    for original in tasks:
        task = dict(original)
        item = by_id.get(task.get('physical_item_id'), {})
        deadline = _date(task.get('deadline_at'))
        blocked = task.get('status') == 'blocked' or task['kind'] in ('hold', 'conflict_resolution', 'channel_stop')
        overdue = bool(instant and deadline and deadline <= instant)
        soon = bool(instant and deadline and instant < deadline <= instant + timedelta(days=7))
        task.update(item_title=item.get('title') or '商品以外の確認',
            item_label=item.get('external_label') or item.get('label_code') or item.get('physical_item_id') or '',
            deadline_label=local_time(task.get('deadline_at')), overdue=overdue,
            attention='blocked' if blocked else 'overdue' if overdue else 'soon' if soon else 'normal',
            attention_label='保留・確認が必要' if blocked else '期限到来' if overdue else '7日以内' if soon else '対応待ち')
        result.append(task)
    priorities = {'blocked': 0, 'overdue': 1, 'soon': 2, 'normal': 3}
    # Unknown dates stay unknown and sort after known deadlines in the same group.
    result.sort(key=lambda task: (priorities[task['attention']],
        _date(task.get('deadline_at')) or datetime.max.replace(tzinfo=timezone.utc),
        str(task.get('physical_item_id') or ''), task['kind']))
    return result


def home_view(dashboard, query, staff):
    """Filter only an already-authorized projection, retaining complete summary counts."""
    search = str(query.get('q', '')).strip()[:100]
    selected = str(query.get('view', 'all'))
    if staff:
        choices = [('all', 'すべて'), ('attention', '保留・期限到来'), ('unassigned', '担当未設定'),
                   ('shipping', '発送'), ('registration', '検品・撮影・登録')]
        rows = dashboard['tasks']
        field = 'tasks'
    else:
        choices = [('all', 'すべて'), ('self', '自分で保管'), ('kaika', '開花へ預けている'), ('sold', '売却・発送の履歴')]
        rows = dashboard['items']
        field = 'items'
    if selected not in dict(choices):
        selected = 'all'
    filtered = []
    for row in rows:
        if search.casefold() not in ' '.join(str(row.get(key) or '') for key in
                ('title', 'item_title', 'item_label', 'external_label', 'label_code', 'physical_item_id', 'customer_id')).casefold():
            continue
        if staff:
            if selected == 'attention' and row['attention'] not in ('blocked', 'overdue'):
                continue
            if selected == 'unassigned' and row.get('assignee_user_id') is not None:
                continue
            if selected in ('shipping', 'registration') and row['kind'] != selected:
                continue
        else:
            sold = row['sales_state'] in ('sold', 'shipped')
            if selected == 'sold' and not sold:
                continue
            if selected in ('self', 'kaika') and (sold or row['custodian'] != ('customer' if selected == 'self' else 'kaika')):
                continue
        filtered.append(row)
    return dict(q=search, selected=selected, choices=choices, **{field: filtered},
                filtered_count=len(filtered), total_count=len(rows))
