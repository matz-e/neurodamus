"""
Collection of utility functions related to clearing the used memory in neurodamus-py or NEURON
"""
from collections import Counter
import ctypes
import ctypes.util
import logging
import math
import os
import json
import psutil
import multiprocessing

from ..core import MPI, NeurodamusCore as Nd, run_only_rank0

import numpy as np


def trim_memory():
    """
    malloc_trim - release free memory from the heap (back to the OS)

    * We should only run malloc_trim if we are using the default glibc memory allocator.
    When using a custom allocator such as jemalloc, this could cause unexpected behavior
    including segfaults.

    * The malloc_trim function returns 1 if memory was actually
    released back to the system, or 0 if it was not possible to
    release any memory.
    """

    if os.getenv("LD_PRELOAD") and "jemalloc" in os.getenv("LD_PRELOAD"):
        logging.warning("malloc_trim works only with the default glibc memory allocator. "
                        "Please avoid using jemalloc (for now, we skip malloc_trim).")
        logging.info("malloc_trim: not possible to release any memory.")
    else:
        try:
            path_libc = ctypes.util.find_library("c")
            libc = ctypes.CDLL(path_libc)
            memory_trimmed = libc.malloc_trim(0)
            if memory_trimmed:
                logging.info("malloc_trim: memory released back to the system.")
        except OSError:
            logging.error("Unable to call malloc_trim.")
            logging.info("malloc_trim: not possible to release any memory.")


def pool_shrink():
    """
    Shrink NEURON ArrayPools to the exact size still being used by NEURON.
    This is useful when clearing all the Nodes from NEURON (cells and synapses) and the underlying
    data structures holding the data of their mechanisms need to be shrinked down since they are
    not used any more.
    This feature is enabled in NEURON after version 8.2.2a.
    See https://github.com/neuronsimulator/nrn/pull/2033 for more information.
    """
    try:
        cv = Nd.CVode()
        cv.poolshrink(1)
    except AttributeError:
        logging.warning("Cannot shrink NEURON ArrayPools. "
                        "NEURON does not support CVode().poolshrink(1)")


def free_event_queues():
    """
    Apart from the NEURON SelfEventQueue and TQItem pools being clear we also free them from any
    memory still referencing to.
    """
    try:
        cv = Nd.CVode()
        cv.free_event_queues()
    except AttributeError:
        logging.warning("Cannot clear NEURON event queues."
                        "NEURON does not support CVode().free_event_queues()")


def print_node_level_mem_usage():
    """Print statistics of the memory usage per compute node."""
    from ..core._shmutils import SHMUtil

    # The association of MPI ranks to compute nodes isn't
    # always know. If unknown, don't print anything.
    if not SHMUtil.is_node_id_known():
        return

    rss = SHMUtil.get_nodewise_rss()

    min_usage_mb = np.min(rss) / 2**20
    max_usage_mb = np.max(rss) / 2**20
    avg_usage_mb = np.mean(rss) / 2**20
    dev_usage_mb = np.std(rss) / 2**20

    logging.info(
        "Memusage (RSS) per node [MB]: Max=%.2lf, Min=%.2lf, Mean(Stdev)=%.2lf(%.2lf)",
        max_usage_mb,
        min_usage_mb,
        avg_usage_mb,
        dev_usage_mb
    )


def get_task_level_mem_usage():
    """Return statistics of the memory usage per MPI task."""
    usage_mb = get_mem_usage_kb() / 1024

    min_usage_mb = MPI.pc.allreduce(usage_mb, MPI.MIN)
    max_usage_mb = MPI.pc.allreduce(usage_mb, MPI.MAX)
    avg_usage_mb = MPI.pc.allreduce(usage_mb, MPI.SUM) / MPI.size

    dev_usage_mb = math.sqrt(MPI.pc.allreduce((usage_mb - avg_usage_mb) ** 2, MPI.SUM) / MPI.size)

    return min_usage_mb, max_usage_mb, avg_usage_mb, dev_usage_mb


def print_task_level_mem_usage():
    """Print statistics of the memory usage per MPI task."""

    min_usage_mb, max_usage_mb, avg_usage_mb, dev_usage_mb = get_task_level_mem_usage()

    logging.info(
        "Memusage (RSS) per task [MB]: Max=%.2lf, Min=%.2lf, Mean(Stdev)=%.2lf(%.2lf)",
        max_usage_mb,
        min_usage_mb,
        avg_usage_mb,
        dev_usage_mb
    )


def print_mem_usage():
    """
    Print memory usage information across all ranks.
    """

    print_node_level_mem_usage()
    print_task_level_mem_usage()


def get_mem_usage_kb():
    """
    Return memory usage information across all ranks, in KiloBytes
    """
    with open("/proc/self/statm") as fd:
        _, data_size, _ = fd.read().split(maxsplit=2)
    usage_kb = float(data_size) * os.sysconf("SC_PAGE_SIZE") / 1024

    return usage_kb


def pretty_printing_memory_mb(memory_mb):
    """
    A simple function that given a memory usage in MB
    returns a string with the most appropriate unit.
    """
    if memory_mb < 1024:
        return "%.2lf MB" % memory_mb
    elif memory_mb < 1024 ** 2:
        return "%.2lf GB" % (memory_mb / 1024)
    elif memory_mb < 1024 ** 3:
        return "%.2lf TB" % (memory_mb / 1024 ** 2)
    else:
        return "%.2lf PB" % (memory_mb / 1024 ** 3)


class SynapseMemoryUsage:
    ''' A small class that works as a lookup table
    for the memory used by each type of synapse.
    The values are in KB. The values cannot be set by the user.
    '''
    _synapse_memory_usage = {
        "ProbAMPANMDA": 1.7,
        "ProbGABAAB": 2.0,
        "Gap": 2.0,
        "Glue": 0.5
    }

    @classmethod
    def get_memory_usage(cls, count, synapse_type):
        return count * cls._synapse_memory_usage[synapse_type]


class DryRunStats:
    _MEMORY_USAGE_FILENAME = "cell_memory_usage.json"

    def __init__(self) -> None:
        self.metype_memory = {}
        self.metype_counts = Counter()
        self.synapse_counts = Counter()
        _, _, self.base_memory, _ = get_task_level_mem_usage()

    @run_only_rank0
    def estimate_cell_memory(self) -> float:
        from .logging import log_verbose
        memory_total = 0
        log_verbose("+{:=^81}+".format(" METype Memory Estimates (MiB) "))
        log_verbose("| {:^40s} | {:^10s} | {:^10s} | {:^10s} |".format(
            'METype', 'Mem/cell', 'N Cells', 'Mem Total'))
        log_verbose("+{:-^81}+".format(""))
        for metype, count in self.metype_counts.items():
            metype_mem = self.metype_memory[metype] / 1024
            metype_total = count * metype_mem
            memory_total += metype_total
            log_verbose("| {:<40s} | {:10.2f} | {:10.0f} | {:10.1f} |".format(
                metype, metype_mem, count, metype_total))
        log_verbose("+{:-^81}+".format(""))
        self.cell_memory_total = memory_total
        logging.info("  Total memory usage for cells: %s", pretty_printing_memory_mb(memory_total))
        return memory_total

    def add(self, other):
        self.metype_memory.update(other.metype_memory)
        self.metype_counts += other.metype_counts

    def collect_all_mpi(self):
        # We combine memory dict via update(). That means if a previous circuit computed
        # cells for the same METype (hopefully unlikely!) the last estimate prevails.
        self.metype_memory = MPI.py_reduce(self.metype_memory, {}, lambda x, y: x.update(y))
        self.metype_counts = self.metype_counts  # Cell counts is complete in every rank

    @run_only_rank0
    def export_cell_memory_usage(self):
        with open(self._MEMORY_USAGE_FILENAME, 'w') as fp:
            json.dump(self.metype_memory, fp, sort_keys=True, indent=4)

    def try_import_cell_memory_usage(self):
        if not os.path.exists(self._MEMORY_USAGE_FILENAME):
            return
        logging.info("Loading memory usage from %s...", self._MEMORY_USAGE_FILENAME)
        with open(self._MEMORY_USAGE_FILENAME, 'r') as fp:
            self.metype_memory = json.load(fp)

    def collect_display_syn_counts(self):
        master_counter = MPI.py_sum(self.synapse_counts, Counter())

        # Done with MPI. Use rank0 to display
        if MPI.rank != 0:
            return

        logging.info(" - Estimated synapse memory usage (MB):")
        from .logging import log_verbose
        inh_count = exc_count = 0
        log_verbose("+{:=^68}+".format(" Synapse Count "))
        log_verbose("| {:^40s} | {:^10s} | {:^10s} |".format("Synapse Type", "Family", "Count"))
        log_verbose("+{:-^68}+".format(""))
        for syn_type, count in sorted(master_counter.items()):
            is_inh = syn_type < 100
            syn_type_str = "INH" if is_inh else "EXC"
            log_verbose("| {:40.0f} | {:<10s} | {:10.0f} |".format(syn_type, syn_type_str, count))
            if is_inh:
                inh_count += count
            else:
                exc_count += count
        log_verbose("+{:-^68}+".format(""))

        in_mem = SynapseMemoryUsage.get_memory_usage(inh_count, "ProbGABAAB") / 1024
        ex_mem = SynapseMemoryUsage.get_memory_usage(exc_count, "ProbAMPANMDA") / 1024
        self.synapse_memory_total = in_mem + ex_mem
        logging.info("   - Inhibitory: %s", pretty_printing_memory_mb(in_mem))
        logging.info("   - Excitatory: %s", pretty_printing_memory_mb(ex_mem))
        logging.info(" - TOTAL : %s", pretty_printing_memory_mb(self.synapse_memory_total))

    @run_only_rank0
    def display_total(self):
        logging.info("+{:=^57}+".format(" Total Memory Estimates "))
        logging.info("| {:^40s} | {:^12s} |".format("Item", "Memory (MiB)"))
        logging.info("+{:-^57}+".format(""))
        full_overhead = self.base_memory * MPI.size
        logging.info("| {:<40s} | {:12.1f} |".format(f"Overhead (ranks={MPI.size})", full_overhead))
        logging.info("| {:<40s} | {:12.1f} |".format("Cells", self.cell_memory_total))
        logging.info("| {:<40s} | {:12.1f} |".format("Synapses", self.synapse_memory_total))
        logging.info("+{:-^57}+".format(""))
        grand_total = full_overhead + self.cell_memory_total + self.synapse_memory_total
        grand_total = pretty_printing_memory_mb(grand_total)
        logging.info("| {:<40s} | {:>12s} |".format("GRAND TOTAL", grand_total))
        logging.info("+{:-^57}+".format(""))

    def total_memory_available():
        """
        Returns the total memory available in the system in MB
        """
        try:
            virtual_memory = psutil.virtual_memory()
            return virtual_memory.total / (1024 * 1024)  # Total available memory in MB
        except Exception as e:
            logging.error(f"Error: {e}")
            return None

    @run_only_rank0
    def suggest_nodes(self, margin):
        """
        A function to calculate the suggested number of nodes to run the simulation
        The function takes into account the fact that the memory overhead is
        variable with the amount of ranks the simulation it's ran with.
        One can also specify a custom margin to add to the memory usage.
        """

        try:
            ranks_per_node = os.cpu_count()
        except AttributeError:
            ranks_per_node = multiprocessing.cpu_count()

        full_overhead = self.base_memory * ranks_per_node

        # initialize variable for iteration
        est_nodes = 0
        prev_est_nodes = None
        max_iter = 5
        iter_count = 0

        while (prev_est_nodes is None or est_nodes != prev_est_nodes) and iter_count < max_iter:
            prev_est_nodes = est_nodes
            mem_usage_per_node = full_overhead + self.cell_memory_total + self.synapse_memory_total
            mem_usage_with_margin = mem_usage_per_node * (1 + margin)
            est_nodes = math.ceil(mem_usage_with_margin / DryRunStats.total_memory_available())
            full_overhead = self.base_memory * ranks_per_node * est_nodes
            iter_count += 1

        return est_nodes

    @run_only_rank0
    def display_node_suggestions(self):
        """
        Display suggestions for how many nodes are approximately
        necessary to run the simulation based on the memory available
        on the current node.
        """
        node_total_memory = DryRunStats.total_memory_available()
        if node_total_memory is None:
            logging.warning("Unable to get the total memory available on the current node.")
            return
        suggested_nodes = self.suggest_nodes(0.3)
        logging.info(f"Based on the memory available on the current node, "
                     f"it is suggested to use at least {suggested_nodes} node(s).")
        logging.info("This is just a suggestion and the actual number of nodes "
                     "needed to run the simulation may be different.")
        logging.info(f"The calculation was based on a total memory available of "
                     f"{pretty_printing_memory_mb(node_total_memory)} on the current node.")
        logging.info("Please remember that it is suggested to use the same class of nodes "
                     "for both the dryrun and the actual simulation.")
