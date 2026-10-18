import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest


class ConfigTests(unittest.TestCase):
    def test_project_dotenv_and_environment_precedence(self):
        with tempfile.TemporaryDirectory() as work:
            Path(work, '.env').write_text('PGHOST=dotenv-host\nPGPASSWORD="example password"\n')
            environment = {k: v for k, v in os.environ.items() if k not in {'PGHOST', 'PGPASSWORD'}}
            check = ('import hscode.config, os; '
                     'assert os.environ["PGHOST"] == "dotenv-host"; '
                     'assert os.environ["PGPASSWORD"] == "example password"')
            subprocess.run([sys.executable, '-c', check], cwd=work, env=environment, check=True)
            environment['PGHOST'] = 'explicit-host'
            subprocess.run([sys.executable, '-c',
                            'import hscode.config, os; assert os.environ["PGHOST"] == "explicit-host"'],
                           cwd=work, env=environment, check=True)

    def test_no_dotenv_does_not_load_other_project_credentials(self):
        with tempfile.TemporaryDirectory() as work:
            environment = {k: v for k, v in os.environ.items() if k not in {'PGHOST', 'PGPASSWORD'}}
            subprocess.run([sys.executable, '-c',
                            'import hscode.config, os; assert "PGHOST" not in os.environ; '
                            'assert "PGPASSWORD" not in os.environ'],
                           cwd=work, env=environment, check=True)
