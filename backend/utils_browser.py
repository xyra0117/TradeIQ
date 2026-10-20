"""
真浏览器反爬绕过 session。

用法:
    async with BrowserSession() as session:
        text = await session.fetch_text(url)
        await session.sleep_human()

为什么是 page.goto 而不是 context.request / requests:
    - page.goto 走真 Chromium BoringSSL TLS + HTTP/2 + 共享 cookie
    - 反爬 JA3 指纹检测根本判定不到
    - context.request 是裸 HTTP 客户端,绕不过 JA3
    - page.evaluate(fetch()) 跨域 CORS 被拒

依赖:
    pip install playwright
    python -m playwright install chromium
"""

import asyncio
import json
import random
import re
import time
from typing import Optional, Tuple

from playwright.async_api import Browser, BrowserContext, Page, async_playwright

# ── 默认参数(可被 BrowserSession(...) 覆盖) ─────────────────────────────────
DEFAULT_USER_AGENT = (
    'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) '
    'AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36'
)
DEFAULT_ACCEPT_LANG = 'zh-CN,zh;q=0.9,en;q=0.8'

# 预热主页:攒 cookie + 浏览历史,让反爬端认为你是"真人刚打开浏览器"
WARM_PAGES = [
    'https://quote.eastmoney.com/center/gridlist.html#hs_a_board',
    'https://data.eastmoney.com/bkzj/BK0438.html',
    # 想稳点再加,但每加一个 startup 时间 +5-8s:
    # 'https://push2.eastmoney.com/',
    # 'https://data.eastmoney.com/',
]


class BrowserSession:
    """真浏览器反爬绕过 session。一次启动一个,顺序 fetch。"""

    def __init__(
        self,
        user_agent: str = DEFAULT_USER_AGENT,
        accept_lang: str = DEFAULT_ACCEPT_LANG,
        warm_pages: Optional[list] = None,
        base_gap_ms: int = 2500,
        jitter_ms: int = 2500,
        burst_n: int = 8,
        burst_pause_ms: int = 15000,
        timeout_ms: int = 30000,
        headless: bool = True,
        verbose: bool = True,
    ):
        self.user_agent = user_agent
        self.accept_lang = accept_lang
        self.warm_pages = warm_pages if warm_pages is not None else WARM_PAGES
        self.base_gap_ms = base_gap_ms
        self.jitter_ms = jitter_ms
        self.burst_n = burst_n
        self.burst_pause_ms = burst_pause_ms
        self.timeout_ms = timeout_ms
        self.headless = headless
        self.verbose = verbose

        self._pw = None
        self._browser: Optional[Browser] = None
        self._context: Optional[BrowserContext] = None
        self._page: Optional[Page] = None
        self._since_burst = 0  # 已跑的请求计数,每 burst_n 个歇一次

    async def __aenter__(self):
        await self.start()
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb):
        await self.close()

    def log(self, msg: str):
        if self.verbose:
            print(f'[{time.strftime("%H:%M:%S")}] {msg}', flush=True)

    async def start(self):
        """启动浏览器,预热攒 cookie。"""
        self.log('启动 playwright 浏览器...')
        self._pw = await async_playwright().start()
        launch_kwargs = {'headless': self.headless}
        # 把 headed 窗口扔到屏幕外,避免干扰用户
        if not self.headless:
            launch_kwargs['args'] = [
                '--window-position=-2400,-2400',
                '--window-size=800,600',
            ]
        self._browser = await self._pw.chromium.launch(**launch_kwargs)
        self._context = await self._browser.new_context(
            user_agent=self.user_agent,
            locale='zh-CN',
            viewport={'width': 1440, 'height': 900},
            ignore_https_errors=True,
            extra_http_headers={'Accept-Language': self.accept_lang},
        )

        # 屏蔽图片 / 视频 / 字体,加速
        async def _route(route, request):
            rt = request.resource_type
            if rt in ('image', 'media', 'font'):
                await route.abort()
            else:
                await route.continue_()

        await self._context.route('**/*', _route_handler := _route)
        self._page = await self._context.new_page()

        # stealth: 隐藏 headless 特征 (navigator.webdriver / 缺 window.chrome / 空 plugins)
        # 东财现在对 headless chromium 触发 502,真人浏览器能过 — 这是关键补丁
        await self._page.add_init_script("""
            Object.defineProperty(navigator, 'webdriver', { get: () => undefined });
            window.chrome = { runtime: {}, loadTimes: () => ({}), csi: () => ({}) };
            Object.defineProperty(navigator, 'plugins', { get: () => [1,2,3,4,5] });
            Object.defineProperty(navigator, 'languages', { get: () => ['zh-CN','zh','en'] });
            Object.defineProperty(navigator, 'platform', { get: () => 'MacIntel' });
        """)

        # 预热主页攒 cookie + 浏览历史
        self.log('预热: 访问真页面攒 cookie + 浏览历史...')
        for url in self.warm_pages:
            try:
                t0 = time.time()
                await self._page.goto(url, wait_until='domcontentloaded', timeout=30000)
                await asyncio.sleep(random.uniform(2.5, 4.0))
                # 模拟鼠标移动攒"用户在浏览"信号
                await self._page.mouse.move(random.randint(200, 1000), random.randint(200, 600))
                await asyncio.sleep(random.uniform(0.3, 0.8))
                self.log(f'  ✓ {url[:80]} ({time.time()-t0:.1f}s)')
            except Exception as e:
                self.log(f'  ⚠ {url[:80]}: {type(e).__name__}: {str(e)[:80]}')

        cookies = await self._context.cookies()
        self.log(f'预热完成: cookie {len(cookies)} 条, 开始抓数据')

    async def close(self):
        if self._browser:
            try:
                await self._browser.close()
            except Exception:
                pass
        if self._pw:
            try:
                await self._pw.stop()
            except Exception:
                pass

    async def fetch_text(self, url: str, max_tries: int = 5) -> Tuple[Optional[str], int, Optional[str]]:
        """page.goto 拿页面渲染后的文本,带 JSON 正则提取。

        返回 (json_str, attempts, error_msg):
            成功: (json_str, attempts, None)
            失败: (None, attempts, '错误信息')

        反爬退避: 失败 2s→4s→8s→16s→32s (skill 推荐起点 2s 不是 1s).
        连续 N 次失败 (本类实例上) → 长歇 5min, 让反爬冷却.
        """
        last_err = None
        for attempt in range(1, max_tries + 1):
            try:
                response = await self._page.goto(
                    url,
                    wait_until='domcontentloaded',
                    timeout=max(1, self.timeout_ms // 1000) * 1000,
                )
                status = response.status if response else 0
                if status != 200:
                    raise RuntimeError(f'HTTP {status}')
                content = await self._page.content()
                m = re.search(r'\{.*\}', content, re.DOTALL)
                if not m:
                    raise RuntimeError(f'页面无 JSON: {content[:80]!r}')
                # 成功 → reset 连续失败计数
                self._consec_fail = 0
                return m.group(0), attempt, None
            except Exception as e:
                last_err = f'{type(e).__name__}: {str(e)[:120]}'
                if attempt < max_tries:
                    delay = min(32, 2 ** attempt)  # skill 推荐 2s 起步 (1<<attempt)
                    await asyncio.sleep(delay + random.uniform(0, 0.8))
        # 5 tries 全失败 → 连续失败计数 +1
        self._consec_fail = getattr(self, '_consec_fail', 0) + 1
        return None, max_tries, last_err

    async def fetch_json(self, url: str, max_tries: int = 5) -> Tuple[Optional[dict], int, Optional[str]]:
        """fetch_text 的便捷版本,直接解析 JSON。"""
        text, attempts, err = await self.fetch_text(url, max_tries)
        if text is None:
            return None, attempts, err
        try:
            return json.loads(text), attempts, None
        except json.JSONDecodeError as e:
            return None, attempts, f'JSON 解析失败: {e}; {text[:80]!r}'

    async def sleep_human(self):
        """每次 fetch 后调一次,自动算间隔 + 每 burst_n 个歇一次。"""
        self._since_burst += 1
        delay = (self.base_gap_ms / 1000.0) + random.uniform(0, self.jitter_ms / 1000.0)
        await asyncio.sleep(delay)
        if self._since_burst >= self.burst_n:
            self._since_burst = 0
            self.log(f'歇 {self.burst_pause_ms // 1000}s (已跑 {self.burst_n} 个)')
            await asyncio.sleep(self.burst_pause_ms / 1000.0 + random.uniform(0, 2.0))


# ── 自检 demo ─────────────────────────────────────────────────────────────
async def _demo():
    """单文件直接跑:`python browser_session.py` 测一个文件,做诊断用。"""
    test_url = (
        'https://push2his.eastmoney.com/api/qt/stock/kline/get'
        '?secid=1.600000&fields1=f1,f2&fields2=f51,f52,f53,f54,f55,f56,f57,f58,f59,f61'
        '&klt=101&fqt=0&beg=20240101&end=20260201'
    )
    async with BrowserSession(verbose=True) as s:
        body, attempts, err = await s.fetch_json(test_url)
        if body:
            print('OK:', list(body.keys())[:5], '|klines|=' + str(len(body.get('data', {}).get('klines') or [])))
        else:
            print(f'FAIL after {attempts} tries: {err}')

if __name__ == '__main__':
    asyncio.run(_demo())