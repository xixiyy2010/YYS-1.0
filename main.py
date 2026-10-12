# main.py
"""
启动器：起引擎进程 + UI 进程，两个都是 subprocess。

    main.py（本进程）
        ├─→ engine_main.py   引擎：DeviceManager / WorkerLauncher / UIHost
        └─→ ui_main.py       UI：QApplication / MainWindow / EngineClient

顺序：
    1. 先起引擎（它先监听 UI 管道）
    2. 等一小会儿（引擎初始化）
    3. 再起 UI（UI 主动连引擎管道）
    4. 主进程阻塞在 UI 上
    5. UI 退出 → 终止引擎 → 本进程退出

容错：
    - 引擎起不来：UI 依然起，UI 内部显示"引擎失联"
    - 引擎崩了：UI 收到管道断开，显示"引擎失联"，其他按钮灰掉
    - UI 崩了：主进程 wait 到 UI 退出，终止引擎

依赖：无（只用标准库 + subprocess）
"""
import os
import subprocess
import sys
import time


BASE_DIR = os.path.dirname(os.path.abspath(__file__))

ENGINE_SCRIPT = os.path.join(BASE_DIR, 'engine_main.py')
UI_SCRIPT = os.path.join(BASE_DIR, 'ui_main.py')

# 引擎起来后，等多少秒再拉 UI（给引擎初始化留时间）
ENGINE_WARMUP_SEC = 1.0

# 引擎进程退出后，再给多少秒，避免瞬时刷屏
WAIT_ENGINE_EXIT_SEC = 2.0


def _spawn(script_path: str, name: str) -> subprocess.Popen:
    """起一个子进程，cwd 固定到项目根。"""
    if not os.path.exists(script_path):
        print(f'❌ 找不到 {name}: {script_path}', file=sys.stderr)
        sys.exit(1)
    try:
        proc = subprocess.Popen(
            [sys.executable, script_path],
            cwd=BASE_DIR,
        )
    except Exception as e:
        print(f'❌ 启动 {name} 失败: {e}', file=sys.stderr)
        sys.exit(1)
    print(f'✅ {name} 已启动 pid={proc.pid}')
    return proc


def _terminate(proc: subprocess.Popen, name: str) -> None:
    """温和结束子进程：terminate → 2s → kill"""
    if proc is None or proc.poll() is not None:
        return
    try:
        proc.terminate()
        try:
            proc.wait(timeout=2.0)
        except subprocess.TimeoutExpired:
            proc.kill()
            try:
                proc.wait(timeout=1.0)
            except subprocess.TimeoutExpired:
                pass
    except Exception as e:
        print(f'⚠ 终止 {name} 异常: {e}', file=sys.stderr)


def main() -> int:
    print('▶ 痒痒鼠启动器')

    engine_proc = _spawn(ENGINE_SCRIPT, '引擎进程')

    # 引擎初始化时间：即使引擎崩了也只是 UI 会显示"失联"，不阻塞本进程
    time.sleep(ENGINE_WARMUP_SEC)

    # 引擎提前死了？
    if engine_proc.poll() is not None:
        print(
            f'⚠ 引擎进程启动后立即退出 code={engine_proc.returncode}；'
            f'UI 仍会启动，但会显示"引擎失联"',
            file=sys.stderr,
        )

    ui_proc = _spawn(UI_SCRIPT, 'UI 进程')

    try:
        # 阻塞到 UI 退出
        ui_proc.wait()
        print(f'ℹ UI 进程已退出 code={ui_proc.returncode}')
    except KeyboardInterrupt:
        print('ℹ 收到 Ctrl+C，准备退出')
    finally:
        # UI 退出 → 关引擎
        _terminate(ui_proc, 'UI 进程')
        _terminate(engine_proc, '引擎进程')

    return 0


if __name__ == '__main__':
    try:
        sys.exit(main())
    except Exception as e:
        import traceback
        print(f'❌ 启动器崩溃: {e}', file=sys.stderr)
        traceback.print_exc()
        sys.exit(1)