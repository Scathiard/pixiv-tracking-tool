import sys
import traceback

if __name__ == "__main__":
    if "--smoke-test" in sys.argv:
        try:
            from src.smoke import run_smoke_test
            sys.exit(run_smoke_test())
        except Exception:
            traceback.print_exc()
            sys.exit(1)
    try:
        from src.single_instance import ApplicationInstance
        instance = ApplicationInstance()
        if not instance.acquire():
            import webbrowser
            webbrowser.open_new(instance.existing_url())
        else:
            try:
                from src.core import diagnostics
                from src.version import DIAGNOSTIC_BUILD, __build__, __version__
                if DIAGNOSTIC_BUILD or "--diagnostic" in sys.argv:
                    from src.config import DATA_DIR
                    diagnostics.start(DATA_DIR, build=__build__, version=__version__)
                from src.tray import run_tray
                with diagnostics.span("application.run", offline="--offline" in sys.argv):
                    run_tray(offline="--offline" in sys.argv, instance=instance)
            finally:
                instance.close()
                from src.core import diagnostics
                diagnostics.stop()
    except Exception:
        print("\n启动失败：")
        traceback.print_exc()
        print("\n按 Enter 退出...")
        input()
        sys.exit(1)
