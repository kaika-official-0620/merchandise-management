"""Run isolated payment tests with socket connections forbidden; save evidence."""
import argparse
from datetime import datetime,timezone
import hashlib
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT)); sys.path.insert(0,str(ROOT/'tests'))


if __name__=='__main__':
    parser=argparse.ArgumentParser(); parser.add_argument('--output',type=Path)
    args=parser.parse_args()
    output=args.output or Path(tempfile.mkdtemp(prefix='kaika-stage9-evidence-'))/'payments.json'
    suite=unittest.TestSuite()
    for pattern in ('test_stage5*.py','test_stage9_payments.py'):
        suite.addTests(unittest.defaultTestLoader.discover(str(ROOT/'tests'),pattern=pattern))
    with patch('socket.socket.connect',side_effect=AssertionError('External network forbidden in isolated tests')), \
         patch('socket.create_connection',side_effect=AssertionError('External network forbidden in isolated tests')):
        result=unittest.TextTestRunner(verbosity=1).run(suite)
    paths=list(ROOT.glob('stage9*.py'))+[ROOT/'stage5_payments.py',ROOT/'stage5_stripe_sandbox.py',ROOT/'account_lifecycle.py',ROOT/'requirements-stage9-sandbox.txt']
    paths+=list((ROOT/'tests').glob('test_stage5*.py'))+[ROOT/'tests/test_stage9_payments.py']
    paths+=list((ROOT/'templates/sandbox_payments').glob('*.html'))
    paths+=[Path(__file__),ROOT/'scripts/inspect_stage9_payment_settings.py']
    try:
        import stripe
        sdk_version=stripe.VERSION
    except ImportError: sdk_version='unavailable'
    evidence={'executed_at_utc':datetime.now(timezone.utc).isoformat(),'test_environment':'disposable SQLite fixtures / Flask test_client / local SDK HTTP transport',
        'tests':result.testsRun,'failures':len(result.failures),'errors':len(result.errors),'skipped':len(result.skipped),
        'success':result.wasSuccessful(),'stripe_sdk':sdk_version,'network_connections_allowed':False,
        'external_stripe_test':'not_executed','actual_cards_or_money':'not_used','production_config_changed':False,
        'postgres_new_schema':'not_executed','real_3ds_browser':'not_executed',
        'source_sha256':{str(path.relative_to(ROOT)).replace('\\','/'):hashlib.sha256(path.read_bytes()).hexdigest() for path in sorted(set(paths))}}
    output.parent.mkdir(parents=True,exist_ok=True); output.write_text(json.dumps(evidence,ensure_ascii=False,indent=2)+'\n',encoding='utf-8')
    print(json.dumps({'evidence':str(output.resolve()),'tests':result.testsRun,'success':result.wasSuccessful()},ensure_ascii=False))
    raise SystemExit(0 if result.wasSuccessful() else 1)
