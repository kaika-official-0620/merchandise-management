"""Explicit bridge from authenticated native registration to Stage6 records.

Registration performs no I/O. Enabled callbacks use existing database connections
only; they never enqueue legacy push events, deliver, discover secrets or migrate.
"""
from contextlib import contextmanager
import hmac
import re
import uuid

from item_registry import Actor, Registry, RegistryError
from stage6_common import Stage6Store
from stage6_notifications import NotificationService, digest


class Stage6PushBridge:
    def __init__(self, app, runtime, push):
        self.app, self.runtime, self.push = app, runtime, push

    def enabled(self):
        return self.app.config.get('STAGE6_ENABLED') is True

    @contextmanager
    def service(self):
        connection = self.runtime.get_db()
        try:
            if not self.push.postgres:
                connection.execute('PRAGMA foreign_keys=ON')
            registry = Registry(connection, 'postgres' if self.push.postgres else 'sqlite')
            yield NotificationService(Stage6Store(registry), clock=self.push.clock,
                                      destination_validator=self.destination_valid)
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    def _device(self, user_id, installation, binding=None):
        if not self.enabled() or type(user_id) is not int or not isinstance(installation, str):
            return None
        connection = self.runtime.get_db()
        try:
            registry = Registry(connection, 'postgres' if self.push.postgres else 'sqlite')
            rows = registry.query('''SELECT * FROM push_devices WHERE installation=? AND user_id=?
                AND enabled=1 AND expires_at>? AND project_id=?''',
                (installation, user_id, int(self.push.clock()), self.push.setting('EAS_PROJECT_ID')))
            if not rows:
                return None
            device = rows[0]
            if binding is not None and not hmac.compare_digest(device['binding'], binding):
                return None
            return device
        except Exception:
            # Missing schema or a stale registration cannot authorize delivery.
            return None
        finally:
            connection.close()

    def destination_valid(self, destination):
        if not isinstance(destination, dict) or destination.get('channel') != 'push':
            return False
        device = self._device(destination.get('customer_id'), destination.get('installation'))
        return bool(device and isinstance(destination.get('binding_hash'), str)
                    and isinstance(destination.get('address'), str)
                    and hmac.compare_digest(digest(device['binding']), destination['binding_hash'])
                    and hmac.compare_digest(device['token'], destination['address'])
                    and device['platform'] == destination.get('platform')
                    and destination.get('revoked_at') is None)

    @staticmethod
    def source_key(action):
        return 'push-bridge:' + action + ':' + uuid.uuid4().hex

    def register(self, user_id, token, installation, binding, platform):
        device = self._device(user_id, installation, binding)
        if not device or not hmac.compare_digest(device['token'], token) or device['platform'] != platform:
            raise RegistryError('push_registration_not_current', status=409)
        with self.service() as service:
            return service.register_push(Actor(user_id), token, installation, binding, platform,
                                         source_key=self.source_key('register'))

    def revoke_binding(self, user_id, binding):
        if not self.enabled():
            return None
        with self.service() as service:
            return service.revoke_push_binding(Actor(user_id), binding, source_key=self.source_key('revoke-binding'))

    def revoke_installation(self, user_id, installation):
        if not self.enabled():
            return None
        with self.service() as service:
            return service.revoke_push_installation(Actor(user_id), installation, source_key=self.source_key('revoke-installation'))

    def deactivate_legacy_destination(self, store, actor, destination_id):
        """Disable the exact native registration in the caller's transaction.

        Web revocation must clear both records before a native status refresh.
        Never disable a replacement token, another login binding or another
        customer's installation using an old destination record.
        """
        if not self.enabled():
            raise RegistryError('stage6_disabled', status=409)
        store.authorize(actor, actor.user_id)
        rows = store.query('SELECT * FROM stage6_destinations WHERE destination_id=? AND customer_id=?',
                           (destination_id, actor.user_id))
        if not rows:
            raise RegistryError('notification_destination_not_found', status=404)
        destination = rows[0]
        if destination['channel'] != 'push':
            return {'disabled': False}
        devices = store.query('''SELECT * FROM push_devices WHERE installation=? AND user_id=?
            AND token=? AND platform=? AND project_id=?''',
            (destination['installation'], actor.user_id, destination['address'], destination['platform'], self.push.setting('EAS_PROJECT_ID')))
        if not devices or not hmac.compare_digest(digest(devices[0]['binding']), destination['binding_hash']):
            return {'disabled': False}
        device = devices[0]
        store.execute('''UPDATE push_devices SET enabled=0 WHERE installation=? AND user_id=?
            AND token=? AND binding=?''', (device['installation'], actor.user_id, device['token'], device['binding']))
        return {'disabled': True}

    def resolve_open(self, user_id, notification_id, installation, binding):
        if not isinstance(notification_id, str) or not re.fullmatch('[a-f0-9]{32}', notification_id):
            return None
        if not self._device(user_id, installation, binding):
            return None
        with self.service() as service:
            # Always navigate via the private notice. That screen revalidates the
            # current business source before exposing its detail destination.
            notice = service.get_notification(Actor(user_id), notification_id)
            return '/notifications/' + notice['notification_id']


def register_stage6_push_bridge(app, runtime):
    if 'stage6_push_bridge' in app.extensions:
        return app.extensions['stage6_push_bridge']
    push = app.extensions.get('push_notifications')
    if push is None:
        raise ValueError('Register authenticated push notifications first')
    bridge = Stage6PushBridge(app, runtime, push)
    app.extensions['stage6_push_bridge'] = bridge
    return bridge
