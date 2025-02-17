import itertools
import logging
import os.path
from abc import ABCMeta, abstractmethod
from functools import lru_cache
from typing import List

import libsonata
import numpy

from .core import MPI, NeurodamusCore as Nd
from .core.configuration import ConfigurationError, SimConfig, GlobalConfig, find_input_file
from .core.nodeset import _NodeSetBase, NodeSet, SelectionNodeSet
from .utils import compat
from .utils.logging import log_verbose


class TargetError(Exception):
    """A Exception class specific to data error with targets and nodesets"""


class TargetSpec:
    """Definition of a new-style target, accounting for multipopulation"""

    GLOBAL_TARGET_NAME = "_ALL_"

    def __init__(self, target_name):
        """Initialize a target specification

        Args:
            target_name: the target name. For specifying a population use
                the format ``population:target_name``
        """
        if target_name and ":" in target_name:
            self.population, self.name = target_name.split(":")
        else:
            self.name = target_name
            self.population = None
        if self.name == "":
            self.name = None

    def __str__(self):
        return (
            (self.name or "")
            if self.population is None
            else "{}:{}".format(self.population, self.name or "")
        )

    def __repr__(self):
        return "<TargetSpec: " + str(self) + ">"

    @property
    def simple_name(self):
        if self.name is None and self.population is None:
            return self.GLOBAL_TARGET_NAME
        return self.__str__().replace(":", "_")

    @property
    def is_full(self):
        return (self.name or "Mosaic") == "Mosaic"

    def matches(self, pop, target_name):
        """Check if it matches a given target. Mosaic and (empty) are equivalent"""
        return pop == self.population and (target_name or "Mosaic") == (self.name or "Mosaic")

    def disjoint_populations(self, other):
        # When a population is None we cannot draw conclusions
        #  - In Sonata there's no filtering and target may have multiple
        #  - In BCs it's the base population, but the specified one may be the same
        if self.population is None or other.population is None:
            return False
        # We are only sure if both are specified and different
        return self.population != other.population

    def overlap_byname(self, other):
        return self.is_full or other.is_full or self.name == other.name

    def overlap(self, other):
        """Are these target specs bound to overlap?
        If not, they still might be overlap, but Target gids need to be inspected
        """
        return self.population == other.population and self.overlap_byname(other)

    def __eq__(self, other):
        return self.matches(other.population, other.name)


class TargetManager:

    def __init__(self, run_conf):
        """
        Initializes a new TargetManager
        """
        self._run_conf = run_conf
        self.parser = Nd.TargetParser()
        self._has_hoc_targets = False
        self.hoc = None  # The hoc level target manager
        self._targets = {}
        self._nodeset_reader = self._init_nodesets(run_conf)
        if MPI.rank == 0:
            self.parser.isVerbose = 1
        # A list of the local node sets
        self.local_nodes = []

    @classmethod
    def _init_nodesets(cls, run_conf):
        config_nodeset_file = run_conf.get("config_node_sets_file", None)
        simulation_nodesets_file = run_conf.get("node_sets_file")
        if not simulation_nodesets_file and "TargetFile" in run_conf:
            target_file = run_conf["TargetFile"]
            if target_file.endswith(".json"):
                simulation_nodesets_file = target_file
        return (config_nodeset_file or simulation_nodesets_file) and \
               NodeSetReader(config_nodeset_file, simulation_nodesets_file)

    def load_targets(self, circuit):
        """Provided that the circuit location is known and whether a user.target file has been
        specified, load any target files via a TargetParser.
        Note that these will be moved into a TargetManager after the cells have been distributed,
        instantiated, and potentially split.
        """
        def _is_sonata_file(file_name):
            if file_name.endswith(".h5"):
                return True
            return False
        if circuit.CircuitPath:
            self._try_open_start_target(circuit)

        nodes_file = circuit.get("CellLibraryFile")
        if nodes_file and _is_sonata_file(nodes_file) and self._nodeset_reader:
            self._nodeset_reader.register_node_file(find_input_file(nodes_file))

    def _try_open_start_target(self, circuit):
        start_target_file = os.path.join(circuit.CircuitPath, "start.target")
        if not os.path.isfile(start_target_file):
            log_verbose("Circuit %s start.target not available! Skipping", circuit._name)
        else:
            self.parser.open(start_target_file, False)
            self._has_hoc_targets = True

    def load_user_target(self):
        # Old target files. Notice new targets with same should not happen
        target_file = self._run_conf.get("TargetFile")
        if not target_file or target_file.endswith(".json"):  # allow any ext, except nodesets
            return
        user_target = find_input_file(target_file)
        self.parser.open(user_target, True)
        self._has_hoc_targets = True
        if MPI.rank == 0:
            logging.info(" => Loaded %d targets", self.parser.targetList.count())
            if GlobalConfig.verbosity >= 3:
                self.parser.printCellCounts()

    @classmethod
    def create_global_target(cls):
        # In blueconfig mode the _ALL_ target refers to base single population)
        if not SimConfig.is_sonata_config:
            return _HocTarget(TargetSpec.GLOBAL_TARGET_NAME, None)  # None population -> generic
        return NodesetTarget(TargetSpec.GLOBAL_TARGET_NAME, [])

    def register_target(self, target):
        self._targets[target.name] = target
        hoc_target = target.get_hoc_target()
        if hoc_target:
            self.parser.updateTargetList(target)

    def register_local_nodes(self, local_nodes):
        """Registers the local nodes so that targets can be scoped to current rank"""
        self.local_nodes.append(local_nodes)
        self.parser.updateTargets(local_nodes.final_gids(), 1)

    def clear_simulation_data(self):
        self.local_nodes.clear()
        self.parser.updateTargets(Nd.Vector(), 0)
        self.init_hoc_manager(None)  # Init/release cell manager

    def get_target(self, target_spec: TargetSpec, target_pop=None):
        """Retrieves a target from any .target file or Sonata nodeset files.

        Targets are generic groups of cells not necessarily restricted to a population.
        When retrieved from the source files they can be cached.
        Targets retrieved from Sonata nodesets keep a reference to all Sonata
        node datasets and can be asked for a sub-target of a specific population.
        """
        if not isinstance(target_spec, TargetSpec):
            target_spec = TargetSpec(target_spec)
        if target_pop:
            target_spec.population = target_pop
        target_name = target_spec.name or TargetSpec.GLOBAL_TARGET_NAME
        target_pop = target_spec.population

        def get_concrete_target(target):
            """Get a more specific target, depending on specified population prefix"""
            target.update_local_nodes(self.local_nodes)
            return target if target_pop is None else target.make_subtarget(target_pop)

        # Check cached
        if target_name in self._targets:
            target = self._targets[target_name]
            return get_concrete_target(target)

        # Check if we can get a Nodeset
        target = self._nodeset_reader and self._nodeset_reader.read_nodeset(target_name)
        if target is not None:
            log_verbose("Retrieved `%s` from Sonata nodeset", target_spec)
            self.register_target(target)
            return get_concrete_target(target)

        if self._has_hoc_targets:
            if self.hoc is not None:
                log_verbose("Retrieved `%s` from Hoc TargetManager", target_spec)
                hoc_target = self.hoc.getTarget(target_name)
            else:
                log_verbose("Retrieved `%s` from the Hoc TargetParser", target_spec)
                hoc_target = self.parser.getTarget(target_name)
            target = _HocTarget(target_name, hoc_target)
            self._targets[target_name] = target
            return get_concrete_target(target)

        raise ConfigurationError(
            "Target {} can't be loaded. Check target sources".format(target_name)
        )

    def init_hoc_manager(self, cell_manager):
        # give a TargetManager the TargetParser's completed targetList
        self.hoc = Nd.TargetManager(self.parser.targetList, cell_manager)

    def get_target_points(self, target, cell_manager, cell_use_compartment_cast, **kw):
        """Helper to retrieve the points of a target.
        If target is a cell then uses compartmentCast to obtain its points.
        Otherwise returns the result of calling getPointList directly on the target.

        Args:
            target: The target name or object (faster)
            manager: The cell manager to access gids and metype infos
            cell_use_compartment_cast: if enabled (default) will use target_manager.compartmentCast
                to get the point list.

        Returns: The target list of points
        """
        if isinstance(target, TargetSpec):
            target = self.get_target(target)
        if target.isCellTarget(**kw) and cell_use_compartment_cast:
            hoc_obj = self.hoc.compartmentCast(target.get_hoc_target(), "")
            return hoc_obj.getPointList(cell_manager)
        return target.getPointList(cell_manager, **kw)

    @lru_cache()
    def intersecting(self, target1, target2):
        """Checks whether two targets intersect"""
        target1_spec = TargetSpec(target1)
        target2_spec = TargetSpec(target2)
        if target1_spec.disjoint_populations(target2_spec):
            return False
        if target1_spec.overlap(target2_spec):
            return True

        # Couldn't get any conclusion from bare target spec
        # Obtain the targets to analyze
        t1, t2 = self.get_target(target1_spec), self.get_target(target2_spec)

        # Check for Sonata nodesets, they might have the same population and overlap
        if set(t1.populations) == set(t2.populations):
            if target1_spec.overlap_byname(target2_spec):
                return True

        # TODO: Investigate this might yield different results depending on the rank.
        return t1.intersects(t2)  # Otherwise go with full gid intersection

    def pathways_overlap(self, conn1, conn2, equal_only=False):
        src1, dst1 = conn1["Source"], conn1["Destination"]
        src2, dst2 = conn2["Source"], conn2["Destination"]
        if equal_only:
            return TargetSpec(src1) == TargetSpec(src2) and TargetSpec(dst1) == TargetSpec(dst2)
        return self.intersecting(src1, src2) and self.intersecting(dst1, dst2)

    def __getattr__(self, item):
        logging.debug("Compat interface to TargetManager::" + item)
        return getattr(self.hoc, item)


class NodeSetReader:
    """
    Implements reading Sonata Nodesets
    """

    def __init__(self, config_nodeset_file, simulation_nodesets_file):
        def _load_nodesets_from_file(nodeset_file):
            if not nodeset_file:
                return libsonata.NodeSets("{}")
            return libsonata.NodeSets.from_file(nodeset_file)
        self._population_stores = {}
        self.nodesets = _load_nodesets_from_file(config_nodeset_file)
        simulation_nodesets = _load_nodesets_from_file(simulation_nodesets_file)
        duplicate_nodesets = self.nodesets.update(simulation_nodesets)
        if duplicate_nodesets:
            logging.warning("Some node set rules were replaced from %s", simulation_nodesets_file)

    def register_node_file(self, node_file):
        storage = libsonata.NodeStorage(node_file)
        for pop_name in storage.population_names:
            self._population_stores[pop_name] = storage

    def __contains__(self, nodeset_name):
        return nodeset_name in self.nodesets.names

    @property
    def names(self):
        return self.nodesets.names

    def read_nodeset(self, nodeset_name: str):
        """Build node sets capable of offsetting.
        The empty population has a special meaning in Sonata, it matches
        all populations in simulation
        """
        if nodeset_name not in self.nodesets.names:
            return None

        def _get_nodeset(pop_name):
            storage = self._population_stores.get(pop_name)
            population = storage.open_population(pop_name)
            # Create NodeSet object with 1-based gids
            try:
                node_selection = self.nodesets.materialize(nodeset_name, population)
            except libsonata.SonataError as e:
                logging.warning("SonataError for nodeset %s from population \"%s\" : %s, skip"
                                % (nodeset_name, pop_name, str(e)))
                return None
            if node_selection:
                logging.debug("Nodeset %s: Appending gis from %s", nodeset_name, pop_name)
                ns = SelectionNodeSet(node_selection)
                ns.register_global(pop_name)
                return ns
            return None

        nodesets = (_get_nodeset(pop_name) for pop_name in self._population_stores)
        nodesets = [ns for ns in nodesets if ns]
        return NodesetTarget(nodeset_name, nodesets)


class _TargetInterface(metaclass=ABCMeta):
    """
    Methods that target/target wrappers should implement
    """

    @abstractmethod
    def gid_count(self):
        return NotImplemented

    @abstractmethod
    def get_gids(self):
        return NotImplemented

    @abstractmethod
    def get_raw_gids(self):
        return NotImplemented

    @abstractmethod
    def __contains__(self, final_gid):
        """
        Checks if a gid (with offset) is present in this target.
        All gids are taken into consideration, not only this ranks.
        """
        return NotImplemented

    @abstractmethod
    def make_subtarget(self, pop_name):
        return NotImplemented

    @abstractmethod
    def is_void(self):
        return NotImplemented

    @abstractmethod
    def get_hoc_target(self):
        return NotImplemented

    @abstractmethod
    def append_nodeset(self, nodeset: NodeSet):
        """Add a nodeset to the current target"""
        return NotImplemented

    def contains(self, items, raw_gids=False):
        """Return a bool or an array of bool's whether the elements are contained
        """
        # Shortcut for empty target. Algorithm below would fail
        if not self.gid_count():
            return ([False] * len(items)) if hasattr(items, "__len__") else False

        gids = self.get_raw_gids() if raw_gids else self.get_gids()
        pos = numpy.searchsorted(gids, items)
        if pos.ndim == 0:
            return pos < gids.size and gids[pos] == items
        else:
            pos[pos == len(gids)] = 0  # arbitrarily change to valid pos
            return gids[pos] == items

    def intersects(self, other):
        """ Check if two targets intersect. At least one common population has to intersect
        """
        if self.population_names.isdisjoint(other.population_names):
            return False

        other_pops = other.populations  # may be created on the fly
        # We loop over one target populations and check the other existence and intersection
        for pop, nodeset in self.populations.items():
            if pop not in other_pops:
                continue
            if nodeset.intersects(other_pops[pop]):
                return True
        return False

    @abstractmethod
    def generate_subtargets(self, n_parts):
        return NotImplemented

    def update_local_nodes(self, _local_nodes):
        """Allows setting the local gids"""
        pass


class _HocTargetInterface:
    """
    Interface of Hoc targets to be respected when we want to use objects
    in place of hoc targets.

    This interface provides a default implementation suitable for Nodeset targets
    where it will be primarily used
    """

    def isCellTarget(self, **kw):
        section_type = kw.get("sections", "soma")
        compartment_type = kw.get("compartments", "center" if section_type == "soma" else "all")
        return section_type == "soma" and compartment_type == "center"

    def isCompartmentTarget(self, *_):
        return 0

    def isSectionTarget(self, *_):
        return 0

    def isSynapseTarget(self, *_):
        return 0

    def getCellCount(self):
        return self.gid_count()

    def completegids(self):
        return compat.hoc_vector(self.get_raw_gids())

    def gids(self):
        return compat.hoc_vector(self.get_local_gids())

    @abstractmethod
    def getPointList(self, _cell_manager, **_kw):
        return NotImplemented

    def set_offset(self, *_):
        pass  # Only hoc targets require manually setting offsets

    def get_offset(self, *_):
        pass  # nodeset targets can span multiple multipopulation -> multiple offsets


class NodesetTarget(_TargetInterface, _HocTargetInterface):
    def __init__(self, name, nodesets: List[_NodeSetBase], local_nodes=None, **_kw):
        self.name = name
        self.nodesets = nodesets
        self.local_nodes = local_nodes

    def gid_count(self):
        return sum(len(ns) for ns in self.nodesets)

    def get_gids(self):
        """ Retrieve the final gids of the nodeset target """
        if not self.nodesets:
            logging.warning("Nodeset '%s' can't be materialized. No node populations", self.name)
            return numpy.array([])
        nodesets = sorted(self.nodesets, key=lambda n: n.offset)  # Get gids ascending
        gids = nodesets[0].final_gids()
        for extra_nodes in nodesets[1:]:
            gids = numpy.append(gids, extra_nodes.final_gids())
        return gids

    def get_raw_gids(self):
        """ Retrieve the raw gids of the nodeset target """
        if not self.nodesets:
            logging.warning("Nodeset '%s' can't be materialized. No node populations", self.name)
            return []
        if len(self.nodesets) > 1:
            raise TargetError("Can not get raw gids for Nodeset target with multiple populations.")
        return numpy.array(self.nodesets[0].raw_gids())

    def __contains__(self, gid):
        """ Determine if a given gid is included in the gid list for this target
        regardless of which cpu. Offsetting is taken into account
        """
        return self.contains(gid)

    def append_nodeset(self, nodeset: NodeSet):
        self.nodesets.append(nodeset)

    @property
    def population_names(self):
        return {ns.population_name for ns in self.nodesets}

    @property
    def populations(self):
        return {ns.population_name: ns for ns in self.nodesets}

    def make_subtarget(self, pop_name):
        """A nodeset subtarget contains only one given population
        """
        nodesets = [ns for ns in self.nodesets if ns.population_name == pop_name]
        local_nodes = [n for n in self.local_nodes if n.population_name == pop_name]
        return NodesetTarget(f"{self.name}#{pop_name}", nodesets, local_nodes)

    def is_void(self):
        return len(self.nodesets) == 0

    def get_hoc_target(self):
        return self  # impersonate a hoc target

    def update_local_nodes(self, local_nodes):
        self.local_nodes = local_nodes

    def get_local_gids(self, raw_gids=False):
        """Return the list of target gids in this rank (with offset)
        """
        assert self.local_nodes, "Local nodes not set"

        def pop_gid_intersect(nodeset: _NodeSetBase, raw_gids=False):
            for local_ns in self.local_nodes:
                if local_ns.population_name == nodeset.population_name:
                    return nodeset.intersection(local_ns, raw_gids)
            return []

        if raw_gids:
            assert len(self.nodesets) == 1, "Multiple populations when asking for raw gids"
            return pop_gid_intersect(self.nodesets[0], raw_gids=True)

        # If target is named Mosaic, basically we don't filter and use local_gids
        if self.name == "Mosaic" or self.name.startswith("Mosaic#"):
            gids_groups = tuple(n.final_gids() for n in self.local_nodes)
        else:
            gids_groups = tuple(pop_gid_intersect(ns) for ns in self.nodesets)

        return numpy.concatenate(gids_groups) if gids_groups else numpy.empty(0)

    def getPointList(self, cell_manager, **kw):
        """ Retrieve a TPointList containing compartments (based on section type and
        compartment type) of any local cells on the cpu.
        Args:
            cell_manager: a cell manager or global cell manager
            sections: section type, such as "soma", "axon", "dend", "apic" and "all",
                      default = "soma"
            compartments: compartment type, such as "center" and "all",
                          default = "center" for "soma", default = "all" for others
        Returns:
            list of TPointList containing the compartment position and retrieved section references
        """
        section_type = kw.get("sections") or "soma"
        compartment_type = kw.get("compartments") or ("center" if section_type == "soma" else "all")
        pointList = compat.List()
        for gid in self.get_local_gids():
            point = Nd.TPointList(gid)
            cellObj = cell_manager.get_cellref(gid)
            secs = getattr(cellObj, section_type)
            for sec in secs:
                if compartment_type == "center":
                    point.append(Nd.SectionRef(sec), 0.5)
                else:
                    for seg in sec:
                        point.append(Nd.SectionRef(sec), seg.x)
            pointList.append(point)
        return pointList

    def generate_subtargets(self, n_parts):
        """generate sub NodeSetTarget per population for multi-cycle runs
        Returns:
            list of [sub_target_n_pop1, sub_target_n_pop2, ...]
        """
        if not n_parts or n_parts == 1:
            return False

        all_raw_gids = {ns.population_name: ns.final_gids() - ns.offset for ns in self.nodesets}
        from collections import defaultdict
        new_targets = defaultdict(list)
        pop_names = list(all_raw_gids.keys())

        for cycle_i in range(n_parts):
            for pop in pop_names:
                # name sub target per populaton, to be registered later
                target_name = "{}__{}_{}".format(pop, self.name, cycle_i)
                target = NodesetTarget(target_name, [NodeSet().register_global(pop)])
                new_targets[pop].append(target)

        for pop, raw_gids in all_raw_gids.items():
            target_looper = itertools.cycle(new_targets[pop])
            for gid in raw_gids:
                target = next(target_looper)
                target.nodesets[0].add_gids([gid])

        # return list of subtargets lists of all pops per cycle
        return [[targets[cycle_i] for targets in new_targets.values()]
                    for cycle_i in range(n_parts)]


class _HocTarget(_TargetInterface):
    """
    A wrapper around Hoc targets to implement _TargetInterface
    """
    GID_DTYPE = numpy.uint32

    def __init__(self, name, hoc_target, pop_name=None, *, _raw_gids=None):
        self.name = name
        self.population_name = pop_name
        self.hoc_target = hoc_target
        self.offset = 0
        self._raw_gids = _raw_gids and numpy.array(_raw_gids, dtype=self.GID_DTYPE)

    @property
    def population_names(self):
        return {self.population_name}

    @property
    def populations(self):
        return {self.population_name: NodeSet(self.get_gids())}

    def gid_count(self):
        return len(self.get_raw_gids())

    def get_gids(self):
        if not self.offset:
            return self.get_raw_gids()
        try:
            return numpy.add(self.get_raw_gids(), self.offset, dtype=self.GID_DTYPE)
        except numpy.core._exceptions.UFuncTypeError as e:
            logging.error("Type error: please use type uint32 for the array of raw gids.")
            raise e

    def get_raw_gids(self):
        if self._raw_gids is None:
            assert self.hoc_target
            self._raw_gids = self.hoc_target.completegids().as_numpy().astype(self.GID_DTYPE)
            self._raw_gids.sort()
        return self._raw_gids

    def get_hoc_target(self):
        return self.hoc_target

    def gids(self):
        """This target gids on this rank, with offset"""
        return self.hoc_target.gids()

    def get_local_gids(self):
        return self.hoc_target.gids().as_numpy().astype(self.GID_DTYPE)

    def getPointList(self, cell_manager, **kw):
        return self.hoc_target.getPointList(cell_manager)

    def make_subtarget(self, pop_name):
        if pop_name is not None:
            # Old targets have only one population. Ensure one doesn't assign more than once
            if self.name == TargetSpec.GLOBAL_TARGET_NAME:  # This target is special
                return _HocTarget(pop_name + "__ALL__", Nd.Target(self.name), pop_name)
            if self.population_name not in (None, pop_name):
                raise ConfigurationError("Target %s cannot be reassigned population %s (cur: %s)"
                                         % (self.name, pop_name, self.population_name))
            self.population_name = pop_name
        return self

    def append_nodeset(self, nodeset: NodeSet):
        # Not very common but we may want to set the nodes later (e.g. the _ALL_ target)
        if self.population_name is not None:
            logging.warning("[Compat] Skipping adding population %s to HOC target %s",
                            nodeset.population_name, self.name)
            return
        self.population_name = nodeset.population_name
        self.offset = nodeset.offset
        self._raw_gids = numpy.asarray(nodeset.raw_gids())
        hoc_gids = compat.hoc_vector(self._raw_gids)
        self.hoc_target = Nd.Target(self.name, hoc_gids, self.population_name)

    def __contains__(self, item):
        return self.hoc_target.completeContains(item)

    def is_void(self):
        return not bool(self.hoc_target)  # old targets could match with any population

    def set_offset(self, offset):
        self.hoc_target.set_offset(offset)
        self.offset = offset

    def isCellTarget(self, **kw):
        return self.hoc_target.isCellTarget()

    def generate_subtargets(self, n_parts):
        """generate sub hoc targets for multi-cycle runs
        Returns:
            list of subtargets
        """
        if not n_parts or n_parts == 1:
            return False

        allgids = self.get_gids()
        new_targets = []

        for cycle_i in range(n_parts):
            target = Nd.Target()
            target.name = "{}_{}".format(self.name, cycle_i)
            new_targets.append(_HocTarget(target.name, target, self.population_name))

        target_looper = itertools.cycle(new_targets)
        for gid in allgids:
            target = next(target_looper)
            target.hoc_target.gidMembers.append(gid)

        return new_targets

    def __getattr__(self, item):
        return getattr(self.hoc_target, item)
