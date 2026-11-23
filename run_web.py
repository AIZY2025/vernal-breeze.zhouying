"""春风 · Web 对话界面入口。

python run_web.py                启动本地服务并打开浏览器
python run_web.py --port 9000    指定端口
python run_web.py --no-browser   只启动服务，不自动打开浏览器

本服务没有登录鉴权，只接受本机回环客户端。确需远程访问时必须同时设置
环境变量 SANDBOX_ALLOW_REMOTE=1 并显式传 --allow-remote，否则拒绝启动。
"""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from web.server import main  # noqa: E402

if __name__ == "__main__":
    sys.exit(main())