# engine_main.py
"""
引擎进程入口。

由 main.py 通过 subprocess.Popen 启动：
    python engine_main.py

职责：
    1. 初始化基础设施（LogManager / EventBus / IPCServer）
    2. 初始化 Host 层（PluginLoader / DeviceManager / WorkerLauncher）
    3. 启动 EngineService（订阅事件 + 开 UI 通道）
    4. 阻塞等待
    5. 优雅退出（关设备 → 停 IPC → 停 UI 通道）

线程/进程：
    - 引擎进程本身不依赖 Qt
    - 后台跑：IPCServer 接收线程、WorkerLauncher 监控线程、UIHost 接收线程
    - 主线程只做 wait

依赖：
    contracts.* / infra.* / host.*
"""
import ctypes
import logging
import os
import signal
import sys
import threading
import time

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
if BASE_DIR not in sys.path:
    sys.path.insert(0, BASE_DIR)


from infra import EventBus, IPCServer, LogManager, set_event_bus
from host import PluginLoader
from host.device_manager import DeviceManager
from host.engine_service import EngineService


logger = logging.getLogger(__name__)


# ==================================================================
#  父进程看门狗：main.py 退出 → 引擎自杀
# ==================================================================

def _watch_parent():
    """后台线程：父进程退出就自杀"""
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


# ==================================================================
#  主流程
# ==================================================================

def main():
    try:
        ctypes.windll.user32.SetProcessDPIAware()
    except Exception:
        pass

    # 日志落到 logs/engine.log（和主进程的 app.log 分开）
    log_dir = os.path.join(BASE_DIR, 'logs')
    os.makedirs(log_dir, exist_ok=True)
    logging.basicConfig(
        level=logging.INFO,
        format='%(asctime)s [%(levelname)s] engine: %(message)s',
        filename=os.path.join(log_dir, 'engine.log'),
        encoding='utf-8',
    )

    logger.info('engine_main: 启动')

    # 父进程看门狗
    threading.Thread(target=_watch_parent, daemon=True).start()

    # ---------- 基础设施 ----------
    event_bus = EventBus()
    set_event_bus(event_bus)

    ipc_server = IPCServer(event_bus)
    ipc_server.start()

    log_manager = LogManager(log_dir=log_dir)

    # ---------- Host ----------
    plugin_loader = PluginLoader(base_dir=BASE_DIR)
    plugin_loader.warmup()   # 启动时预扫一遍业务名片，入缓存

    device_manager = DeviceManager(
        event_bus=event_bus,
        ipc_server=ipc_server,
        log_manager=log_manager,
        base_dir=BASE_DIR,
    )

    # ---------- UI 服务 ----------
    engine_service = EngineService(
        event_bus=event_bus,
        log_manager=log_manager,
        device_manager=device_manager,
        plugin_loader=plugin_loader,
    )

    if not engine_service.start():
        logger.error('engine_main: EngineService 启动失败')
        _shutdown(engine_service, device_manager, ipc_server, log_manager)
        sys.exit(1)

    # ---------- 信号处理：Ctrl+C / SIGTERM 优雅退出 ----------
    def _on_signal(signum, frame):
        logger.info('engine_main: 收到信号 %s，准备退出', signum)
        engine_service.stop()

    try:
        signal.signal(signal.SIGINT, _on_signal)
        signal.signal(signal.SIGTERM, _on_signal)
    except Exception:
        pass

    logger.info('engine_main: 就绪，等待 UI 连接')

    # ---------- 阻塞 ----------
    try:
        engine_service.wait()          # 到 stop() 被调用
    except KeyboardInterrupt:
        logger.info('engine_main: KeyboardInterrupt')
    finally:
        _shutdown(engine_service, device_manager, ipc_server, log_manager)

    logger.info('engine_main: 已退出')


def _shutdown(engine_service, device_manager, ipc_server, log_manager) -> None:
    """按依赖倒序关闭"""
    try:
        engine_service.stop()
    except Exception:
        logger.exception('engine_main: EngineService 关闭异常')

    try:
        device_manager.shutdown()
    except Exception:
        logger.exception('engine_main: DeviceManager 关闭异常')

    try:
        ipc_server.stop()
    except Exception:
        logger.exception('engine_main: IPCServer 关闭异常')

    try:
        log_manager.shutdown()
    except Exception:
        logger.exception('engine_main: LogManager 关闭异常')

    try:
        set_event_bus(None)
    except Exception:
        pass


if __name__ == '__main__':
    try:
        main()
    except Exception as e:
        logger.exception('engine_main 崩溃: %s', e)
        sys.exit(1)