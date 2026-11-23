"""An ordinary reporting view of stock, excluding the active synthetic sale."""

# Leave purchase values and the stock identity visible even when the current
# sale was a rehearsal. These are the fields the old reports use for a sale.
_SALE_FIELDS = frozenset(('sale_date', 'sale_price', 'sales_destination', 'sale_type',
                          'shipping_cost', 'commission', 'other_cost', 'is_shipped',
                          'status', 'selling_price'))
_PROVENANCE_TABLES = {'business_item_links', 'business_item_observations', 'stage4_channel_orders'}


def current_sale_query(item, customer, *, order_id_only=False):
    """SQL for the current sale/return event. Arguments are internal SQL expressions."""
    columns = ('test_order.order_id' if order_id_only else 'sale_event.observation_id AS event_id, '
               'sale_event.kind AS event_kind, test_order.order_id AS test_order_id')
    return f'''SELECT {columns}
        FROM business_item_observations AS sale_event
        LEFT JOIN stage4_channel_orders AS test_order
            ON sale_event.kind = 'sold'
            AND test_order.order_id = SUBSTR(sale_event.source_key, LENGTH('channel-order:') + 1)
            AND sale_event.source_key = ('channel-order:' || test_order.order_id)
            AND test_order.physical_item_id = sale_event.physical_item_id
            AND test_order.customer_id = sale_event.customer_id
            AND test_order.initial_status = 'accepted' AND test_order.mode = 'test_only'
        WHERE sale_event.physical_item_id = {item} AND sale_event.customer_id = {customer}
            AND sale_event.kind IN ('sold', 'returned', 'sale_cancelled')
        ORDER BY sale_event.occurred_at DESC, sale_event.recorded_at DESC, sale_event.observation_id DESC
        LIMIT 1'''


def ordinary_merchandise_source(connection, postgres, *, keep_stock_state=False):
    """Return a SQL table expression for authorized reporting queries.

    Old installations without the Stage4 schema keep their existing data source.
    The projection preserves all legacy columns and masks only the current test
    sale's fields. Raw inventory workflows, source rows and audit logs stay intact.
    """
    catalog = ('SELECT table_name AS name FROM information_schema.tables '
               'WHERE table_schema = current_schema() AND table_name' if postgres
               else "SELECT name FROM sqlite_master WHERE type = 'table' AND name")
    cursor = connection.cursor()
    try:
        placeholders = ', '.join(['%s' if postgres else '?'] * len(_PROVENANCE_TABLES))
        cursor.execute(f'{catalog} IN ({placeholders})', tuple(sorted(_PROVENANCE_TABLES)))
        names = {row['name'] if hasattr(row, 'keys') else row[0] for row in cursor.fetchall()}
        if not _PROVENANCE_TABLES <= names:
            return 'merchandise'
        cursor.execute('SELECT * FROM merchandise WHERE 1 = 0')
        columns = ['"' + name.replace('"', '""') + '"' for name, *_ in cursor.description]
    finally:
        cursor.close()
    fields = ', '.join(
        f'CASE WHEN sales_stock.reporting_sale_hidden THEN NULL ELSE sales_stock.{column} END AS {column}'
        if not keep_stock_state and column[1:-1] in _SALE_FIELDS else f'sales_stock.{column}'
        for column in columns)
    current_sale = current_sale_query('report_link.physical_item_id', 'report_item.user_id', order_id_only=True)
    return f'''(SELECT {fields}, sales_stock.reporting_sale_hidden
        FROM (
            SELECT report_item.*, report_item.sale_date IS NOT NULL AND EXISTS (
                SELECT 1 FROM business_item_links AS report_link
                WHERE report_link.merchandise_id = report_item.id
                  AND ({current_sale}) IS NOT NULL
            ) AS reporting_sale_hidden
            FROM merchandise AS report_item
        ) AS sales_stock)'''
