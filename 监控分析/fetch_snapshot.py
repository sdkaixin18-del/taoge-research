"""Read the independent server collector over existing SSH; save only locally."""
import argparse
import json
import subprocess
import re
from pathlib import Path

REMOTE_COMMAND='~/apps/cb-limitup-monitor/.venv/bin/python ~/apps/cb-market-observer/market_observer.py --snapshot'


def fetch_snapshot(ssh_config, host, ssh_binary='ssh'):
    if not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.-]*',host):
        raise ValueError('ssh_host must be an existing SSH host alias')
    r=subprocess.run([ssh_binary,'-F',str(Path(ssh_config).expanduser()),'-o','BatchMode=yes','-o','ConnectTimeout=10',
                      host,REMOTE_COMMAND],capture_output=True,text=True,encoding='utf-8',timeout=45)
    if r.returncode:raise RuntimeError('server_snapshot_failed: '+r.stderr[-400:])
    data=json.loads(r.stdout)
    if data.get('schema')!='cb-research-snapshot-v2':raise ValueError('unexpected_snapshot_schema')
    return data


def save_snapshot(data, path):
    path.parent.mkdir(parents=True,exist_ok=True)
    tmp=path.with_suffix('.tmp')
    tmp.write_text(json.dumps(data,ensure_ascii=False,indent=2)+'\n',encoding='utf-8')
    tmp.replace(path)


def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--out',type=Path,required=True)
    p.add_argument('--config',type=Path,default=Path(__file__).with_name('client_config.local.json'))
    p.add_argument('--ssh-config',type=Path)
    p.add_argument('--ssh-host')
    args=p.parse_args()
    cfg=json.loads(args.config.read_text(encoding='utf-8-sig')) if args.config.exists() else {}
    try:
        data=fetch_snapshot(args.ssh_config or cfg.get('ssh_config',Path.home()/'.ssh/config'),
                            args.ssh_host or cfg.get('ssh_host','cb-monitor-shanghai'),cfg.get('ssh_binary','ssh'))
    except (OSError,RuntimeError,ValueError,subprocess.TimeoutExpired) as exc:
        raise SystemExit(str(exc)) from exc
    save_snapshot(data,args.out)
    print(json.dumps({k:data.get(k) for k in ['generated_at','quote_day','mode','coverage','notifications_sent','news_stored']},ensure_ascii=False))


if __name__=='__main__':main()
