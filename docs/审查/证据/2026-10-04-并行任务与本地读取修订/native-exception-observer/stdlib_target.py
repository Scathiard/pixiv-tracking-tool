import faulthandler
faulthandler.enable(all_threads=True)
import json
import sys
import threading
import time

worker = threading.Thread(target=lambda: time.sleep(0.05))
worker.start()
worker.join()
print(json.dumps({'stdlib_target': 'completed', 'faulthandler_enabled': faulthandler.is_enabled()}), flush=True)
raise SystemExit(0)
