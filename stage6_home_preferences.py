"""Customer-owned home display choices; no money, permissions or notification changes."""
import json

from item_registry import RegistryError, _now


SECTIONS = (
    ('sales', '売上・商品の利益', '入力した売上、確定条件を満たす利益、見込利益を分けて表示します。'),
    ('funds', '残高・精算・未払い', '購入用の実残高、精算待ちの売上、未払い料金を表示します。'),
    ('self_sales', '自分で入力した販売記録', '自己販売の売却額と発送の状況を表示します。'),
    ('inventory', '商品一覧と登録', '管理中の商品と登録への入口を表示します。'),
    ('notifications', '最近のお知らせ', 'アプリ内のお知らせを最大5件表示します。'),
    ('test_records', '検証用の資金・売上', '実際のお金とは別のテスト記録を表示します。'),
)
SECTION_IDS = tuple(section[0] for section in SECTIONS)
DEFAULT_SECTIONS = ('sales', 'funds', 'inventory', 'notifications')


class HomePreferences:
    def __init__(self, store, clock=None):
        self.store = store
        self.clock = clock or _now

    def _customer(self, actor):
        if self.store.authorize(actor):
            raise RegistryError('home_customer_required', status=403)
        self.store.authorize(actor, actor.user_id)
        return actor.user_id

    def available(self):
        # Check metadata before querying so an unapplied additive migration does
        # not abort a PostgreSQL transaction or prevent reading the old home.
        if self.store.dialect == 'postgres':
            rows = self.store.query("SELECT to_regclass('stage6_home_preferences') AS name")
        else:
            rows = self.store.query("SELECT name FROM sqlite_master WHERE type='table' AND name='stage6_home_preferences'")
        return bool(rows and rows[0]['name'])

    def get(self, actor):
        customer = self._customer(actor)
        available = self.available()
        selected = list(DEFAULT_SECTIONS)
        if available:
            rows = self.store.query('SELECT sections_json FROM stage6_home_preferences WHERE customer_id=?', (customer,))
            if rows:
                try:
                    value = json.loads(rows[0]['sections_json'])
                    if isinstance(value, list) and all(type(key) is str and key in SECTION_IDS for key in value):
                        selected = [key for key in SECTION_IDS if key in value]
                except (ValueError, TypeError):
                    pass
        return dict(selected=selected, available=available, sections=SECTIONS,
                    all_hidden=not selected)

    def save(self, actor, selected, *, source_key):
        customer = self._customer(actor)
        if (not isinstance(selected, (list, tuple)) or len(selected) > len(SECTION_IDS)
                or any(type(key) is not str or key not in SECTION_IDS for key in selected)
                or len(selected) != len(set(selected))):
            raise RegistryError('home_preferences_invalid', status=400)
        if not self.available():
            raise RegistryError('home_preferences_not_ready', status=409)
        ordered = [key for key in SECTION_IDS if key in selected]

        def action():
            self.store.execute('''INSERT INTO stage6_home_preferences(customer_id,sections_json,updated_at)
                VALUES (?,?,?) ON CONFLICT(customer_id) DO UPDATE SET
                sections_json=excluded.sections_json,updated_at=excluded.updated_at''',
                (customer, json.dumps(ordered), self.clock()))
            return {'selected': ordered}

        return self.store.operation(actor, source_key, {'home_sections': ordered}, action, customer_id=customer)
