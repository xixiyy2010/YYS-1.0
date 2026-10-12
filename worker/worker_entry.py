# worker/worker_entry.py
"""
Worker 子进程入口。

由 WorkerLauncher 通过 subprocess.Popen 调用：
    python worker/worker_entry.py \
        --pipe <pipe_address> \
        --worker-id <id> \
        --device <device> \
        --task-module <plugin_module> \
        --task-class <class_name> \
        --hwnd-parent <int> \
        --hwnd-click <int> \
        --hwnd-shot <int> \
        --shm <shm_name> \
        --window-w <int> \
        --window-h <int> \
        --templates-dir <abs_path> \
        --max-count <int|inf>

改动要点（v4）：
    - 新增 --hwnd-parent：set_device_hwnd 用它，之前错用 hwnd_shot。
      语义上 _device_hwnd_map 存的是顶层窗口，用作 fallback，
      与 _render_hwnd_map（click_hwnd）区分。
    - 新增 --max-count：UI 上"次数"输入框的值，'inf' 或正整数。
      _TaskTabAdapter 用它初始化 max_count。
    - 移除重复的 device_utils.init_shared_memory 调用。
      Worker 侧读取共享内存统一走 SharedMemoryAccessor。

流程：
    1. 解析参数
    2. 设置 resource_paths.templates_dir
    3. 初始化 device_utils（子进程用）
    4. 动态加载任务类
    5. 创建 WorkerHost + SharedMemoryAccessor
    6. 用 _TaskTabAdapter 包装成 BaseTask 认识的 tab
    7. 实例化任务 + 跑

依赖：
    capability.device_utils
    capability.resource_paths
    contracts.core
    worker.worker_host
    worker.shared_memory_accessor
"""
import argparse
import importlib
import logging
import os
import sys
import threading
import time
import ctypes


def _watch_parent():
    """父进程死了就自杀"""
    parent_pid = os.getppid()
    kernel32 = ctypes.windll.kernel32
    while True:
        try:
            h = kernel32.OpenProcess(0x1000, False, parent_pid)
            if not h:
                os._exit(0)
            code = ctypes.c_ulong()
            ok = kernel32.GetExitCodeProcess(h, ctypes.byref(code))
            kernel32.CloseHandle(h)
            if not ok or code.value != 259:
                os._exit(0)
        except Exception:
            pass
        time.sleep(1.0)


_BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _BASE_DIR not in sys.path:
    sys.path.insert(0, _BASE_DIR)


from capability import device_utils
from capability import resource_paths
from contracts.core import StateEnteredEvent, StateExitedEvent
from worker.worker_host import WorkerHost
from worker.shared_memory_accessor import SharedMemoryAccessor


logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [%(levelname)s] worker: %(message)s',
)


# ==================================================================
#  任务 tab 适配器
# ==================================================================

class _TaskTabAdapter:
    """
    BaseTask 需要的最小 tab 接口。

    接口契约（业务层 BaseTask 依赖）：
        device_serial       属性
        max_count           属性
        task_stop_flag      属性（读写）
        wait_if_paused()    方法
        enter_state(name)   方法（业务上报进入状态）
        exit_state(name)    方法（业务上报退出状态）
        log(msg)            方法
    """

    def __init__(self, host: WorkerHost, device: str,
                 max_count: float = float('inf')):
        self._host = host
        self._device = device
        self._max_count = max_count
        self._stop_flag = False
        self._pause_flag = threading.Event()

    @property
    def device_serial(self) -> str:
        return self._device

    @property
    def max_count(self):
        return self._max_count

    @property
    def task_stop_flag(self) -> bool:
        return self._stop_flag or self._host.stopped

    @task_stop_flag.setter
    def task_stop_flag(self, value):
        self._stop_flag = bool(value)

    def wait_if_paused(self):
        while self._pause_flag.is_set():
            if self.task_stop_flag:
                return
            time.sleep(0.1)

    def pause(self):
        self._pause_flag.set()

    def resume(self):
        self._pause_flag.clear()

    def enter_state(self, state_name: str):
        self._host.publish(StateEnteredEvent(
            sender=self._device, state_name=state_name))

    def exit_state(self, state_name: str):
        self._host.publish(StateExitedEvent(
            sender=self._device, state_name=state_name))

    def log(self, msg, quiet=False):
        self._host.log(msg, quiet=quiet)


# ==================================================================
#  参数 / 设备初始化
# ==================================================================

def _parse_args():
    p = argparse.ArgumentParser()
    p.add_argument('--pipe', required=True)
    p.add_argument('--worker-id', required=True)
    p.add_argument('--device', required=True)
    p.add_argument('--task-module', required=True)
    p.add_argument('--task-class', required=True)
    p.add_argument('--hwnd-parent', type=int, default=0)
    p.add_argument('--hwnd-click', type=int, default=0)
    p.add_argument('--hwnd-shot', type=int, default=0)
    p.add_argument('--shm', default='')
    p.add_argument('--window-w', type=int, default=0)
    p.add_argument('--window-h', type=int, default=0)
    p.add_argument('--templates-dir', default='')
    p.add_argument('--max-count', default='inf')
    return p.parse_args()


def _parse_max_count(s: str) -> float:
    """把 UI 传来的次数解析成 float：'inf' 或正整数，其他情况兜底为 inf"""
    s = (s or '').strip().lower()
    if not s or s == 'inf':
        return float('inf')
    try:
        v = float(s)
    except ValueError:
        return float('inf')
    return v if v > 0 else float('inf')


def _setup_templates_dir(args):
    """设置 resource_paths.templates_dir（必须在 device_utils 初始化前调用）"""
    td = args.templates_dir or ''
    if not td:
        logging.warning(
            'worker_entry: 未收到 --templates-dir，模板匹配会失败')
        return
    try:
        ok = resource_paths.set_templates_dir(td)
        if ok:
            logging.info('worker_entry: templates 目录 = %s', td)
        else:
            logging.warning('worker_entry: templates 目录无效: %s', td)
    except Exception:
        logging.exception('worker_entry: 设置 templates 目录失败')


def _setup_device(args):
    """初始化 device_utils（子进程用）"""
    device = args.device

    if args.window_w > 0 and args.window_h > 0:
        device_utils.set_window_size(device, args.window_w, args.window_h)

    # 顶层窗口句柄：优先用 --hwnd-parent，兜底 hwnd_shot
    parent = args.hwnd_parent or args.hwnd_shot
    if parent:
        device_utils.set_device_hwnd(device, parent)

    if args.hwnd_click:
        device_utils.set_device_render_hwnd(device, args.hwnd_click)
    elif args.hwnd_shot:
        device_utils.set_device_render_hwnd(device, args.hwnd_shot)

    if args.shm:
        try:
            # 只 register，不 init_shared_memory。
            # Worker 侧读帧统一走 SharedMemoryAccessor，
            # device_utils 的全局 _shared_mem 是旧路径，不再使用。
            device_utils.register_shared_memory(device, args.shm)
        except Exception:
            logging.exception('worker_entry: device_utils 共享内存注册失败')

    try:
        device_utils.sync_base_resolution_from_index(device=device)
    except Exception:
        logging.exception('worker_entry: 基准分辨率同步失败')


# ==================================================================
#  主入口
# ==================================================================

def main():
    threading.Thread(target=_watch_parent, daemon=True).start()
    args = _parse_args()

    # 1. 先设置 templates 目录
    _setup_templates_dir(args)

    # 2. 再初始化 device_utils
    _setup_device(args)

    # 3. 动态加载任务类
    try:
        module = importlib.import_module(args.task_module)
        task_cls = getattr(module, args.task_class)
    except Exception:
        logging.exception('worker_entry: 加载任务失败 %s.%s',
                          args.task_module, args.task_class)
        sys.exit(2)

    # 4. 创建 WorkerHost
    host = WorkerHost(
        pipe_address=args.pipe,
        worker_id=args.worker_id,
        device=args.device,
    )

    # 5. 共享内存访问器
    accessor = SharedMemoryAccessor()
    if args.shm:
        accessor.attach(args.shm)
    host.attach_shared_memory(accessor)

    if not host.start():
        logging.error('worker_entry: WorkerHost 启动失败')
        sys.exit(3)

    # 6. tab 适配器（带 max_count）
    max_count = _parse_max_count(args.max_count)
    logging.info('worker_entry: max_count = %s', max_count)
    tab = _TaskTabAdapter(host=host, device=args.device, max_count=max_count)

    def log_cb(msg, quiet=False):
        host.log(msg, quiet=quiet)

    def stats_cb(data):
        host.report_progress(
            task_name=args.task_module,
            message=str(data),
        )

    def error_cb(msg, quiet=False):
        host.log(msg, level='error', quiet=quiet)

    # 7. 实例化任务
    try:
        task = task_cls(
            tab,
            log_callback=log_cb,
            stats_callback=stats_cb,
            error_callback=error_cb,
        )
    except Exception:
        logging.exception('worker_entry: 实例化任务失败')
        host.report_crash(crash_reason='实例化任务失败')
        sys.exit(4)

    # 8. 注入 pause / resume 回调
    host.set_pause_resume_hooks(on_pause=tab.pause, on_resume=tab.resume)
    host.set_task(task)

    # 9. 跑（阻塞到任务结束）
    host.run_until_stopped()


if __name__ == '__main__':
    main()