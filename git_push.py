import subprocess, os, sys

repo = os.path.dirname(os.path.abspath(__file__))
print(f"Repo: {repo}")

lock = os.path.join(repo, '.git', 'index.lock')
if os.path.exists(lock):
    os.remove(lock)
    print("Lock removed")

def run(cmd):
    r = subprocess.run(cmd, cwd=repo, capture_output=True, text=True, encoding='utf-8')
    print(f"$ {' '.join(cmd)}")
    if r.stdout.strip(): print(r.stdout.strip())
    if r.stderr.strip(): print(r.stderr.strip())
    return r.returncode

run(['git', 'add', '-A'])

msg = """Update fee structure to tiered fixed amounts with plan names

- Monthly fees: Light/Standard/Pro/Business/Enterprise plans
- Photo packing: 4-tier fixed fees (500/650/800/1000 yen)
- Wholesale: 5-tier fixed fees (500/1000/1500/2000/3000 yen)
- Multi-listing: 3-tier fixed fees (300/400/500 yen)
- Auction: 4-tier fixed fees (300/400/500/700 yen)
- Replace percentage-based commission with price-range tiers
- Update master settings UI, saleTypeRules, and all fee tables"""

run(['git', 'commit', '-m', msg])

run(['git', 'push', 'origin', 'master'])

run(['git', 'checkout', 'main'])
run(['git', 'merge', 'master'])
run(['git', 'push', 'origin', 'main'])
run(['git', 'checkout', 'master'])
