"""Common boundary for synthetic-only destructive SQLite rehearsals."""
import json
import os
from pathlib import Path
import stat
import tempfile
from item_registry import RegistryError


def assert_fixture_database(store, fixture_database):
    def deny():
        raise RegistryError('fixture_only', status=403)
    if store.dialect != 'sqlite':
        deny()
    expected=Path(os.path.abspath(fixture_database))
    temp=Path(tempfile.gettempdir()).resolve()
    if temp not in expected.parents or not expected.name.startswith('account-fixture-'):
        deny()
    def plain(path):
        try:
            value=path.lstat()
        except OSError:
            deny()
        if stat.S_ISLNK(value.st_mode) or getattr(value,'st_file_attributes',0)&0x400:
            deny()
        if stat.S_ISREG(value.st_mode) and value.st_nlink!=1:
            deny()
        return value
    for part in (expected,*expected.parents):
        plain(part)
        if part==temp:break
    databases=store.query('PRAGMA database_list')
    main=[row for row in databases if row['name']=='main']
    if len(main)!=1 or not main[0]['file'] or any(row['name'] not in ('main','temp') for row in databases):
        deny()
    if Path(main[0]['file']).resolve()!=expected.resolve():
        deny()
    if store.query("SELECT name FROM sqlite_temp_master WHERE type IN ('table','view','trigger')"):
        deny()
    marker=expected.with_suffix('.fixture.json')
    if not stat.S_ISREG(plain(marker).st_mode):
        deny()
    try:
        if json.loads(marker.read_text(encoding='utf-8'))!={'synthetic_only':True,'purpose':'account-deletion-rehearsal'}:
            deny()
    except (OSError, ValueError):
        deny()
    return expected.resolve()
