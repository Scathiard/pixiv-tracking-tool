"""Private headless blank page; no user profile, real app URL, or account access."""
import json
from pathlib import Path
from playwright.sync_api import sync_playwright

paths = [Path(r"C:\Program Files\Google\Chrome\Application\chrome.exe"),
         Path(r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe")]
executable = next(path for path in paths if path.is_file())
with sync_playwright() as engine:
    browser = engine.chromium.launch(headless=True, executable_path=str(executable))
    page = browser.new_page()
    result = page.evaluate("""async () => {
      const signal = AbortSignal.timeout(1);
      await new Promise(resolve => signal.addEventListener('abort', resolve, {once: true}));
      try {signal.throwIfAborted()} catch (error) {
        return {name: error.name, message: error.message, instanceof_Error: error instanceof Error};
      }
    }""")
    assert result["name"] == "TimeoutError"
    assert "signal timed out" in result["message"]
    print(json.dumps(result, ensure_ascii=False, indent=2))
    browser.close()
