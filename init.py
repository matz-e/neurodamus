"""
Neurodamus is a software for handling neuronal simulation using neuron.

Copyright (c) 2018 Blue Brain Project, EPFL.
All rights reserved
"""
import sys
import os
from neurodamus import commands
from neuron import h

import time


def main():
    """Get the options for neurodamus and launch it.

    We can't use positional arguments with special so we look for
    --configFile=FILE, which defaults to BlueConfig
    """
    first_argument_pos = 1
    config_file = "BlueConfig"

    for i, arg in enumerate(sys.argv):
        if arg.endswith("init.py"):
            first_argument_pos = i + 1
        elif arg.startswith("--configFile="):
            config_file = arg.split('=')[1]
            first_argument_pos = i + 1
            break

    args = [config_file] + sys.argv[first_argument_pos:]

    return commands.neurodamus(args)


if __name__ == "__main__":
    import memray
    memray_file = os.getenv("MEMRAY_OUTPUT_FILE", default="output_memray.bin")
    memray_mode = os.getenv("MEMRAY_MODE")

    if memray_mode == "off":
        exit_code = main()

    else:
        memray_native_traces = memray_mode.lower() == "native_traces"
        with memray.Tracker(memray_file, native_traces=memray_native_traces):
            # Returns exit code and calls MPI.Finalize
            exit_code = main()
            time.sleep(10)

    sys.exit(exit_code)
