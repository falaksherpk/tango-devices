import os
import sys

# Make sure `import temperature_sensor` works regardless of which directory
# pytest is invoked from (e.g. locally vs. inside the CI container).
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))
