"""等同花顺解禁后自动续抓板块成分股。

同花顺按 IP 限流: 抓太快会整站 403 (Nginx forbidden) / 502, 过一阵自己恢复。
这个脚本循环探测, 一旦能访问就跑一轮抓取, 直到没有失败/截断为止。

用法:
  python3 backend/retry_ths_crawl.py --type thshy
  python3 backend/retry_ths_crawl.py --type thshy --max-rounds 20 --wait 300
"""
import argparse
import os
import subprocess
import sys
import time
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

PROBE_URL = 'http://q.10jqka.com.cn/thshy/detail/code/881121/'
UA = ('Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 '
      '(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36')


def probe(proxy=None):
    """返回 True 表示页面可访问."""
    handlers = []
    if proxy:
        handlers.append(urllib.request.ProxyHandler({'http': proxy, 'https': proxy}))
    else:
        handlers.append(urllib.request.ProxyHandler({}))
    opener = urllib.request.build_opener(*handlers)
    try:
        resp = opener.open(urllib.request.Request(PROBE_URL, headers={'User-Agent': UA}),
                           timeout=20)
        body = resp.read().decode('gbk', errors='replace')
        return resp.status == 200 and '成分股涨跌排行榜' in body
    except Exception:  # noqa: BLE001
        return False


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--type', default='thshy', help='板块类型 (concept/thshy/zjhhy/dy)')
    ap.add_argument('--db', default=os.path.join(HERE, 'market_data.db'))
    ap.add_argument('--max-rounds', type=int, default=30)
    ap.add_argument('--wait', type=int, default=300, help='两轮之间等待秒数')
    ap.add_argument('--proxy', default=None)
    args = ap.parse_args()

    for round_no in range(1, args.max_rounds + 1):
        if not probe(args.proxy):
            print(f'[{round_no}] 同花顺还是封着 ({time.strftime("%H:%M:%S")}), '
                  f'{args.wait}s 后再试', flush=True)
            time.sleep(args.wait)
            continue
        print(f'[{round_no}] 可以访问了, 开始抓 {args.type} ({time.strftime("%H:%M:%S")})', flush=True)
        cmd = [sys.executable, os.path.join(HERE, 'fetch_ths_concepts_full.py'),
               '--type', args.type, '--db', args.db, '--delay', '0.3', '--resume']
        if args.proxy:
            cmd += ['--proxy', args.proxy]
        proc = subprocess.run(cmd, cwd=HERE)
        out = proc.stdout or ''
        tail = [ln for ln in out.splitlines() if ln.startswith('完成')]
        print(tail[-1] if tail else out[-500:], flush=True)
        if '失败 0' in out and '未抓全 0' in out:
            print('这轮没有失败也没有截断, 收工。', flush=True)
            return 0
        print('还有失败/截断, 等一会儿再来一轮。', flush=True)
        time.sleep(args.wait)
    print('达到最大轮次, 退出。', flush=True)
    return 1


if __name__ == '__main__':
    sys.exit(main())
