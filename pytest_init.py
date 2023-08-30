import pytest
import sys

if __name__ == "__main__":
    sys.exit(pytest.main(["-s", "-x", "-n2", "--forked", "--cov=neurodamus"]))