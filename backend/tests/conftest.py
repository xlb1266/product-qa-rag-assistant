"""让测试在 backend 目录下无需安装即可 import app 包。"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
