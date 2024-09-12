# -*- coding: utf-8 -*-
# emacs: -*- mode: python; py-indent-offset: 4; indent-tabs-mode: nil -*-
# vi: set ft=python sts=4 ts=4 sw=4 et:
"""
Interfaces for handling BIDS-like neuroimaging structures
^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^

Fetch some example data:

    >>> import os
    >>> from niworkflows import data
    >>> data_root = data.get_bids_examples(variant='BIDS-examples-1-enh-ds054')
    >>> os.chdir(data_root)

Disable warnings:

    >>> from nipype import logging
    >>> logging.getLogger('nipype.interface').setLevel('ERROR')

"""

import os.path as op
import shutil
from glob import glob
from pathlib import Path

import nibabel as nb
from nipype import logging
from nipype.interfaces.base import (
    BaseInterfaceInputSpec,
    File,
    SimpleInterface,
    TraitedSpec,
    traits,
)
from nipype.utils.filemanip import split_filename

from .bids import get_bids_params
from .dsi_studio import btable_from_bvals_bvecs
from .images import ConformDwi, to_lps
from .mrtrix import _convert_fsl_to_mrtrix

LOGGER = logging.getLogger("nipype.interface")


class QSIPrepDWIIngressInputSpec(BaseInterfaceInputSpec):
    # DWI files
    dwi_file = File(exists=True)
    bval_file = File(exists=True)
    bvec_file = File(exists=True)
    b_file = File(exists=True)
    atlas_names = traits.List()


class QSIPrepDWIIngressOutputSpec(TraitedSpec):
    subject_id = traits.Str()
    session_id = traits.Str()
    space_id = traits.Str()
    acq_id = traits.Str()
    rec_id = traits.Str()
    run_id = traits.Str()
    dir_id = traits.Str()
    bval_file = File(exists=True)
    bvec_file = File(exists=True)
    b_file = File(exists=True)
    btable_file = File(exists=True)
    confounds_file = File(exists=True)
    dwi_file = File(exists=True)
    local_bvec_file = File()
    mask_file = File()
    dwi_ref = File(exists=True)
    qc_file = File(exists=True)
    slice_qc_file = File(exists=True)


class QSIPrepDWIIngress(SimpleInterface):
    input_spec = QSIPrepDWIIngressInputSpec
    output_spec = QSIPrepDWIIngressOutputSpec

    def _run_interface(self, runtime):
        params = get_bids_params(self.inputs.dwi_file)
        self._results = {key: val for key, val in list(params.items()) if val is not None}
        space = self._results.get("space_id")
        if space is None:
            raise Exception("Unable to detect space of %s" % self.inputs.dwi_file)

        # Find the additional files
        out_root, fname, _ = split_filename(self.inputs.dwi_file)
        self._results["bval_file"] = op.join(out_root, fname + ".bval")
        self._results["bvec_file"] = op.join(out_root, fname + ".bvec")
        self._get_if_exists("confounds_file", op.join(out_root, "*confounds.tsv"))
        self._get_if_exists("local_bvec_file", op.join(out_root, fname[:-3] + "bvec.nii*"))
        self._get_if_exists("b_file", op.join(out_root, fname + ".b"))
        self._get_if_exists("mask_file", op.join(out_root, fname[:-11] + "brain_mask.nii*"))
        self._get_if_exists("dwi_ref", op.join(out_root, fname[:-16] + "dwiref.nii*"))
        self._results["dwi_file"] = self.inputs.dwi_file

        # Image QC doesn't include space
        self._get_if_exists("qc_file", self._get_qc_filename(out_root, params, "ImageQC", "csv"))
        self._get_if_exists(
            "slice_qc_file", self._get_qc_filename(out_root, params, "SliceQC", "json")
        )

        # Get the anatomical data
        path_parts = out_root.split(op.sep)[:-1]  # remove "dwi"
        # Anat is above ses
        if path_parts[-1].startswith("ses"):
            path_parts.pop()
        return runtime

    def _get_if_exists(self, name, pattern, multi_ok=False):
        files = glob(pattern)
        if len(files) == 1:
            self._results[name] = files[0]
        if len(files) > 1 and multi_ok:
            self._results[name] = files[0]

    def _get_qc_filename(self, out_root, params, desc, suffix):
        used_keys = ["subject_id", "session_id", "acq_id", "dir_id", "run_id"]
        fname = "_".join([params[key] for key in used_keys if params[key]])
        return out_root + "/" + fname + "_desc-%s_dwi.%s" % (desc, suffix)
    