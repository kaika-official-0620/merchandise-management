"""Custody boundaries for existing Kaika inventory fees (no price changes)."""


def is_kaika_fee_item(item):
    # Old rows predate custody tracking and retain their original treatment.
    return item.get('custody_location') in (None, 'kaika')


def is_kaika_fee_client(user, kaika_user_ids):
    """Exclude self-only app users from projected legacy inventory revenue.

    A zero-item legacy contract still uses the existing minimum tariff.
    """
    return bool(user.get('id') in kaika_user_ids or user.get('stripe_subscription_id') or
                user.get('subscription_status') in ('active', 'past_due', 'canceling'))
