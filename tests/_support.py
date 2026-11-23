"""测试共用工具：把项目根目录加入 sys.path，并提供不阻塞的假 UI。"""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.append(str(ROOT))


class FakeUI:
    """按脚本顺序回答提问，同时记录所有推送事件。"""

    def __init__(self, answers=None):
        self.answers = list(answers or [])
        self.events = []

    def agent(self, name, tag, text):
        self.events.append(("agent", name, tag, text))

    def info(self, text):
        self.events.append(("info", text))

    def show(self, text, final=False):
        self.events.append(("show", text, final))

    def ask(self, prompt, echo=True):
        if not self.answers:
            raise AssertionError("预置答案不足，卡在提问：%s" % prompt)
        return self.answers.pop(0)

    def of_kind(self, kind):
        return [event for event in self.events if event[0] == kind]