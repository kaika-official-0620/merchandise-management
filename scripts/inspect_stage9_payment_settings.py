"""Read allowlisted setting presence/mode only; no SDK, DB or network."""
import argparse
import json
import os
from pathlib import Path
import re

NAMES=('STAGE9_STRIPE_SECRET_KEY','STAGE9_STRIPE_WEBHOOK_SECRET','STAGE9_STRIPE_PUBLISHABLE_KEY',
       'STAGE9_PAYMENT_SCOPE_ID','STAGE9_PAYMENT_CUSTOMERS_JSON','STAGE9_PAYMENT_ORIGIN',
       'STAGE9_PAYMENTS_ENABLED','STAGE9_STRIPE_NETWORK_ENABLED','STAGE9_STRIPE_WRITES_ENABLED',
       'FEATURE_STRIPE_SECRET_KEY','FEATURE_STRIPE_WEBHOOK_SECRET','STRIPE_SECRET_KEY')


def statuses(values):
    result={name:'present_not_verified' if values.get(name) else 'not_present_locally' for name in NAMES}
    for name in NAMES:
        value=values.get(name,'')
        if not value: continue
        if name.endswith('SECRET_KEY'):
            result[name]='test_prefix_not_verified' if value.startswith(('sk_test_','rk_test_')) else 'live_prefix_not_usable' if value.startswith(('sk_live_','rk_live_')) else 'unrecognized_not_usable'
        elif name.endswith('PUBLISHABLE_KEY'):
            result[name]='test_prefix_not_verified' if value.startswith('pk_test_') else 'unrecognized_not_usable'
        elif name.endswith('_ENABLED'):
            result[name]='enabled' if value=='1' else 'disabled' if value=='0' else 'unrecognized'
    return result


def inspect_file(path):
    if not path.is_file(): return {'file':'not_found','settings':statuses({})}
    values={}
    try:
        # No dotenv evaluation/interpolation. Only target names; no contents in
        # exceptions/output. Quoted literals work, computed values unverified.
        for line in path.read_text(encoding='utf-8-sig').splitlines():
            found=re.match(r'^\s*(?:export\s+)?([A-Z0-9_]+)\s*=\s*(.*)$',line)
            if found and found[1] in NAMES: values[found[1]]=found[2].strip().strip('\"\'')
        return {'file':'read_allowlist_only','settings':statuses(values)}
    except (OSError,UnicodeError): return {'file':'unreadable','settings':statuses({})}


if __name__=='__main__':
    parser=argparse.ArgumentParser(); parser.add_argument('--config',type=Path,action='append',default=[])
    args=parser.parse_args()
    print(json.dumps({'current_process':statuses(os.environ),
        'explicit_files':[{'path':str(path),'result':inspect_file(path)} for path in args.config],
        'external_settings':'unknown_not_queried','network_attempted':False,
        'test_customer_approval_and_provider_metadata':'not_verified'},ensure_ascii=False,indent=2))
