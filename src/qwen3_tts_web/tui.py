"""A thin terminal interface over the same CLI used by scripts."""

import asyncio
import json
import logging
import sys
import urllib.request
import webbrowser
from dataclasses import replace
from logging.handlers import RotatingFileHandler

from textual.app import App, ComposeResult
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.screen import ModalScreen
from textual.widgets import (
    Button,
    Checkbox,
    DataTable,
    Footer,
    Header,
    Input,
    Label,
    RichLog,
    Select,
    Static,
    TabbedContent,
    TabPane,
)

from .config import save_settings
from .models import MODELS, model_path, validate_model
from .process import Cancelled, Runner


class ConfirmScreen(ModalScreen[bool]):
    DEFAULT_CSS = """
    ConfirmScreen { align: center middle; background: $background 70%; }
    ConfirmScreen > Vertical { width: 64; height: auto; padding: 1 2; border: solid $accent; background: $surface; }
    ConfirmScreen Label { height: auto; margin-bottom: 1; }
    ConfirmScreen Horizontal { height: 3; align-horizontal: right; }
    """

    def __init__(self, message):
        super().__init__()
        self.message = message

    def compose(self):
        with Vertical():
            yield Label(self.message, markup=False)
            with Horizontal():
                yield Button("取消", id="no")
                yield Button("确认", variant="primary", id="yes")

    def on_button_pressed(self, event):
        self.dismiss(event.button.id == "yes")

    def on_key(self, event):
        if event.key == "escape":
            self.dismiss(False)


class ManagerApp(App):
    TITLE = "Qwen3-TTS"
    SUB_TITLE = "环境与服务管理"
    BINDINGS = [("q", "request_quit", "退出")]
    CSS = """
    Screen { background: $background; }
    #status-row { height: 3; background: $surface; }
    #status { width: 1fr; height: 3; padding: 0 2; content-align: left middle; }
    #model-status { height: 10; margin-bottom: 1; }
    TabPane { padding: 1 2; }
    .actions { height: auto; min-height: 3; layout: horizontal; }
    Button { margin-right: 1; min-width: 12; }
    DataTable { height: 1fr; min-height: 6; }
    Select, Input { margin-bottom: 1; }
    Label { margin-top: 1; }
    #log { height: 1fr; }
    #paths { height: auto; margin-bottom: 1; }
    """

    def __init__(self, settings):
        super().__init__()
        self.settings = settings
        self.task_runner = None
        self.service_runner = None
        self.service_ready = False
        self.closing = False
        self.logger = logging.getLogger(f"qwen3.manager.{id(self)}")
        self.logger.setLevel(logging.INFO)
        self.logger.propagate = False
        self.file_handler = None

    def compose(self) -> ComposeResult:
        yield Header()
        with Horizontal(id="status-row"):
            yield Static("服务未启动", id="status", markup=False)
            yield Button("取消任务", id="cancel", disabled=True)
            yield Button("停止服务", id="stop", disabled=True)
        with TabbedContent():
            with TabPane("环境", id="environment"):
                yield Static(str(self.settings.root), id="paths", markup=False)
                with Horizontal(classes="actions"):
                    yield Button("重新检查", id="doctor")
                    yield Button("安装 / 修复", id="setup", variant="primary")
                yield Checkbox("同时安装系统音频工具", id="install-system")
                yield DataTable(id="checks", cursor_type="row")
            with TabPane("模型", id="models"):
                yield DataTable(id="model-status", cursor_type="row")
                yield Select(
                    [(name, key) for key, name in MODELS.items()],
                    value=self.settings.model_key,
                    allow_blank=False,
                    id="download-model",
                )
                yield Button("下载 / 校验", id="download", variant="primary")
            with TabPane("服务与配置", id="settings"):
                with VerticalScroll():
                    yield Label("设备")
                    yield Input(self.settings.device, id="device")
                    yield Label("克隆模型")
                    yield Select(
                        [("1.7B Base", "1.7B"), ("0.6B Base", "0.6B")],
                        value=self.settings.model,
                        allow_blank=False,
                        id="model",
                    )
                    yield Label("下载源")
                    yield Select(
                        [("国内优先", "domestic"), ("官方", "official")],
                        value=self.settings.source,
                        allow_blank=False,
                        id="source",
                    )
                    yield Label("监听地址")
                    yield Input(self.settings.host, id="host")
                    yield Label("端口")
                    yield Input(str(self.settings.port), type="integer", id="port")
                    yield Checkbox(
                        "离线模式", value=self.settings.offline, id="offline"
                    )
                    yield Button("保存配置", id="save")
                    with Horizontal(classes="actions"):
                        yield Button("启动服务", id="start", variant="success")
                        yield Button("打开 Web", id="web", disabled=True)
            with TabPane("日志", id="logs"):
                yield RichLog(id="log", markup=False, wrap=True, max_lines=2000)
        yield Footer()

    def on_mount(self):
        logs = self.settings.root / ".logs"
        logs.mkdir(exist_ok=True)
        self.file_handler = RotatingFileHandler(
            logs / "manager.log", maxBytes=2_000_000, backupCount=3, encoding="utf-8"
        )
        self.logger.addHandler(self.file_handler)
        self.query_one("#checks", DataTable).add_columns("检查项", "状态", "详情")
        self.query_one("#model-status", DataTable).add_columns("模型", "本地状态")
        self.refresh_models()
        self.set_interval(2, self.poll_health)
        self.start_task("doctor", ["--json"])

    def on_unmount(self):
        if self.file_handler:
            self.logger.removeHandler(self.file_handler)
            self.file_handler.close()

    def write_log(self, line):
        if not self.closing:
            self.query_one("#log", RichLog).write(line)
        self.logger.info(line)

    def refresh_controls(self):
        busy = self.task_runner is not None
        serving = self.service_runner is not None
        for name in ("doctor", "setup", "download", "save", "start"):
            self.query_one(f"#{name}", Button).disabled = busy or serving
        self.query_one("#cancel", Button).disabled = not busy
        self.query_one("#stop", Button).disabled = not serving
        self.query_one("#web", Button).disabled = not self.service_ready

    def refresh_models(self):
        table = self.query_one("#model-status", DataTable)
        table.clear()
        for key, name in MODELS.items():
            path = model_path(self.settings, key)
            status = (
                "未下载"
                if not path.exists()
                else ("不完整" if validate_model(path, key) else "完整")
            )
            table.add_row(name, status)

    def start_task(self, command, arguments=None):
        if self.task_runner or self.service_runner:
            return
        runner = Runner(lambda line: self.call_from_thread(self.write_log, line))
        self.task_runner = runner
        settings = self.settings
        self.refresh_controls()
        self.query_one("#status", Static).update(f"正在执行: {command}")

        def work():
            lines = []

            def log(line):
                lines.append(line)
                self.call_from_thread(self.write_log, line)

            runner.log = log
            message = f"{command} 完成"
            try:
                runner.run(
                    [
                        sys.executable,
                        "-m",
                        "qwen3_tts_web",
                        command,
                        *(arguments or []),
                    ],
                    env=settings.environment(),
                    cwd=settings.root,
                )
            except Exception as exc:
                message = str(exc)
                self.call_from_thread(self.write_log, message)
            finally:
                self.call_from_thread(self.task_finished, command, lines, message)

        self.run_worker(work, thread=True, name=command, exit_on_error=False)

    def task_finished(self, command, lines, message):
        self.task_runner = None
        if self.closing:
            return
        if command == "doctor":
            table = self.query_one("#checks", DataTable)
            table.clear()
            for line in lines:
                try:
                    checks = json.loads(line)
                    if isinstance(checks, list):
                        message = "环境检查完成"
                        if any(check["status"] == "error" for check in checks):
                            message += "，存在待处理项"
                        for check in checks:
                            table.add_row(
                                check["name"], check["status"], check["detail"]
                            )
                except (ValueError, TypeError, KeyError):
                    continue
        self.query_one("#status", Static).update(message)
        self.refresh_models()
        self.refresh_controls()

    def save_form(self):
        try:
            settings = replace(
                self.settings,
                device=self.query_one("#device", Input).value.strip(),
                model=self.query_one("#model", Select).value,
                source=self.query_one("#source", Select).value,
                host=self.query_one("#host", Input).value.strip(),
                port=int(self.query_one("#port", Input).value),
                offline=self.query_one("#offline", Checkbox).value,
            )
            save_settings(settings)
            self.settings = settings
            self.notify("配置已保存")
            return True
        except (ValueError, OSError) as exc:
            self.notify(str(exc), severity="error")
            return False

    def start_service(self):
        if self.service_runner or self.task_runner:
            return
        if not self.settings.runtime_python.exists():
            self.notify("请先安装推理环境", severity="error")
            return
        runner = Runner(lambda line: self.call_from_thread(self.write_log, line))
        self.service_runner = runner
        settings = self.settings
        self.refresh_controls()
        self.query_one("#status", Static).update("服务启动中，正在检查模型")

        def work():
            message = "服务已停止"
            try:
                runner.run(
                    [settings.runtime_python, "-m", "qwen3_tts_web", "serve", "--yes"],
                    env=settings.environment(),
                    cwd=settings.root,
                )
            except Cancelled:
                pass
            except Exception as exc:
                message = f"服务退出: {exc}"
                self.call_from_thread(self.write_log, message)
            finally:
                self.call_from_thread(self.service_finished, message)

        self.run_worker(work, thread=True, name="service", exit_on_error=False)

    def service_finished(self, message):
        self.service_runner = None
        self.service_ready = False
        if not self.closing:
            self.query_one("#status", Static).update(message)
            self.refresh_controls()

    async def poll_health(self):
        runner = self.service_runner
        if not runner or self.closing:
            return

        def request():
            try:
                opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
                with opener.open(
                    self.settings.url + "/api/health", timeout=1
                ) as response:
                    return json.load(response).get("ready", False)
            except Exception:
                return False

        ready = await asyncio.to_thread(request)
        if (
            self.service_runner is runner
            and runner.process
            and runner.process.poll() is None
        ):
            self.service_ready = ready
            if ready:
                self.query_one("#status", Static).update(
                    f"服务已就绪  {self.settings.url}"
                )
            self.refresh_controls()

    async def on_button_pressed(self, event):
        name = event.button.id
        if name == "doctor":
            self.start_task("doctor", ["--json"])
        elif name == "setup":
            arguments = ["--yes"]
            message = "安装 / 修复 .venv 并下载当前 Base 模型（数 GB）？"
            if self.query_one("#install-system", Checkbox).value:
                from .environment import system_install_command

                try:
                    message += "\n系统命令：" + " ".join(system_install_command())
                except RuntimeError as exc:
                    self.notify(str(exc), severity="error")
                    return
                arguments.append("--install-system")
            self.push_screen(
                ConfirmScreen(message),
                lambda ok: self.start_task("setup", arguments) if ok else None,
            )
        elif name == "download":
            key = self.query_one("#download-model", Select).value
            self.push_screen(
                ConfirmScreen(f"下载 / 校验 {MODELS[key]} 完整快照？"),
                lambda ok: (
                    self.start_task("download", ["--model-key", key, "--yes"])
                    if ok
                    else None
                ),
            )
        elif name == "save":
            self.save_form()
        elif name == "start":
            if self.save_form():
                self.push_screen(
                    ConfirmScreen("启动服务？缺失的模型将自动下载（数 GB）。"),
                    lambda ok: self.start_service() if ok else None,
                )
        elif name == "cancel" and self.task_runner:
            runner = self.task_runner
            self.query_one("#cancel", Button).disabled = True
            self.run_worker(runner.cancel, thread=True, exit_on_error=False)
        elif name == "stop" and self.service_runner:
            runner = self.service_runner
            self.query_one("#stop", Button).disabled = True
            self.run_worker(runner.cancel, thread=True, exit_on_error=False)
        elif name == "web" and self.service_ready:
            webbrowser.open(self.settings.url)

    async def shutdown_owned(self):
        self.closing = True
        for runner in (self.task_runner, self.service_runner):
            if runner:
                await asyncio.to_thread(runner.cancel)
        self.exit()

    def action_request_quit(self):
        if self.task_runner or self.service_runner:
            self.push_screen(
                ConfirmScreen("停止本界面启动的任务和服务并退出？"),
                lambda ok: self.run_worker(self.shutdown_owned()) if ok else None,
            )
        else:
            self.exit()

    def action_quit(self):
        self.action_request_quit()
