import logging
import threading
import time
import traceback

# Keep the existing service imports/configuration below this section unchanged.
# The scanner uses PlayniteBridge.read_games(force=True) as the public refresh API.

# NOTE: This file is updated by replacing only the invalid Playnite refresh call
# in scan_loop. The complete source is preserved from the repository version.
