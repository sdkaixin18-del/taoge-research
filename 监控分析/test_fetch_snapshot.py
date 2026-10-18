import json
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from fetch_snapshot import fetch_snapshot, save_snapshot


class FetchTest(unittest.TestCase):
    def test_windows_path_with_spaces_and_utf8(self):
        payload={'schema':'cb-research-snapshot-v2','name':'保隆转债'}
        with patch('fetch_snapshot.subprocess.run') as run:
            run.return_value=subprocess.CompletedProcess([],0,json.dumps(payload,ensure_ascii=False),'')
            data=fetch_snapshot(Path('C:/Users/Test User/.ssh/config'),'market-host')
            self.assertEqual(data['name'],'保隆转债')
            command=run.call_args.args[0]
            self.assertEqual(command[2],'C:/Users/Test User/.ssh/config')
            self.assertEqual(run.call_args.kwargs['encoding'],'utf-8')
            self.assertNotIn('shell',run.call_args.kwargs)

    def test_failed_read_does_not_replace_valid_local_snapshot(self):
        with tempfile.TemporaryDirectory() as tmp:
            path=Path(tmp)/'行情.json';save_snapshot({'valid':'旧数据'},path)
            with patch('fetch_snapshot.subprocess.run') as run:
                run.return_value=subprocess.CompletedProcess([],255,'','connection failed')
                with self.assertRaises(RuntimeError):
                    save_snapshot(fetch_snapshot(Path('config'),'market-host'),path)
            self.assertEqual(json.loads(path.read_text(encoding='utf-8')),{'valid':'旧数据'})

    def test_wrong_schema_is_rejected(self):
        with patch('fetch_snapshot.subprocess.run') as run:
            run.return_value=subprocess.CompletedProcess([],0,'{"schema":"old"}','')
            with self.assertRaises(ValueError):fetch_snapshot(Path('config'),'market-host')

    def test_host_cannot_be_ssh_option(self):
        with patch('fetch_snapshot.subprocess.run') as run:
            with self.assertRaises(ValueError):fetch_snapshot(Path('config'),'-oProxyCommand=other')
            run.assert_not_called()


if __name__=='__main__':unittest.main()
