"""
PyInstaller entry point for QueryLux server.
This is the script PyInstaller bundles — it just calls uvicorn directly.
"""
import os
import sys
from pathlib import Path

# When frozen by PyInstaller, _MEIPASS contains all bundled files
if getattr(sys, 'frozen', False) and hasattr(sys, '_MEIPASS'):
    # Add the bundle dir to path so server.py imports work
    bundle_dir = Path(sys._MEIPASS)
    sys.path.insert(0, str(bundle_dir))

import uvicorn

# Import the FastAPI app
from server import app

if __name__ == '__main__':
    port = int(os.environ.get('PORT', 8766))
    host = os.environ.get('HOST', '127.0.0.1')
    uvicorn.run(app, host=host, port=port, log_level='warning')
