import sys
import traceback

def main():
    if "--smoke-test" in sys.argv:
        try:
            from src.smoke import run_smoke_test
            return run_smoke_test()
        except Exception:
            traceback.print_exc()
            return 1
    instance = None
    lifecycle = None
    diagnostics = None
    exit_code = 0
    completed = False
    try:
        from src.single_instance import ApplicationInstance
        instance = ApplicationInstance()
        if not instance.acquire():
            import webbrowser
            webbrowser.open_new(instance.existing_url())
        else:
            from src.config import DATA_DIR
            from src.core import lifecycle, diagnostics
            from src.core.stdio_log import open_stdio_log
            from src.version import DIAGNOSTIC_BUILD, __build__, __version__
            # Install ordinary output before importing the tray/service. These
            # few records remain enabled when detailed diagnostics are off.
            DATA_DIR.mkdir(parents=True, exist_ok=True)
            sys.stdout = sys.stderr = open_stdio_log(DATA_DIR / "tracker.log")
            lifecycle.start(DATA_DIR, build=__build__, version=__version__)
            if DIAGNOSTIC_BUILD or "--diagnostic" in sys.argv:
                diagnostics.start(DATA_DIR, build=__build__, version=__version__)
            from src.tray import run_tray
            with diagnostics.span("application.run", offline="--offline" in sys.argv):
                exit_code = run_tray(offline="--offline" in sys.argv, instance=instance) or 0
            completed = exit_code == 0
    except BaseException as exc:
        if lifecycle is not None:
            lifecycle.exception("application", exc)
        exit_code = 130 if isinstance(exc, KeyboardInterrupt) else 1
        print("\n启动失败：")
        traceback.print_exc()
        # A windowed EXE has no console input. Do not hide failure behind an
        # impossible Enter prompt or replace the original error with EOFError.
        if sys.stdin is not None and sys.stdin.isatty():
            print("\n按 Enter 退出...")
            input()
    finally:
        try:
            if diagnostics is not None:
                diagnostics.stop()
        finally:
            if lifecycle is not None:
                lifecycle.emit("cleanup_completed", outcome="ok" if completed else "failed")
                # Publish the final marker while still owning the instance lock.
                lifecycle.finish(clean=completed, exit_code=exit_code)
            if instance is not None:
                instance.close()
    return exit_code


if __name__ == "__main__":
    sys.exit(main())
