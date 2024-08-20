#!/usr/bin/env python
# -*- coding: utf-8 -*-
# emacs: -*- mode: python; py-indent-offset: 4; indent-tabs-mode: nil -*-
# vi: set ft=python sts=4 ts=4 sw=4 et:

from pathlib import Path

import nipype.interfaces.io as nio
from nipype.interfaces import afni, ants, mrtrix3
from nipype.interfaces import utility as niu
from nipype.interfaces.base import traits
from nipype.pipeline import engine as pe
from niworkflows.engine.workflows import LiterateWorkflow as Workflow
from pkg_resources import resource_filename as pkgrf

from ... import config
from ...interfaces.anatomical import (
    GetTemplate,
    HCPAnatomicalIngress,
    QSIPrepAnatomicalIngress,
    UKBAnatomicalIngress,
    VoxelSizeChooser,
)
from ...interfaces.ants import ConvertTransformFile
from ...interfaces.bids import DerivativesDataSink
from ...interfaces.freesurfer import find_fs_path
from ...interfaces.gradients import ExtractB0s
from ...interfaces.interchange import (
    anatomical_workflow_outputs,
    FS_FILES_TO_REGISTER,
    qsiprep_highres_anatomical_ingressed_fields,
    recon_workflow_input_fields,
)
from ...interfaces.mrtrix import GenerateMasked5tt, ITKTransformConvert, TransformHeader
from ...utils.bids import clean_datasinks
from qsirecon.interfaces.utils import GetConnectivityAtlases

# Required freesurfer files for mrtrix's HSV 5tt generation
HSV_REQUIREMENTS = [
    "mri/aparc+aseg.mgz",
    "mri/brainmask.mgz",
    "mri/norm.mgz",
    "mri/transforms/talairach.xfm",
    "surf/lh.white",
    "surf/lh.pial",
    "surf/rh.white",
    "surf/rh.pial",
]

UKB_REQUIREMENTS = ["T1/T1_brain.nii.gz", "T1/T1_brain_mask.nii.gz"]
HCP_REQUIREMENTS = ["T1w/T1w_acpc_dc_restore_brain.nii.gz", "T1w/brainmask_fs.nii.gz"]

# Files that must exist if QSIRecon ran the anatomical workflow
QSIRECON_ANAT_REQUIREMENTS = [
    "sub-{subject_id}/anat/sub-{subject_id}_desc-brain_mask.nii.gz",
    "sub-{subject_id}/anat/sub-{subject_id}_desc-preproc_T1w.nii.gz",
]

QSIRECON_NORMALIZED_ANAT_REQUIREMENTS = [
    "sub-{subject_id}/anat/sub-{subject_id}_from-T1w_to-MNI152NLin2009cAsym_mode-image_xfm.h5",
    "sub-{subject_id}/anat/sub-{subject_id}_from-MNI152NLin2009cAsym_to-T1w_mode-image_xfm.h5",
]


def init_highres_recon_anatomical_wf(
    subject_id,
    extras_to_make,
    needs_t1w_transform,
):
    """Gather any high-res anatomical data (images, transforms, segmentations) to use
    in recon workflows.

    This workflow searches through input data to see what anatomical data is available.
    The anatomical data may be in a freesurfer directory.

    """

    workflow = Workflow(name="recon_anatomical_wf")
    outputnode = pe.Node(
        niu.IdentityInterface(fields=anatomical_workflow_outputs), name="outputnode"
    )
    workflow.__desc__ = ""
    # "Gather" the input data. ``status`` is a dict that reflects which anatomical data
    # are present. The anat_ingress_node is a nipype node that ensures that qsiprep-style
    # anatomical data is available. In the case where ``pipeline_source`` is not "qsiprep",
    # the data is converted in this node to be qsiprep-like.
    pipeline_source = config.workflow.recon_input_pipeline
    freesurfer_dir = config.execution.freesurfer_input
    qsirecon_suffix = "anat"
    if pipeline_source == "qsiprep":
        anat_ingress_node, status = gather_qsiprep_anatomical_data(subject_id)
    elif pipeline_source == "ukb":
        anat_ingress_node, status = gather_ukb_anatomical_data(subject_id)
    elif pipeline_source == "hcpya":
        anat_ingress_node, status = gather_hcp_anatomical_data(subject_id)
    else:
        raise Exception(f"Unknown pipeline source '{pipeline_source}'")
    anat_ingress_node.inputs.infant_mode = config.workflow.infant
    if needs_t1w_transform and not status["has_qsiprep_t1w_transforms"]:
        raise Exception("Cannot compute to-template")

    # If there is no high-res anat data in the inputs there may still be an image available
    # from freesurfer. Check for it:
    subject_freesurfer_path = find_fs_path(freesurfer_dir, subject_id)
    status["has_freesurfer"] = subject_freesurfer_path is not None

    # If no high-res are available, we're done here
    if not status["has_qsiprep_t1w"] and subject_freesurfer_path is None:
        config.loggers.workflow.warning(
            f"No high-res anatomical data available directly in recon inputs for {subject_id}."
        )
        # If a 5tt image is needed, this is an error
        if "mrtrix_5tt_hsvs" in extras_to_make:
            raise Exception("FreeSurfer data is required to make HSVS 5tt image.")
        workflow.add_nodes([outputnode])
        workflow = clean_datasinks(workflow, qsirecon_suffix)
        return workflow, status

    config.loggers.workflow.info(
        f"Found high-res anatomical data in preprocessed inputs for {subject_id}."
    )
    workflow.connect([
        (anat_ingress_node, outputnode,
            [(name, name) for name in qsiprep_highres_anatomical_ingressed_fields])])  # fmt:skip

    # grab un-coregistered freesurfer images later use
    if subject_freesurfer_path is not None:
        status["has_freesurfer"] = True
        config.loggers.workflow.info(
            "Freesurfer directory %s exists for %s", subject_freesurfer_path, subject_id
        )
        fs_source = pe.Node(
            nio.FreeSurferSource(
                subjects_dir=str(subject_freesurfer_path.parent),
                subject_id=subject_freesurfer_path.name,
            ),
            name="fs_source",
        )
        # workflow.connect([
        #     (fs_source, outputnode, [
        #         (field, field) for field in FS_FILES_TO_REGISTER])])  # fmt:skip

    # Do we need to calculate anything else on the
    if "mrtrix_5tt_hsvs" in extras_to_make:
        # Check for specific files needed for HSVs.
        missing_fs_hsvs_files = check_hsv_inputs(Path(subject_freesurfer_path))
        if missing_fs_hsvs_files:
            raise Exception(" ".join(missing_fs_hsvs_files) + "are missing: unable to make a HSV.")

        config.loggers.workflow.info("FreeSurfer data will be used to create a HSVS 5tt image.")
        status["has_freesurfer_5tt_hsvs"] = True
        create_5tt_hsvs = pe.Node(
            GenerateMasked5tt(
                algorithm="hsvs",
                in_file=str(subject_freesurfer_path),
                nthreads=config.nipype.omp_nthreads,
            ),
            name="create_5tt_hsvs",
            n_procs=config.nipype.omp_nthreads,
        )
        ds_qsiprep_5tt_hsvs = pe.Node(
            DerivativesDataSink(
                dismiss_entities=("desc",),
                atlas="hsvs",
                space="T1w",
                suffix="dseg",
            ),
            name="ds_qsiprep_5tt_hsvs",
            run_without_submitting=True,
        )
        ds_fs_5tt_hsvs = pe.Node(
            DerivativesDataSink(
                dismiss_entities=("desc",),
                desc="hsvs",
                space="fsnative",
                suffix="dseg",
                compress=True,
            ),
            name="ds_fs_5tt_hsvs",
            run_without_submitting=True,
        )
        workflow.connect([
            (anat_ingress_node, ds_fs_5tt_hsvs, [("t1_preproc", "source_file")]),
            (anat_ingress_node, ds_qsiprep_5tt_hsvs, [("t1_preproc", "source_file")]),
            (create_5tt_hsvs, outputnode, [('out_file', 'fs_5tt_hsvs')]),
            (create_5tt_hsvs, ds_fs_5tt_hsvs, [("out_file", "in_file")]),
        ])  # fmt:skip

        # Transform the 5tt image so it's registered to the QSIRecon AC-PC T1w
        if status["has_qsiprep_t1w"]:
            config.loggers.workflow.info(
                "HSVS 5tt imaged will be registered to the QSIRecon T1w image."
            )
            status["has_qsiprep_5tt_hsvs"] = True
            register_fs_to_qsiprep_wf = init_register_fs_to_qsiprep_wf(
                use_qsiprep_reference_mask=True
            )
            apply_header_to_5tt = pe.Node(TransformHeader(), name="apply_header_to_5tt")
            workflow.connect([
                (anat_ingress_node, register_fs_to_qsiprep_wf, [
                    ("t1_preproc", "inputnode.qsiprep_reference_image"),
                    ("t1_brain_mask", "inputnode.qsiprep_reference_mask")]),
                (fs_source, register_fs_to_qsiprep_wf, [
                    (field, "inputnode." + field) for field in FS_FILES_TO_REGISTER]),
                (register_fs_to_qsiprep_wf, outputnode, [
                    ("outputnode.fs_to_qsiprep_transform_mrtrix",
                        "fs_to_qsiprep_transform_mrtrix"),
                    ("outputnode.fs_to_qsiprep_transform_itk",
                        "fs_to_qsiprep_transform_itk")] + [
                    ("outputnode." + field, field) for field in FS_FILES_TO_REGISTER]),
                (create_5tt_hsvs, apply_header_to_5tt, [("out_file", "in_image")]),
                (register_fs_to_qsiprep_wf, apply_header_to_5tt, [
                    ("outputnode.fs_to_qsiprep_transform_mrtrix", "transform_file")]),
                (apply_header_to_5tt, outputnode, [
                    ("out_image", "qsiprep_5tt_hsvs")]),
                (apply_header_to_5tt, ds_qsiprep_5tt_hsvs, [("out_image", "in_file")]),
            ])  # fmt:skip
            workflow.__desc__ += "A hybrid surface/volume segmentation was created [Smith 2020]."

    workflow = clean_datasinks(workflow, qsirecon_suffix)
    return workflow, status


def gather_ukb_anatomical_data(subject_id):
    """
    Check a UKB directory for the necessary files for recon workflows.

    Parameters
    ----------
    subject_id : str
        List of subject labels

    """
    status = {
        "has_qsiprep_5tt_hsvs": False,
        "has_freesurfer_5tt_hsvs": False,
        "has_freesurfer": False,
    }
    recon_input_dir = config.execution.bids_dir

    # Check to see if we have a T1w preprocessed by QSIRecon
    missing_ukb_anats = check_ukb_anatomical_outputs(recon_input_dir)
    has_t1w = not missing_ukb_anats
    status["has_qsiprep_t1w"] = has_t1w
    if missing_ukb_anats:
        config.loggers.workflow.info(f"Missing T1w from UKB session: {recon_input_dir}")
    else:
        config.loggers.workflow.info("Found usable UKB-preprocessed T1w image and mask.")
    anat_ingress = pe.Node(
        UKBAnatomicalIngress(subject_id=subject_id, recon_input_dir=recon_input_dir),
        name="ukb_anat_ingress",
    )

    # I couldn't figure out how to convert UKB transforms to ants. So
    # they're not available for recon workflows for now
    status["has_qsiprep_t1w_transforms"] = False
    config.loggers.workflow.info("QSIRecon can't read FNIRT transforms from UKB at this time.")

    return anat_ingress, status


def gather_hcp_anatomical_data(subject_id):
    """
    Check a HCP directory for the necessary files for recon workflows.

    Parameters
    ----------
    subject_id : str
        List of subject labels

    """
    status = {
        "has_qsiprep_5tt_hsvs": False,
        "has_freesurfer_5tt_hsvs": False,
        "has_freesurfer": False,
    }
    recon_input_dir = config.execution.bids_dir

    # Check to see if we have a T1w preprocessed by QSIRecon
    missing_hcp_anats = check_hcp_anatomical_outputs(recon_input_dir)
    has_t1w = not missing_hcp_anats
    status["has_qsiprep_t1w"] = has_t1w
    if missing_hcp_anats:
        config.loggers.workflow.info(f"Missing T1w from HCP session: {recon_input_dir}")
    else:
        config.loggers.workflow.info("Found usable HCP-preprocessed T1w image and mask.")
    anat_ingress = pe.Node(
        HCPAnatomicalIngress(subject_id=subject_id, recon_input_dir=recon_input_dir),
        name="hcp_anat_ingress",
    )

    # I couldn't figure out how to convert UKB transforms to ants. So
    # they're not available for recon workflows for now
    status["has_qsiprep_t1w_transforms"] = False
    config.loggers.workflow.info("QSIRecon can't read FNIRT transforms from HCP at this time.")

    return anat_ingress, status


def gather_qsiprep_anatomical_data(subject_id):
    """
    Gathers the anatomical data from a QSIPrep input and finds which files are available.


    Parameters
    ----------
    subject_id : str
        List of subject labels

    """
    status = {
        "has_qsiprep_5tt_hsvs": False,
        "has_freesurfer_5tt_hsvs": False,
        "has_freesurfer": False,
    }
    recon_input_dir = config.execution.bids_dir
    # Check to see if we have a T1w preprocessed by QSIPrep
    missing_qsiprep_anats = check_qsiprep_anatomical_outputs(recon_input_dir, subject_id, "T1w")
    has_qsiprep_t1w = not missing_qsiprep_anats
    status["has_qsiprep_t1w"] = has_qsiprep_t1w
    if missing_qsiprep_anats:
        config.loggers.workflow.info(
            "Missing T1w QSIPrep outputs found: %s", " ".join(missing_qsiprep_anats)
        )
    else:
        config.loggers.workflow.info("Found usable QSIPrep-preprocessed T1w image and mask.")
    anat_ingress = pe.Node(
        QSIPrepAnatomicalIngress(subject_id=subject_id, recon_input_dir=recon_input_dir),
        name="qsiprep_anat_ingress",
    )

    # Check if the T1w-to-MNI transforms are in the QSIRecon outputs
    missing_qsiprep_transforms = check_qsiprep_anatomical_outputs(
        recon_input_dir, subject_id, "transforms"
    )
    has_qsiprep_t1w_transforms = not missing_qsiprep_transforms
    status["has_qsiprep_t1w_transforms"] = has_qsiprep_t1w_transforms

    if missing_qsiprep_transforms:
        config.loggers.workflow.info(
            "Missing T1w QSIPrep outputs: %s", " ".join(missing_qsiprep_transforms)
        )

    return anat_ingress, status


def check_hsv_inputs(subj_fs_path):
    """Determine if a FreeSurfer directory has the required files for HSV."""
    missing = []
    for requirement in HSV_REQUIREMENTS:
        if not (subj_fs_path / requirement).exists():
            missing.append(requirement)
    return missing


def _check_zipped_unzipped(path_to_check):
    """Check to see if a path exists and warn if it's gzipped."""

    exists = False
    if path_to_check.exists():
        exists = True
    if path_to_check.name.endswith(".gz"):
        nonzipped = str(path_to_check)[:-3]
        if Path(nonzipped).exists():
            config.loggers.workflow.warn(
                "A Non-gzipped input nifti file was found. Consider gzipping %s", nonzipped
            )
            exists = True
    config.loggers.workflow.info(f"CHECKING {path_to_check}: {exists}")
    return exists


def check_qsiprep_anatomical_outputs(recon_input_dir, subject_id, anat_type):
    """Determines whether an aligned T1w exists in a qsiprep derivatives directory.

    It is possible that:
      - ``--dwi-only`` was used, in which case there is NO T1w available
      - ``--skip-anat-based-spatial-normalization``, there is a T1w but no transform to a template
      - Normal mode, there is a T1w and a transform to template space.
    """
    missing = []
    recon_input_path = Path(recon_input_dir)
    to_check = (
        QSIRECON_ANAT_REQUIREMENTS if anat_type == "T1w" else QSIRECON_NORMALIZED_ANAT_REQUIREMENTS
    )

    for requirement in to_check:
        requirement = requirement.format(subject_id=subject_id)
        t1_version = recon_input_path / requirement
        if _check_zipped_unzipped(t1_version):
            continue

        # Try to see if a T2w version is present
        t2w_version = recon_input_path / requirement.replace("_T1w", "_T2w")
        if _check_zipped_unzipped(t2w_version):
            continue
        missing.append(str(t1_version))

    return missing


def check_ukb_anatomical_outputs(recon_input_dir):
    """Check for required files under a UKB session directory.

    Parameters:

    recon_input_dir: pathlike
        Path to a UKB subject directory (eg 1234567_2_0)
    """
    missing = []
    recon_input_path = Path(recon_input_dir)
    for requirement in UKB_REQUIREMENTS:
        if not (recon_input_path / requirement).exists():
            missing.append(str(requirement))
    return missing


def check_hcp_anatomical_outputs(recon_input_dir):
    """Check for required files under a HCP session directory.

    Parameters:

    recon_input_dir: pathlike
        Path to a HCP subject directory (eg 100307/)
    """
    missing = []
    recon_input_path = Path(recon_input_dir)
    for requirement in HCP_REQUIREMENTS:
        if not (recon_input_path / requirement).exists():
            missing.append(str(requirement))
    return missing


def init_register_fs_to_qsiprep_wf(
    use_qsiprep_reference_mask=False, name="register_fs_to_qsiprep_wf"
):
    """Registers a T1w images from freesurfer to another image and transforms"""
    inputnode = pe.Node(
        niu.IdentityInterface(
            fields=FS_FILES_TO_REGISTER + ["qsiprep_reference_image", "qsiprep_reference_mask"]
        ),
        name="inputnode",
    )
    outputnode = pe.Node(
        niu.IdentityInterface(
            fields=FS_FILES_TO_REGISTER
            + ["fs_to_qsiprep_transform_itk", "fs_to_qsiprep_transform_mrtrix"]
        ),
        name="outputnode",
    )
    workflow = Workflow(name=name)
    workflow.__desc__ = "FreeSurfer outputs were registered to the QSIRecon outputs."

    # Convert the freesurfer inputs so we can register them with ANTs
    convert_fs_brain = pe.Node(
        mrtrix3.MRConvert(out_file="fs_brain.nii", args="-strides -1,-2,3"),
        name="convert_fs_brain",
    )

    # Register the brain to the QSIRecon reference
    ants_settings = pkgrf("qsirecon", "data/freesurfer_to_qsiprep.json")
    register_to_qsiprep = pe.Node(
        ants.Registration(from_file=ants_settings), name="register_to_qsiprep"
    )

    # If there is a mask for the QSIRecon reference image, use it
    if use_qsiprep_reference_mask:
        workflow.connect(inputnode, "qsiprep_reference_mask",
                         register_to_qsiprep, "fixed_image_masks")  # fmt:skip

    # The more recent ANTs mat format isn't compatible with transformconvert.
    # So convert it to ANTs text format with ConvertTransform
    convert_ants_transform = pe.Node(
        ConvertTransformFile(dimension=3), name="convert_ants_transform"
    )

    # Convert from ANTs text format to MRtrix3 format
    convert_ants_to_mrtrix_transform = pe.Node(
        ITKTransformConvert(), name="convert_ants_to_mrtrix_transform"
    )

    # Adjust the headers of all the input images so they're aligned to the qsiprep ref
    transform_nodes = {}
    for image_name in FS_FILES_TO_REGISTER:
        transform_nodes[image_name] = pe.Node(TransformHeader(), name="transform_" + image_name)
        workflow.connect([
            (inputnode, transform_nodes[image_name], [(image_name, "in_image")]),
            (convert_ants_to_mrtrix_transform,
             transform_nodes[image_name], [("out_transform", "transform_file")]),
            (transform_nodes[image_name], outputnode, [("out_image", image_name)])
        ])  # fmt:skip

    workflow.connect([
        (inputnode, convert_fs_brain, [
            ("brain", "in_file")]),
        (inputnode, register_to_qsiprep, [
            ("qsiprep_reference_image", "fixed_image")]),
        (convert_fs_brain, register_to_qsiprep, [
            ("out_file", "moving_image")]),
        (register_to_qsiprep, convert_ants_transform, [
            (("forward_transforms", _get_first), "in_transform")]),
        (register_to_qsiprep, outputnode, [
            ("composite_transform", "fs_to_qsiprep_transform_itk")]),
        (convert_ants_transform, convert_ants_to_mrtrix_transform, [
            ("out_transform", "in_transform")]),
        (convert_ants_to_mrtrix_transform, outputnode,
         [("out_transform", "fs_to_qsiprep_transform_mrtrix")])
    ])  # fmt:skip

    return workflow


def init_dwi_recon_anatomical_workflow(
    atlas_names,
    has_qsiprep_5tt_hsvs,
    needs_t1w_transform,
    has_freesurfer_5tt_hsvs,
    has_qsiprep_t1w,
    has_qsiprep_t1w_transforms,
    has_freesurfer,
    extras_to_make,
    name,
    prefer_dwi_mask=False,
):
    """Ensure that anatomical data is available for the reconstruction workflows.

    This workflow calculates images/transforms that require a DWI spatial reference.
    Specifically, three additional features are added:

      * ``"dwi_mask"``: a brain mask in the voxel space of the DWI
      * ``"atlas_configs"``: A dictionary used by connectivity workflows to get
        brain parcellations.
      * ``"odf_rois"``: An image with some interesting ROIs for plotting ODFs

    Parameters:
    ===========
        has_qsiprep_5tt_hsvs:
        has_freesurfer_5tt_hsvs: True,
        has_qsiprep_t1w:
        has_qsiprep_t1w_transforms: True}
    """
    # Inputnode holds data from the T1w-based anatomical workflow
    inputnode = pe.Node(
        niu.IdentityInterface(fields=recon_workflow_input_fields), name="inputnode"
    )
    connect_from_inputnode = set(recon_workflow_input_fields)
    # Buffer to hold the anatomical files that are calculated here
    buffernode = pe.Node(
        niu.IdentityInterface(fields=recon_workflow_input_fields), name="buffernode"
    )
    connect_from_buffernode = set()
    b0_threshold = config.workflow.b0_threshold
    qsirecon_suffix = "anat"

    def _get_source_node(fieldname):
        if fieldname in connect_from_inputnode:
            return inputnode
        if fieldname in connect_from_buffernode:
            return buffernode
        raise Exception("Can't determine location of " + fieldname)

    def _exchange_fields(fields):
        connect_from_inputnode.difference_update(fields)
        connect_from_buffernode.update(fields)

    # These are always created here
    _exchange_fields(["dwi_mask", "atlas_configs", "odf_rois", "resampling_template"])

    outputnode = pe.Node(
        niu.IdentityInterface(fields=recon_workflow_input_fields), name="outputnode"
    )
    workflow = Workflow(name=name)
    skull_strip_method = "antsBrainExtraction"
    desc = ""

    def _get_status():
        return {
            "has_qsiprep_5tt_hsvs": has_qsiprep_5tt_hsvs,
            "has_freesurfer_5tt_hsvs": has_freesurfer_5tt_hsvs,
            "has_qsiprep_t1w": has_qsiprep_t1w,
            "has_qsiprep_t1w_transforms": has_qsiprep_t1w_transforms,
        }

    # XXX: This is a temporary solution until QSIRecon supports flexible output spaces.
    get_template = pe.Node(
        GetTemplate(
            template_name="MNI152NLin2009cAsym" if not config.workflow.infant else "MNIInfant",
        ),
        name="get_template",
    )
    mask_template = pe.Node(
        afni.Calc(expr="a*b", outputtype="NIFTI_GZ"),
        name="mask_template",
    )
    reorient_to_lps = pe.Node(
        afni.Resample(orientation="RAI", outputtype="NIFTI_GZ"),
        name="reorient_to_lps",
    )

    reference_grid_wf = init_output_grid_wf()
    workflow.connect([
        (get_template, mask_template, [
            ('template_file', 'in_file_a'),
            ('mask_file', 'in_file_b'),
        ]),
        (mask_template, reorient_to_lps, [('out_file', 'in_file')]),
        (inputnode, reference_grid_wf, [('dwi_ref', 'inputnode.input_image')]),
        (reorient_to_lps, reference_grid_wf, [('out_file', 'inputnode.template_image')]),
        (reference_grid_wf, buffernode, [('outputnode.grid_image', 'resampling_template')]),
    ])  # fmt:skip

    # Missing Freesurfer AND QSIRecon T1ws, or the user wants a DWI-based mask
    if not (has_qsiprep_t1w or has_freesurfer) or prefer_dwi_mask:
        desc += (
            "No T1w weighted images were available for masking, so a mask "
            "was estimated based on the b=0 images in the DWI data itself."
        )
        extract_b0s = pe.Node(ExtractB0s(b0_threshold=b0_threshold), name="extract_b0s")
        mask_b0s = pe.Node(afni.Automask(outputtype="NIFTI_GZ"), name="mask_b0s")
        workflow.connect([
            (inputnode, extract_b0s, [
                ("dwi_file", "dwi_series"),
                ("bval_file", "bval_file")]),
            (extract_b0s, mask_b0s, [("b0_series", "in_file")]),
            (mask_b0s, outputnode, [("out_file", "dwi_mask")]),
            (inputnode, outputnode, [(field, field) for field in connect_from_inputnode])
        ])  # fmt:skip
        workflow = clean_datasinks(workflow, qsirecon_suffix)
        return workflow, _get_status()

    # No data from QSIRecon was available, BUT we have freesurfer! register it and
    # get the brain, masks and possibly a to-MNI transform.
    # --> If has_freesurfer AND has qsiprep_t1w, the necessary files were created earlier
    elif has_freesurfer and not has_qsiprep_t1w:
        fs_source = pe.Node(
            nio.FreeSurferSource(subjects_dir=config.execution.fs_subjects_dir), name="fs_source"
        )
        # Register the FreeSurfer brain to the DWI reference
        desc += (
            "A brainmasked T1w image from FreeSurfer was registered to the "
            "preprocessed DWI data. Brainmasks from FreeSurfer were used in all "
            "subsequent reconstruction steps. "
        )

        # Move these fields to the buffernode
        _exchange_fields(
            FS_FILES_TO_REGISTER
            + [
                "t1_brain_mask",
                "t1_preproc",
                "fs_to_qsiprep_transform_mrtrix",
                "fs_to_qsiprep_transform_itk",
            ]
        )

        # Perform the registration and connect the outputs to buffernode
        # NOTE: using FreeSurfer "brain" image as t1_preproc and aseg as the brainmask
        has_qsiprep_t1w = True
        register_fs_to_qsiprep_wf = init_register_fs_to_qsiprep_wf(
            use_qsiprep_reference_mask=False
        )
        workflow.connect([
            (inputnode, fs_source, [("subject_id", "subject_id")]),
            (inputnode, register_fs_to_qsiprep_wf, [
                ("dwi_ref", "inputnode.qsiprep_reference_image")]),
            (fs_source, register_fs_to_qsiprep_wf, [
                (field, "inputnode." + field) for field in FS_FILES_TO_REGISTER]),
            (register_fs_to_qsiprep_wf, buffernode, [
                ("outputnode.brain", "t1_preproc"),
                ("outputnode.aseg", "t1_brain_mask"),
                ("outputnode.fs_to_qsiprep_transform_mrtrix",
                    "fs_to_qsiprep_transform_mrtrix"),
                ("outputnode.fs_to_qsiprep_transform_itk",
                    "fs_to_qsiprep_transform_itk")] + [
                ("outputnode." + field, field) for field in FS_FILES_TO_REGISTER]),
        ])  # fmt:skip

    # Do we need to transform the 5tt hsvs from fsnative?
    if "mrtrix_5tt_hsvs" in extras_to_make and not has_qsiprep_5tt_hsvs:
        # Transform the 5tt image so it's registered to the QSIRecon AC-PC T1w
        config.loggers.workflow.info(
            "HSVS 5tt imaged will be registered to the " "QSIRecon dwiref image."
        )
        _exchange_fields(["qsiprep_5tt_hsvs"])
        if not has_freesurfer_5tt_hsvs:
            raise Exception("The 5tt image in fsnative should have been created by now")
        apply_header_to_5tt_hsvs = pe.Node(TransformHeader(), name="apply_header_to_5tt_hsvs")
        ds_qsiprep_5tt_hsvs = pe.Node(
            DerivativesDataSink(
                dismiss_entities=("desc",),
                atlas="hsvs",
                suffix="dseg",
            ),
            name="ds_qsiprep_5tt_hsvs",
            run_without_submitting=True,
        )
        workflow.connect([
            (inputnode, apply_header_to_5tt_hsvs, [("fs_5tt_hsvs", "in_image")]),
            (apply_header_to_5tt_hsvs, buffernode, [
                ("out_image", "qsiprep_5tt_hsvs")]),
            (apply_header_to_5tt_hsvs, ds_qsiprep_5tt_hsvs, [("out_image", "in_file")]),
        ])  # fmt:skip
        desc += "A hybrid surface/volume segmentation was created [Smith 2020]."

    # If we have transforms to the template space, use them to get ROIs/atlases
    # if not has_qsiprep_t1w_transforms and has_qsiprep_t1w:
    #     desc += "In order to warp brain parcellations from template space into " \
    #         "alignment with the DWI data, the DWI-aligned FreeSurfer brain was " \
    #         "registered to template space. "

    #     # We now have qsiprep t1w and transforms!!
    #     has_qsiprep_t1w = has_qsiprep_t1w_transforms = True
    #     # Calculate the transforms here:
    #     has_qsiprep_t1w_transforms = True
    #     _exchange_fields(['t1_2_mni_forward_transform', 't1_2_mni_reverse_transform'])
    #     t1_2_mni = pe.Node(
    #         get_t1w_registration_node(
    #             infant_mode, sloppy or not atlas_names, omp_nthreads),
    #         name="t1_2_mni")
    #     workflow.connect([
    #         (_get_source_node("t1_preproc"), t1_2_mni, [('t1_preproc', 'moving_image')]),
    #         (t1_2_mni, buffernode, [
    #             ("composite_transform", "t1_2_mni_forward_transform"),
    #             ("inverse_composite_transform", "t1_2_mni_reverse_transform")
    #         ])
    #     ])  # fmt:skip
    #     # TODO: add datasinks here

    # Check the status of the T1wACPC-to-template transforms
    if needs_t1w_transform:
        if has_qsiprep_t1w_transforms:
            config.loggers.workflow.info("Found T1w-to-template transforms from QSIRecon")
            desc += (
                "T1w-based spatial normalization calculated during "
                "preprocessing was used to map atlases from template space into "
                "alignment with DWIs."
            )
        else:
            raise Exception(
                "Reconstruction workflow requires a T1wACPC-to-template transform. "
                "None were found."
            )

    # Simply resample the T1w mask into the DWI resolution. This was the default
    # up to version 0.14.3
    if has_qsiprep_t1w and not prefer_dwi_mask:
        desc += "Brainmasks from {} were used in all " "subsequent reconstruction steps.".format(
            skull_strip_method
        )
        # Resample anat mask
        resample_mask = pe.Node(
            ants.ApplyTransforms(
                dimension=3, transforms=["identity"], interpolation="NearestNeighbor"
            ),
            name="resample_mask",
        )

        workflow.connect([
            (inputnode, resample_mask, [
                ("t1_brain_mask", "input_image"),
                ("dwi_ref", "reference_image")]),
            (resample_mask, buffernode, [("output_image", "dwi_mask")])
        ])  # fmt:skip

    if has_qsiprep_t1w_transforms:
        config.loggers.workflow.info("Transforming ODF ROIs into DWI space for visual report.")
        # Resample ROI targets to DWI resolution for ODF plotting
        crossing_rois_file = pkgrf("qsirecon", "data/crossing_rois.nii.gz")
        odf_rois = pe.Node(
            ants.ApplyTransforms(interpolation="MultiLabel", dimension=3), name="odf_rois"
        )
        odf_rois.inputs.input_image = crossing_rois_file
        workflow.connect([
            (_get_source_node('t1_2_mni_reverse_transform'), odf_rois, [
                ('t1_2_mni_reverse_transform', 'transforms')]),
            (inputnode, odf_rois, [
                ("dwi_file", "reference_image")]),
            (odf_rois, buffernode, [("output_image", "odf_rois")])
        ])  # fmt:skip

        # Similarly, if we need atlases, transform them into DWI space
        if atlas_names:
            desc += (
                "Cortical parcellations were mapped from template space to DWIS "
                "using the T1w-based spatial normalization. "
            )

            # Resample all atlases to dwi_file's resolution
            get_atlases = pe.Node(
                GetConnectivityAtlases(atlas_names=atlas_names),
                name="get_atlases",
                run_without_submitting=True,
            )
            workflow.connect([
                (_get_source_node('t1_2_mni_reverse_transform'), get_atlases, [
                    ('t1_2_mni_reverse_transform', 'forward_transform')]),
                (get_atlases, buffernode, [
                    ("atlas_configs", "atlas_configs")]),
                (inputnode, get_atlases, [
                    ('dwi_file', 'reference_image')])
            ])  # fmt:skip

        for atlas in atlas_names:
            workflow.connect([
                (
                    get_atlases,
                    pe.Node(
                        DerivativesDataSink(
                            dismiss_entities=("desc",),
                            atlas=atlas,
                            suffix="dseg",
                            compress=True,
                        ),
                        name=f'ds_atlas_{atlas}',
                        run_without_submitting=True), [
                            (
                                ('atlas_configs',
                                 _get_resampled, atlas, 'dwi_resolution_file'),
                                'in_file')]),
                (
                    get_atlases,
                    pe.Node(
                        DerivativesDataSink(
                            dismiss_entities=("desc",),
                            atlas=atlas,
                            suffix="dseg",
                            extension=".mif.gz",
                            compress=True,
                        ),
                        name=f'ds_atlas_mifs_{atlas}',
                        run_without_submitting=True), [
                            (
                                ('atlas_configs',
                                 _get_resampled, atlas, 'dwi_resolution_mif'),
                                'in_file')]),
                (
                    get_atlases,
                    pe.Node(
                        DerivativesDataSink(
                            dismiss_entities=("desc",),
                            atlas=atlas,
                            suffix="dseg",
                            extension=".txt",
                        ),
                        name=f'ds_atlas_mrtrix_lut_{atlas}',
                        run_without_submitting=True), [
                            (
                                ('atlas_configs',
                                 _get_resampled, atlas, 'mrtrix_lut'),
                                'in_file')]),
                (
                    get_atlases,
                    pe.Node(
                        DerivativesDataSink(
                            dismiss_entities=("desc",),
                            atlas=atlas,
                            suffix="dseg",
                            extension=".txt",
                        ),
                        name=f'ds_atlas_orig_lut_{atlas}',
                        run_without_submitting=True), [
                            (
                                ('atlas_configs',
                                 _get_resampled, atlas, 'orig_lut'),
                                'in_file')]),
            ])  # fmt:skip

        # Fill in the atlas datasinks
        for node in workflow.list_node_names():
            node_suffix = node.split(".")[-1]
            if node_suffix.startswith("ds_atlas_"):
                workflow.connect([
                    (inputnode, workflow.get_node(node), [("dwi_file", "source_file")]),
                ])  # fmt:skip

    if "mrtrix_5tt_hsv" in extras_to_make and not has_qsiprep_5tt_hsvs:
        raise Exception("Unable to create a 5tt HSV image given input data.")

    # Directly connect anything from the inputs that we haven't created here
    workflow.connect([
        (inputnode, outputnode, [(name, name) for name in connect_from_inputnode]),
        (buffernode, outputnode, [(name, name) for name in connect_from_buffernode])
    ])  # fmt:skip
    workflow = clean_datasinks(workflow, qsirecon_suffix)
    return workflow, _get_status()


def _get_first(item):
    if isinstance(item, (list, tuple)):
        return item[0]
    return item


def _get_resampled(atlas_configs, atlas_name, to_retrieve):
    return atlas_configs[atlas_name][to_retrieve]


def init_output_grid_wf() -> Workflow:
    """Generate a non-oblique, uniform voxel-size grid around a brain."""
    workflow = Workflow(name="output_grid_wf")
    inputnode = pe.Node(
        niu.IdentityInterface(fields=["template_image", "input_image"]), name="inputnode"
    )
    outputnode = pe.Node(niu.IdentityInterface(fields=["grid_image"]), name="outputnode")
    # Create the output reference grid_image
    if config.workflow.output_resolution is None:
        voxel_size = traits.Undefined
    else:
        voxel_size = config.workflow.output_resolution
    padding = 4 if config.workflow.infant else 8

    autobox_template = pe.Node(
        afni.Autobox(outputtype="NIFTI_GZ", padding=padding), name="autobox_template"
    )
    deoblique_autobox = pe.Node(
        afni.Warp(outputtype="NIFTI_GZ", deoblique=True), name="deoblique_autobox"
    )
    voxel_size_chooser = pe.Node(
        VoxelSizeChooser(voxel_size=voxel_size), name="voxel_size_chooser"
    )
    resample_to_voxel_size = pe.Node(
        afni.Resample(outputtype="NIFTI_GZ"), name="resample_to_voxel_size"
    )

    workflow.connect([
        (inputnode, autobox_template, [('template_image', 'in_file')]),
        (autobox_template, deoblique_autobox, [('out_file', 'in_file')]),
        (deoblique_autobox, resample_to_voxel_size, [('out_file', 'in_file')]),
        (resample_to_voxel_size, outputnode, [('out_file', 'grid_image')]),
        (inputnode, voxel_size_chooser, [('input_image', 'input_image')]),
        (voxel_size_chooser, resample_to_voxel_size, [(('voxel_size', _tupleize), 'voxel_size')])
    ])  # fmt:skip

    return workflow


def _tupleize(value):
    # Nipype did not like having a Tuple output trait
    return (value, value, value)
