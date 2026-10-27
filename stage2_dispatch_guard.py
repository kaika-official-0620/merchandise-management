"""Prevent returning a physical item while recorded external listings remain live.

Read only: the optional commerce tables are inspected inside the caller's item
transaction. No marketplace request, automatic stop or migration is performed.
"""
from item_registry import RegistryError, _time


def assert_return_listings_stopped(registry, item_id, occurred_at):
    if registry.dialect == 'postgres':
        rows = registry.query("SELECT to_regclass('stage4_channel_listings') AS name")
    else:
        rows = registry.query("SELECT name FROM sqlite_master WHERE type='table' AND name='stage4_channel_listings'")
    if not rows or not rows[0]['name']:
        return  # No recorded commerce schema; do not manufacture an integration.
    listings = registry.query('SELECT listing_id FROM stage4_channel_listings WHERE physical_item_id=?', (item_id,))
    if listings:
        if registry.dialect == 'postgres':
            tables = registry.query("SELECT to_regclass('stage4_stop_tasks') AS tasks,to_regclass('stage4_stop_events') AS events")
            ready = tables and tables[0]['tasks'] and tables[0]['events']
        else:
            tables = registry.query("SELECT name FROM sqlite_master WHERE type='table' AND name IN ('stage4_stop_tasks','stage4_stop_events')")
            ready = len(tables) == 2
        if not ready:
            raise RegistryError('return_listing_stop_schema_required',
                '出品停止の確認記録が未準備です。記録と移行状態を確認するまで返送を保留してください。', 409)
    for listing in listings:
        tasks = registry.query('SELECT task_id FROM stage4_stop_tasks WHERE listing_id=?', (listing['listing_id'],))
        if not tasks:
            raise RegistryError('return_listing_stop_unconfirmed',
                '返送前に「販路・受注・発送」でこの商品の全出品を停止確認してください。', 409)
        for task in tasks:
            latest = registry.query('''SELECT status,occurred_at FROM stage4_stop_events
                WHERE task_id=? ORDER BY revision DESC LIMIT 1''', (task['task_id'],))
            if not latest or latest[0]['status'] != 'confirmed_stopped':
                raise RegistryError('return_listing_stop_unconfirmed',
                    '外部出品の停止が未確認です。停止失敗・対応中の販路を確認してから返送してください。', 409)
            if _time(occurred_at) < _time(latest[0]['occurred_at']):
                raise RegistryError('return_before_listing_stop_confirmation',
                    '全販路の出品停止を確認した後の返送発送日時を記録してください。', 409)
