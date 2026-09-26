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
        from src.tray import run_tray
        run_tray(offline="--offline" in sys.argv)
    except Exception:
        print("\n启动失败：")
        traceback.print_exc()
        print("\n按 Enter 退出...")
        input()
        sys.exit(1)
