"""Portable dry-run first CLI; --apply never migrates, seeds tenants or approvals."""
import argparse
import json
from pathlib import Path

from ai.evaluation.demo_import import Target, offline_plan, backend_adapters, run_import


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--bundle',type=Path,required=True)
    parser.add_argument('--target',type=Path)
    parser.add_argument('--backend-root',type=Path)
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument('--dry-run',action='store_true')
    mode.add_argument('--check-target',action='store_true',help='Read-only explicit DB/S3 preflight')
    mode.add_argument('--apply',action='store_true')
    parser.add_argument('--acknowledge-demo-target',action='store_true')
    parser.add_argument('--enqueue',action='store_true')
    parser.add_argument('--retry-failed',action='store_true')
    parser.add_argument('--output',type=Path,help='Private reconciliation JSON OUTSIDE the frozen bundle')
    args = parser.parse_args()
    if args.output and args.output.resolve().is_relative_to(args.bundle.resolve()):
        parser.error('Reconciliation output must be outside the frozen handoff')
    if args.retry_failed and not (args.apply and args.enqueue):
        parser.error('--retry-failed requires --apply --enqueue')
    target = Target.model_validate_json(args.target.read_text()) if args.target else None
    if args.apply or args.check_target:
        if target is None or args.backend_root is None or not args.acknowledge_demo_target:
            parser.error('Target file, backend root and --acknowledge-demo-target are required')
        target.require_complete()
        # Validate all physical assets before touching either DB or storage.
        offline_plan(args.bundle,target)
        connect, storage, attempts = backend_adapters(args.backend_root,target,enqueue=args.enqueue and args.apply)
        result = run_import(args.bundle,target,connect,storage,attempts,dry_run=not args.apply,
                            enqueue=args.enqueue,retry_failed=args.retry_failed)
    else:
        if args.enqueue:
            parser.error('Offline dry runs cannot enqueue')
        result = offline_plan(args.bundle,target)
    encoded = json.dumps(result,indent=2) + '\n'
    if args.output:
        args.output.parent.mkdir(parents=True,exist_ok=True)
        args.output.write_text(encoded,encoding='utf-8')
    else:
        print(encoded)
    return 1 if result.get('failed') else 0


if __name__ == '__main__':
    try:
        raise SystemExit(main())
    except Exception as error:
        code = str(error) if isinstance(error,ValueError) and str(error).isupper() else type(error).__name__
        print(json.dumps({'status':'FAILED','error_code':code}))
        raise SystemExit(2) from None
