"""Reject quarantined recovery copies before the application can use a new DB.

Offline forensic inspection uses a raw, separately authorized connection. There
is deliberately no environment flag, marker deletion or activation bypass here.
"""


class RecoveryQuarantineError(RuntimeError):
    pass


def guard_new_connection(connection, *, postgres=False):
    """Only for a newly opened connection, before any application statement."""
    cursor = None
    try:
        cursor = connection.cursor()
        cursor.execute("SELECT to_regclass('public.kaika_recovery_quarantine')" if postgres else
                       "SELECT name FROM sqlite_master WHERE type='table' AND name='kaika_recovery_quarantine'")
        row = cursor.fetchone()
        if row and (next(iter(row.values())) if isinstance(row, dict) else row[0]):
            raise RecoveryQuarantineError('A quarantined recovery database cannot start the application.')
        cursor.close()
        cursor = None
        # The PostgreSQL metadata SELECT must not leave an open transaction,
        # since existing startup code configures autocommit on the returned DB.
        if postgres:
            connection.rollback()
    except Exception:
        if cursor is not None:
            try:
                cursor.close()
            except Exception:
                pass
        try:
            connection.close()
        except Exception:
            pass
        # Never include driver errors (which may expose connection details).
        raise RecoveryQuarantineError('The recovery isolation check failed; application database use is stopped.') from None
    return connection
