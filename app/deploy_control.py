"""Control the submission drain from inside the running service container."""

import json
import sys
from urllib.request import Request, urlopen

from app.config import Settings


def main():
    methods = {'begin': 'POST', 'status': 'GET', 'end': 'DELETE'}
    if len(sys.argv) != 2 or sys.argv[1] not in methods:
        raise SystemExit('usage: python -m app.deploy_control {begin|status|end}')
    request = Request(
        'http://127.0.0.1:8797/internal/deploy/drain',
        method=methods[sys.argv[1]],
        headers={'X-Deploy-Token': Settings().admin_token},
    )
    with urlopen(request, timeout=10) as response:
        data = json.load(response)
    if data['draining'] != (sys.argv[1] != 'end'):
        raise RuntimeError('unexpected deployment drain state')
    print(int(data['submitting']))


if __name__ == '__main__':
    main()
