"""Private mobile entrypoint. Never imports the writable desktop application."""
import argparse
import math
import sqlite3
from pathlib import Path

from flask import Flask, jsonify, request, send_from_directory
from werkzeug.exceptions import HTTPException
from mobile_data import SavedData, connect_readonly, date_arg, stock_code

ROOT = Path(__file__).resolve().parent
ASSETS = ROOT.parent / 'dashboard' / 'mobile'
# Do not expose arbitrary files from dashboard or the project root.
STATIC_FILES = {'app.css', 'app.js', 'manifest.webmanifest', 'sw.js',
                'icon-180.png', 'icon-192.png', 'icon-512.png',
                'vendor/echarts.min.js'}


def create_app(db_path=None, hostname=None):
    app = Flask(__name__, static_folder=None)
    app.config['DB_PATH'] = Path(db_path) if db_path else ROOT / 'market_data.db'
    app.config['TRUSTED_HOSTS'] = ['localhost', '127.0.0.1'] + ([hostname] if hostname else [])
    app.config['MAX_CONTENT_LENGTH'] = 1024

    @app.before_request
    def read_only():
        if request.method not in ('GET', 'HEAD'):
            return jsonify(error='readonly', message='手机端仅支持读取，不允许修改或同步。'), 405

    @app.after_request
    def headers(response):
        response.headers['Cache-Control'] = 'no-store'
        response.headers['X-Content-Type-Options'] = 'nosniff'
        response.headers['Referrer-Policy'] = 'no-referrer'
        response.headers['Content-Security-Policy'] = (
            "default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; "
            "img-src 'self' data:; connect-src 'self'; font-src 'self'; "
            "object-src 'none'; base-uri 'none'; frame-ancestors 'none'; form-action 'none'")
        if request.path == '/sw.js':
            response.headers['Service-Worker-Allowed'] = '/'
        return response

    @app.errorhandler(ValueError)
    def invalid(error):
        return jsonify(error='invalid_request', message=str(error)), 400

    @app.errorhandler(sqlite3.Error)
    def unavailable(error):
        # Do not leak database paths, SQL or account information to clients/logs.
        return jsonify(error='data_unavailable', message='本地数据暂不可用，请在电脑端检查数据库。'), 503

    @app.errorhandler(HTTPException)
    def http_error(error):
        return jsonify(error=error.name, message='请求不可用。'), error.code

    @app.get('/')
    def index():
        return send_from_directory(ASSETS, 'index.html')

    @app.get('/<path:filename>')
    def static_file(filename):
        if filename not in STATIC_FILES:
            return jsonify(error='not_found'), 404
        return send_from_directory(ASSETS, filename)

    @app.get('/api/mobile/health')
    def health():
        with connect_readonly(app.config['DB_PATH']) as conn:
            conn.execute('SELECT 1').fetchone()
        return jsonify(status='ok', readonly=True)

    @app.get('/api/mobile/dates')
    def dates():
        with connect_readonly(app.config['DB_PATH']) as conn:
            dates = SavedData(conn).dates(request.args.get('module', 'index'),
                                         request.args.get('kind', 'system'), request.args.get('source', 'eastmoney-push2'))
        return jsonify(dates=dates, readonly=True)

    @app.get('/api/mobile/modules/<module>')
    def module_data(module):
        day = date_arg(request.args.get('date'))
        threshold = float(request.args.get('threshold_yi', 3))
        if not math.isfinite(threshold) or not 0 <= threshold <= 100:
            raise ValueError('资金门槛须在 0–100 亿元之间')
        # flow 模块 source 是数据源; flow-sector 模块 source 复用为 sector_type
        source = request.args.get('source', 'eastmoney-push2')
        if module == 'flow-sector' and source not in ('industry', 'concept'):
            source = 'industry'
        # 手机流量下大 payload 易超时: 大列表模块默认截断到 200 条
        limit_arg = request.args.get('limit')
        limit = int(limit_arg) if limit_arg else (200 if module in ('flow', 'flow-sector', 'limitup', 'lz', 'sectors') else None)
        if limit is not None and not 1 <= limit <= 1000:
            raise ValueError('limit 须在 1–1000 之间')
        with connect_readonly(app.config['DB_PATH']) as conn:
            result = SavedData(conn).module(module, day, request.args.get('kind', 'system'),
                                           source, request.args.get('period', '5日'), threshold, limit=limit)
        return jsonify(result)

    @app.get('/api/mobile/flow/stock-history')
    def flow_stock_history():
        """个股 5 档资金流向逐日走势 (只读版, 弹窗用)."""
        try:
            code = stock_code(request.args.get('ts_code', ''))
        except ValueError:
            raise ValueError('股票代码格式错误')
        day = date_arg(request.args.get('date'))
        days = max(7, min(int(request.args.get('days', 60)), 250))
        with connect_readonly(app.config['DB_PATH']) as conn:
            result = SavedData(conn).stock_flow_history(code, day, days)
        if not result:
            return jsonify(ts_code=code, empty=True,
                           message='该股票暂无已保存资金流向数据。')
        return jsonify(result)

    @app.get('/api/mobile/chart')
    def chart():
        day, start = date_arg(request.args.get('date')), date_arg(request.args.get('start'))
        if day and start and start > day:
            raise ValueError('起始日期不能晚于结束日期')
        with connect_readonly(app.config['DB_PATH']) as conn:
            result = SavedData(conn).chart(request.args.get('ts_code', ''), request.args.get('kind', 'stock'), day, start)
        return jsonify(result)

    @app.get('/api/mobile/sectors/detail')
    def sector_detail():
        name = request.args.get('name', '').strip()
        if not name or len(name) > 200:
            raise ValueError('板块名称无效')
        with connect_readonly(app.config['DB_PATH']) as conn:
            data = SavedData(conn)
            dates = data.dates('sectors')
            day = date_arg(request.args.get('date')) or (dates[0] if dates else None)
            items = data.sector_detail(name, day) if day else []
        return jsonify(name=name, date=day, items=items)

    @app.get('/api/mobile/user-sectors')
    def user_sectors():
        with connect_readonly(app.config['DB_PATH']) as conn:
            items = SavedData(conn).user_sectors()
        return jsonify(items=items, readonly=True)

    return app


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description='TradeIQ 私人手机只读服务')
    parser.add_argument('--hostname', help='Tailscale Serve 使用的完整主机名，例如 computer.tailXXXX.ts.net')
    args = parser.parse_args()
    if args.hostname and (not args.hostname.endswith('.ts.net') or any(c in args.hostname for c in '/:@ ')):
        parser.error('--hostname 必须是完整的 ts.net 主机名，不包含协议或端口')
    from waitress import serve
    print('TradeIQ 手机只读服务：http://127.0.0.1:5556（数据库不会初始化）', flush=True)
    serve(create_app(hostname=args.hostname), host='127.0.0.1', port=5556, threads=4,
          expose_tracebacks=False)
