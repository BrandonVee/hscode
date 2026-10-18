import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest


class ConfigTests(unittest.TestCase):
    def test_project_dotenv_and_environment_precedence(self):
        with tempfile.TemporaryDirectory() as work:
            Path(work, '.env').write_text('DATABASE_URL="postgresql://user:p%40ss@dotenv-host/db"\n')
            environment = {k: v for k, v in os.environ.items() if k != 'DATABASE_URL'}
            subprocess.run([sys.executable, '-c',
                            'from hscode.config import DEFAULT_DSN; '
                            'assert DEFAULT_DSN == "postgresql://user:p%40ss@dotenv-host/db"'],
                           cwd=work, env=environment, check=True)
            environment['DATABASE_URL'] = 'postgresql://explicit-host/db'
            subprocess.run([sys.executable, '-c',
                            'from hscode.config import DEFAULT_DSN; '
                            'assert DEFAULT_DSN == "postgresql://explicit-host/db"'],
                           cwd=work, env=environment, check=True)

    def test_no_url_does_not_fall_back_to_pg_fields(self):
        with tempfile.TemporaryDirectory() as work:
            environment = {k: v for k, v in os.environ.items() if k != 'DATABASE_URL'}
            environment['PGHOST'] = 'legacy-host'
            subprocess.run([sys.executable, '-c',
                            'from hscode.config import DEFAULT_DSN; assert DEFAULT_DSN == ""; '
                            'from hscode.server import connect; '
                            '\ntry: connect()\nexcept ValueError: pass\nelse: raise AssertionError("missing URL accepted")'],
                           cwd=work, env=environment, check=True)
