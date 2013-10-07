#!/usr/bin/env python
from __future__ import division, print_function

import os
import numpy as np
import yaml
import time
import collections
from collections import OrderedDict
import cPickle as pickle
from abipy.abilab import Workflow, AbinitFlow, Mrgscr, Mrgddb
from abipy.tools import AttrDict

from pydispatch import dispatcher
from pymatgen.io.abinitio.tasks import Task, Dependency, ScfTask, PhononTask
from pymatgen.io.abinitio.workflows import BandStructureWorkflow, IterativeWorkflow

import logging
logger = logging.getLogger(__name__)


def hello(signal, sender):
    print("on_hello with sender %s, signal %s" % (sender, signal))


def bandstructure_flow(workdir, manager, scf_input, nscf_input):
    """
    Build an `AbinitFlow` for band structure calculations.

    Args:
        workdir:
            Working directory.
        manager:
            `TaskManager` object used to submit the jobs
        scf_input:
            Input for the GS SCF run.
        nscf_input:
            Input for the NSCF run (band structure run).

    Returns:
        `AbinitFlow`
    """
    flow = AbinitFlow(workdir, manager)
    work = BandStructureWorkflow(scf_input, nscf_input)
    flow.register_work(work)
    return flow.allocate()


def g0w0_flow(workdir, manager, scf_input, nscf_input, scr_input, sigma_input):
    """
    Build an `AbinitFlow` for one-shot G0W0 calculations.

    Args:
        workdir:
            Working directory.
        manager:
            `TaskManager` object used to submit the jobs
        scf_input:
            Input for the GS SCF run.
        nscf_input:
            Input for the NSCF run (band structure run).
        scr_input:
            Input for the SCR run.
        sigma_input:
            Input for the SIGMA run.

    Returns:
        `AbinitFlow`
    """
    flow = AbinitFlow(workdir, manager)
    work = G0W0_Workflow(scf_input, scf_input, nscf_input, scr_input, sigma_input)
    flow.register_work(work)
    return flow.allocate()


class QptdmWorkflow(Workflow):
    """
    This workflow parallelizes the calculation of the q-points of the screening. 
    It also provides the callback `on_all_ok` that calls mrgscr to merge 
    all the partial screening files produced.
    """
    def fill(self, wfk_file, scr_input):
        """
        Create the SCR tasks and register them in self.

        Args:
            wfk_file:
                Path to the ABINIT WFK file to use for the computation of the screening.
            scr_input:
                Input for the screening calculation.

        Return:
            self
        """
        assert len(self) == 0
        wfk_file = self.wfk_file = os.path.abspath(wfk_file)

        # Build a temporary workflow in the tmpdir that will use a shell manager 
        # to run ABINIT in order to get the list of q-points for the screening.
        shell_manager = self.manager.to_shell_manager(mpi_ncpus=1)

        w = Workflow(workdir=self.tmpdir.path_join("_qptdm_run"), manager=shell_manager)

        fake_input = scr_input.deepcopy()
        fake_task = w.register(fake_input)
        w.build()

        # Create the symbolic link and add the magic value 
        # nqpdm = -1 to the input to get the list of q-points.
        fake_task.inlink_file(wfk_file)
        fake_task.strategy.add_extra_abivars({"nqptdm": -1})
        w.start()

        # Parse the section with the q-points
        qpoints = yaml_kpoints(fake_task.log_file.path, tag="<QPTDM>")
        #print(qpoints)
        #w.rmtree()

        # Now we can register the task for the different q-points 
        for qpoint in qpoints:
            qptdm_input = scr_input.deepcopy()
            qptdm_input.set_variables(
                nqptdm=1,
                qptdm=qpoint
            )
            self.register(qptdm_input, manager=self.manager)

        return self.allocate()

    def on_all_ok(self):
        """
        This method is called when all the q-points have been computed.
        It runs `mrgscr` in sequential on the local machine to produce
        the final SCR file in the outdir of the `Workflow`.
        """
        scr_files = filter(None, [task.outdir.has_abiext("SCR") for task in self])

        msg = "on_all_ok: will call mrgscr to merge %s:\n" % str(scr_files)
        logger.debug(msg)
        assert len(scr_files) == len(self)

        mrgscr = Mrgscr(verbose=1)

        final_scr = mrgscr.merge_qpoints(scr_files, out_prefix="out", cwd=self.outdir.path)

        results = dict(
            returncode=0,
            message="mrgscr done",
            final_scr=final_scr,
        )

        return results

#class PhononFlow(AbinitFlow):
#    def on_all_ok(self):
#        """
#        This method is called when all the q-points have been computed.
#        It runs `mrgddb` in sequential on the local machine to produce
#        the final DDB file in the outdir of the `Workflow`.
#        """
#        ph_works = self[1:]
#        ddb_files = filter(None, [work.outdir.has_abiext("DDB") for work in ph_works])
#
#        msg = "on_all_ok: will call mrgddb to merge %s:\n" % str(ddb_files)
#        logger.debug(msg)
#        assert len(ddb_files) == len(self)
#
#        # Final DDB file will be produced in the outdir of the workflow.
#        out_ddb = self.outdir.path_in("out_DDB")
#        desc = "DDB file merged by %s on %s" % (self.__class__.__name__, time.asctime())
#
#        mrgddb = Mrgddb(verbose=1)
#        mrgddb.merge(ddb_files, out_ddb=out_dbb, description=desc, cwd=self.outdir.path)
#
#        results = dict(
#            returncode=0,
#            message="DDB merge done",
#        )
#
#        return results


def phonon_flow(workdir, manager, scf_input, ph_inputs):
    """
    Build an `AbinitFlow` for phonon calculations.

    Args:
        workdir:
            Working directory.
        manager:
            `TaskManager` object used to submit the jobs
        scf_input:
            Input for the GS SCF run.
        ph_inputs:
            List of Inpus for the phonon runs.

    Returns:
        `AbinitFlow`
    """
    # FIXME cannot use this since abinit complains about ndtset=0 and acell1
    #natom = len(scf_input.structure)
    #natom = scf_input["natom"]
    natom = 2
    assert natom == 2

    # Create the container that will manage the different workflows.
    flow = AbinitFlow(workdir, manager)

    # Register the first workflow (GS calculation)
    scf_task = flow.register_task(scf_input, task_class=ScfTask)

    # Build a temporary workflow with a shell manager just to run 
    # ABINIT to get the list of irreducible pertubations for this q-point.
    shell_manager = manager.to_shell_manager(mpi_ncpus=1)

    if not isinstance(ph_inputs, (list, tuple)):
        ph_inputs = [ph_inputs]

    for i, ph_input in enumerate(ph_inputs):
        fake_input = ph_input.deepcopy()

        tmp_dir = os.path.join(workdir, "_ph_run" + str(i))
        w = Workflow(workdir=tmp_dir, manager=shell_manager)
        fake_task = w.register(fake_input)

        # Create the symbolic link and add the magic value 
        # paral_rf = -1 to get the list of irreducible perturbations for this q-point.
        vars = dict(paral_rf=-1,
                    rfatpol=[1, natom],  # Only the first atom is displaced
                    rfdir=[1, 1, 1],     # Along the first reduced coordinate axis
                   )

        fake_task.strategy.add_extra_abivars(vars)

        w.build()
        w.start()

        # Parse the file to get the perturbations.
        irred_perts = yaml_irred_perts(fake_task.log_file.path, tag="<IRRED_PERTS>")
        #print(irred_perts)
        w.rmtree()

        # Now we can build the final list of workflows:
        # One workflow per q-point, each workflow computes all 
        # the irreducible perturbations for a singe q-point.
        work_qpt = PhononWorkflow()
        for irred_pert in irred_perts:
            print(irred_pert)
            new_input = ph_input.deepcopy()

            #rfatpol   1 1   # Only the first atom is displaced
            #rfdir   1 0 0   # Along the first reduced coordinate axis
            qpt = irred_pert["qpt"]
            idir = irred_pert["idir"]
            ipert = irred_pert["ipert"]

            # TODO this will work for phonons, not for the 
            # other types of perturbations.
            rfdir = 3 * [0]
            rfdir[idir -1] = 1
            rfatpol = [ipert, ipert]

            new_input.set_variables(
                #rfpert=1,
                qpt=qpt,
                rfdir=rfdir,
                rfatpol=rfatpol,
            )

            work_qpt.register(new_input, deps={scf_task: "WFK"}, task_class=PhononTask)

        flow.register_work(work_qpt)
                                            
    return flow.allocate()


def yaml_kpoints(filename, tag="<KPOINTS>"):
    end_tag = tag.replace("<", "</")

    with open(filename, "r") as fh:
        lines = fh.readlines()

    start, end = None, None
    for i, line in enumerate(lines):
        if tag in line:
            start = i
        elif end_tag in line:
            end = i
            break
                                                                                                             
    if start is None or end is None:
        raise ValueError("%s\n does not contain any valid %s section" % (filename, tag))
                                                                                                             
    if start == end:
        # Empy section ==> User didn't enable Yaml support in ABINIT.
        raise ValueError("%s\n contains an empty RUN_HINTS section. Enable Yaml support in ABINIT" % filename)

    s = "".join(lines[start+1:end])

    try:
        d = yaml.load(s)
    except Exception as exc:
        raise ValueError("Malformatted Yaml document in file %s:\n %s" % (filename, str(exc)))

    return np.array(d["reduced_coordinates_of_qpoints"])
    #return KpointList(reciprocal_lattice, frac_coords, weights=None, names=None)


#class IrredPert(object):
#    def to_abivars(self):
#        #rfatpol   1 1   # Only the first atom is displaced
#        #rfdir   1 0 0   # Along the first reduced coordinate axis
#        qpt = irred_pert["qpt"]
#        idir = irred_pert["idir"]
#        ipert = irred_pert["ipert"]
#        return vars


def yaml_irred_perts(filename, tag="<KPOINTS>"):
    end_tag = tag.replace("<", "</")

    with open(filename, "r") as fh:
        lines = fh.readlines()

    start, end = None, None
    for i, line in enumerate(lines):
        if tag in line:
            start = i
        elif end_tag in line:
            end = i
            break

    if start is None or end is None:
        raise ValueError("%s\n does not contain any valid %s section" % (filename, tag))
                                                                                                             
    if start == end:
        # Empy section ==> User didn't enable Yaml support in ABINIT.
        raise ValueError("%s\n contains an empty RUN_HINTS section. Enable Yaml support in ABINIT" % filename)

    s = "".join(lines[start+1:end])

    try:
        d = yaml.load(s)

    except Exception as exc:
        raise ValueError("Malformatted Yaml document in file %s:\n %s" % (filename, str(exc)))

    return d["irred_perts"]


class PhononWorkflow(Workflow):
    """
    This workflow is used for parallelizing the calculation of the 
    q-points of the screening. It also provides a on_all_ok method 
    that calls mrgddb to merge the partial DDB files.
    """
    def on_all_ok(self):
        """
        This method is called when all the q-points have been computed.
        Ir runs `mrgddb` in sequential on the local machine to produce
        the final DDB file in the outdir of the `Workflow`.
        """
        ddb_files = filter(None, [task.outdir.has_abiext("DDB") for task in self])

        msg = "on_all_ok: will call mrgddb to merge %s:\n" % str(ddb_files)
        logger.debug(msg)
        assert len(ddb_files) == len(self)

        #if len(ddb_files) == 1:
        # Avoid the merge. Just move the DDB file to the outdir of the workflow

        # Final DDB file will be produced in the outdir of the workflow.
        out_ddb = self.outdir.path_in("out_DDB")
        desc = "DDB file merged by %s on %s" % (self.__class__.__name__, time.asctime())

        mrgddb = Mrgddb(verbose=1)
        mrgddb.merge(ddb_files, out_ddb=out_dbb, description=desc, cwd=self.outdir.path)

        results = dict(
            returncode=0,
            message="DDB merge done",
        )

        return results


def cbk_qptdm_workflow(flow, work, cbk_data):
    scr_input = cbk_data["input"]
    # Use the WFK file produced by the second 
    # Task in the first Workflow (NSCF step).
    nscf_task = flow[0][1]
    wfk_file = nscf_task.outdir.has_abiext("WFK")

    work.fill(wfk_file, scr_input)
    #work.add_deps(cbk.deps)
    work.connect_signals()
    work.build()

    return work


def g0w0_flow_with_qptdm(workdir, manager, scf_input, nscf_input, scr_input, sigma_input):
    """
    Build an `AbinitFlow` for one-shot G0W0 calculations.
    The computation of the q-points for the screening is done with qptdm.

    Args:
        workdir:
            Working directory.
        manager:
            `TaskManager` object used to submit the jobs
        scf_input:
            Input for the GS SCF run.
        nscf_input:
            Input for the NSCF run (band structure run).
        scr_input:
            Input for the SCR run.
        sigma_input:
            Input for the SIGMA run.

    Returns:
        `AbinitFlow`
    """                                                      
    # Create the container that will manage the different workflows.
    flow = AbinitFlow(workdir, manager)

    # Register the first workflow (GS + NSCF calculation)
    bands_work = flow.register_work(BandStructureWorkflow(scf_input, nscf_input))

    assert not bands_work.scf_task.depends_on(bands_work.scf_task)
    assert bands_work.nscf_task.depends_on(bands_work.scf_task)

    # Register the callback that will be executed the workflow for the SCR with qptdm.
    scr_work = flow.register_cbk(cbk=cbk_qptdm_workflow, cbk_data={"input": scr_input},
                                 deps={bands_work.nscf_task: "WFK"}, work_class=QptdmWorkflow
                                )
                             
    assert scr_work.depends_on(bands_work.nscf_task)
    assert not scr_work.depends_on(bands_work.scf_task)

    # The last workflow contains a single SIGMA task that will use 
    # the data produced in the previous two workflows.
    flow.register_task(sigma_input, deps={bands_work.nscf_task: "WFK", scr_work: "SCR"})

    flow.allocate()
    assert sigma_task.depends_on(bands_work.nscf_task)
    assert not sigma_task.depends_on(bands_work.scf_task)
    assert sigma_task.depends_on(scr_work)

    flow.show_dependencies()
    print("sigma_work.deps", sigma_work.deps)
    print("sigma_task.deps", sigma_task.deps)

    return flow