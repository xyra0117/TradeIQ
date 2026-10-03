"""Opt-in mobile browser regression, fixture data only.
RUN_MOBILE_BROWSER_TESTS=1 .venv/bin/python -m unittest discover -s backend -p test_mobile_browser.py
"""
import os
import tempfile
import threading
import unittest
from pathlib import Path

from mobile_app import create_app
from test_mobile import seed_fixture


@unittest.skipUnless(os.environ.get('RUN_MOBILE_BROWSER_TESTS') == '1', 'opt-in browser test')
class MobileBrowserTests(unittest.TestCase):
    def test_phone_navigation_charts_cache_and_disconnect(self):
        from playwright.sync_api import sync_playwright, expect
        from waitress import create_server, wasyncore
        with tempfile.TemporaryDirectory() as tmp:
            db=Path(tmp)/'fixture.db'
            seed_fixture(db)
            server=create_server(create_app(db),host='127.0.0.1',port=0)
            stop=threading.Event()
            def pump():
                while not stop.is_set():
                    wasyncore.loop(timeout=.05,count=1,map=server._map)
            thread=threading.Thread(target=pump,daemon=True)
            thread.start()
            origin=f'http://127.0.0.1:{server.effective_port}'
            try:
                with sync_playwright() as p:
                    browser=p.chromium.launch()
                    context=browser.new_context(viewport={'width':390,'height':844},device_scale_factor=2,is_mobile=True,has_touch=True)
                    page=context.new_page();errors=[];requests=[]
                    page.on('pageerror',lambda e:errors.append(str(e)))
                    page.on('request',lambda r:requests.append((r.method,r.url)))
                    page.goto(origin)
                    expect(page.locator('.index-card')).to_have_count(4)
                    expect(page.locator('#connection')).to_contain_text('已连接电脑')
                    for width in (320,375,390,430,844):
                        page.set_viewport_size({'width':width,'height':844 if width<800 else 390})
                        self.assertTrue(page.evaluate('document.documentElement.scrollWidth<=innerWidth'),str(width))
                    page.set_viewport_size({'width':390,'height':844})
                    page.screenshot(path='/private/tmp/tradeiq-mobile-overview.png',full_page=True)
                    page.locator('[data-index]').first.click()
                    expect(page.locator('#price-chart canvas')).to_have_count(1)
                    expect(page.locator('#chart-note')).to_contain_text('缺少完整 OHLC')
                    page.locator('[data-range="30"]').click()
                    expect(page.locator('#chart-note')).to_contain_text('30 个交易日')
                    page.locator('[data-range="all"]').click()
                    page.locator('#chart-start').fill('2026-09-11')
                    expect(page.locator('#chart-note')).to_contain_text('1 个交易日')
                    page.locator('#detail-close').click()
                    page.locator('[data-module="leaderboard"]').click()
                    expect(page.locator('tbody tr')).to_have_count(1)
                    page.locator('[data-stock]').first.click()
                    expect(page.locator('#price-chart canvas')).to_have_count(1)
                    page.screenshot(path='/private/tmp/tradeiq-mobile-chart.png',full_page=True)
                    page.locator('#detail-close').click()
                    page.locator('#search').fill('不匹配')
                    expect(page.locator('tbody tr')).to_have_count(0)
                    page.locator('#search').fill('')
                    page.locator('#period').select_option('20日')
                    expect(page.locator('tbody tr')).to_have_count(1)
                    page.locator('[data-module="flow"]').click()
                    expect(page.locator('tbody tr')).to_have_count(1)
                    page.locator('#source').select_option('ths')
                    expect(page.locator('#data-note')).to_contain_text('不同口径')
                    page.locator('[data-nav="picks"]').click()
                    expect(page.locator('tbody tr')).to_have_count(1)
                    page.locator('#kind').select_option('conditional')
                    expect(page.locator('#data-note')).to_contain_text('快照')
                    page.locator('[data-nav="positions"]').click()
                    expect(page.locator('tbody tr')).to_have_count(2)
                    expect(page.locator('#data-note')).to_contain_text('不是历史持仓快照')
                    page.locator('#date').select_option('20260911')
                    expect(page.locator('tbody tr')).to_have_count(2)
                    page.screenshot(path='/private/tmp/tradeiq-mobile-positions.png',full_page=True)
                    page.locator('[data-nav="more"]').click()
                    expect(page.locator('tbody tr')).to_have_count(1)
                    page.locator('[data-module="lz"]').click()
                    expect(page.locator('tbody tr')).to_have_count(1)
                    page.locator('[data-module="sectors"]').click()
                    expect(page.locator('[data-sector]')).to_have_count(1)
                    page.locator('[data-sector]').click()
                    expect(page.locator('#detail [data-stock]')).to_have_count(1)
                    page.locator('#detail [data-stock]').click()
                    expect(page.locator('#price-chart canvas')).to_have_count(1)
                    page.locator('#detail-back').click()
                    expect(page.locator('#detail [data-stock]')).to_have_count(1)
                    page.locator('#detail-close').click()
                    page.locator('#user-sectors').click()
                    expect(page.locator('#detail')).to_contain_text('我的机器人')
                    page.locator('#detail-close').click()
                    # Wait for worker control before proving offline shell / API separation.
                    page.evaluate('navigator.serviceWorker.ready')
                    page.reload()
                    expect(page.locator('.index-card')).to_have_count(4)
                    page.wait_for_function('!!navigator.serviceWorker.controller')
                    keys=page.evaluate('async()=>{const keys=await caches.keys();return (await Promise.all(keys.map(async k=>(await (await caches.open(k)).keys()).map(r=>new URL(r.url).pathname)))).flat()}')
                    self.assertTrue(keys)
                    self.assertFalse(any('/api/' in k for k in keys),keys)
                    context.set_offline(True)
                    page.locator('#refresh').click()
                    expect(page.locator('#connection')).to_contain_text('无法连接电脑')
                    page.reload()
                    expect(page.locator('#connection')).to_contain_text('无法连接电脑')
                    expect(page.locator('.index-card')).to_have_count(0)
                    context.set_offline(False)
                    page.locator('#refresh').click()
                    expect(page.locator('.index-card')).to_have_count(4)
                    self.assertFalse(errors,errors)
                    self.assertTrue(all(method=='GET' and url.startswith(origin) for method,url in requests))
                    context.close();browser.close()
            finally:
                stop.set()
                thread.join(timeout=2)
                server.task_dispatcher.shutdown()
                server.close()
