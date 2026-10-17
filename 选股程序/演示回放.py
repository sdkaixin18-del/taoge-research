#!/usr/bin/env python3
"""Reproduce every bundled screen and outcome without network dependencies."""
import argparse
from pathlib import Path
import subprocess
import sys
from taoge_screen.io import read,verify


def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--data',required=True,type=Path)
    p.add_argument('--out',required=True,type=Path)
    a=p.parse_args();data=a.data.resolve();out=a.out.resolve();app=Path(__file__).resolve().parent
    subprocess.run([sys.executable,'-m','taoge_screen','replay','--universe',str(data/'universe.json'),
        '--daily',str(data/'daily.json'),'--minutes',str(data/'minutes.json'),
        '--rules',str(app/'配置/反抽规则.json'),'--freeze',str(data/'freeze.json'),'--out',str(out)],cwd=app,check=True)
    expected=read(data/'expected_hashes.json')
    for day,hashes in expected.items():
        for filename,sha in hashes.items():
            result=read(out/day/filename);verify(result)
            if result['content_sha256']!=sha:raise SystemExit('复现与封存结果不一致:'+day+'/'+filename)
    print('5个筛选日及95个验证记录全部与封存哈希一致。报告：'+str(out/'导出/报告.html'))


if __name__=='__main__':main()
