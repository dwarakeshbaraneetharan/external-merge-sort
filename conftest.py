import os
import sys

# Make `engine` and `benchmark` importable when pytest is invoked from anywhere.
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
