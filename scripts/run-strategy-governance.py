"""Run bounded independent governance roles; dry-run unless --execute is explicit."""
import argparse
import json
import os
from pathlib import Path
import sys

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'quant-service'))


def main():
    if hasattr(sys.stdout,'reconfigure'):sys.stdout.reconfigure(encoding='utf-8')
    if hasattr(sys.stderr,'reconfigure'):sys.stderr.reconfigure(encoding='utf-8')
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--execute',action='store_true')
    p.add_argument('--registry',default=os.getenv('STRATEGY_GOVERNANCE_ACTORS_FILE'))
    p.add_argument('--env-file',default='G:/StockPlatform/config/runtime.env')
    p.add_argument('--job-root',default='G:/StockPlatform/data/governance/jobs')
    p.add_argument('--roles',default='reviewer,proposer,designer,implementer,evaluator,validator,release_preparer')
    p.add_argument('--max-jobs',type=int,default=1)
    p.add_argument('--timeout-seconds',type=int,default=180)
    p.add_argument('--cooldown-seconds',type=int,default=3600)
    p.add_argument('--daily-jobs',type=int,default=6)
    p.add_argument('--codex-executable')
    p.add_argument('--model',default=os.getenv('QUANT_GOVERNANCE_MODEL'))
    p.add_argument('--design-context')
    p.add_argument('--measurement')
    p.add_argument('--review-context',help='JSON map: issue_id -> archived review reference (path, sha256, issue_key, status); raw legacy dicts are not executable')
    p.add_argument('--retry-unchanged',action='store_true',help='Explicit operator retry of unchanged previously failed/deferred input')
    args=p.parse_args()
    for line in Path(args.env_file).read_text(encoding='utf-8-sig').splitlines():
        if '=' in line and not line.startswith('#'):
            key,value=line.split('=',1); os.environ[key]=value
    from app.database import Database
    from app.strategy_governance.dispatcher import dispatch,Limits
    def load(path):
        return json.loads(Path(path).read_text(encoding='utf-8-sig')) if path else None
    result=dispatch(Database(),registry=args.registry or os.getenv('STRATEGY_GOVERNANCE_ACTORS_FILE'),
        job_root=args.job_root,roles=tuple(args.roles.split(',')),execute=args.execute,
        limits=Limits(args.max_jobs,args.timeout_seconds,args.cooldown_seconds,daily_jobs=args.daily_jobs),
        executable=args.codex_executable,model=args.model or os.getenv('QUANT_GOVERNANCE_MODEL'),
        design_context=load(args.design_context),measurement=load(args.measurement),review_context=load(args.review_context),
        retry_unchanged=args.retry_unchanged)
    print(json.dumps(result,ensure_ascii=False,default=str))
    if result['status']=='host_preflight_failed' or any(j['status']=='failed' for j in result.get('jobs',[])):
        raise SystemExit(2)


if __name__=='__main__':
    main()
