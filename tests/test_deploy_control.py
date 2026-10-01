import io
import json
import sys
from unittest.mock import patch

import pytest

from app.deploy_control import main


@pytest.mark.parametrize('command,draining', [
    ('begin', True), ('status', True), ('status', False), ('end', False),
])
def test_deploy_control_accepts_expected_drain_states(command, draining, capsys):
    response = io.BytesIO(json.dumps({'submitting': 2, 'draining': draining}).encode())
    with patch.object(sys, 'argv', ['deploy_control', command]), patch('app.deploy_control.urlopen', return_value=response):
        main()
    assert capsys.readouterr().out.strip() == '2'
