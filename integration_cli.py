"""Local operator entrypoint. Explicit credentials are never printed by the server."""
import argparse
import json
from pathlib import Path

from integration import ExternalEngine


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--data', default='data/games.sqlite3')
    parser.add_argument('operation', choices=('credentials', 'call', 'tick'))
    parser.add_argument('--route')
    parser.add_argument('--json', default='{}')
    args = parser.parse_args()
    Path(args.data).parent.mkdir(parents=True, exist_ok=True)
    engine = ExternalEngine(args.data)
    if args.operation == 'credentials':
        result = {'operator_token': engine.operator_token}
    elif args.operation == 'tick':
        engine.tick()
        result = {'status': 'watchdog_checked'}
    else:
        if not args.route:
            parser.error('call requires --route')
        data = json.loads(args.json)
        result = engine.handle(args.route, data | {'operator_token': engine.operator_token})
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == '__main__':
    main()
