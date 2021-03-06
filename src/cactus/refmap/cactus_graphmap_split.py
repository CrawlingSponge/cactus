#!/usr/bin/env python3

"""This sciprt will take the cactus-graphmap output and input and split it up into chromosomes.  This is done after a whole-genome graphmap as we need the whole-genome minigraph alignments to do the chromosome splitting in the first place.  For each chromosome, it will make a PAF, GFA and Seqfile (pointing to chrosome fastas)
"""
import os
from argparse import ArgumentParser
import xml.etree.ElementTree as ET
import copy
import timeit

from operator import itemgetter

from Bio import SeqIO
from Bio.SeqRecord import SeqRecord

from cactus.progressive.seqFile import SeqFile
from cactus.progressive.multiCactusTree import MultiCactusTree
from cactus.shared.common import setupBinaries, importSingularityImage
from cactus.progressive.multiCactusProject import MultiCactusProject
from cactus.shared.experimentWrapper import ExperimentWrapper
from cactus.progressive.schedule import Schedule
from cactus.progressive.projectWrapper import ProjectWrapper
from cactus.shared.common import cactusRootPath
from cactus.shared.configWrapper import ConfigWrapper
from cactus.pipeline.cactus_workflow import CactusWorkflowArguments
from cactus.pipeline.cactus_workflow import addCactusWorkflowOptions
from cactus.pipeline.cactus_workflow import CactusTrimmingBlastPhase
from cactus.shared.common import makeURL, catFiles
from cactus.shared.common import enableDumpStack
from cactus.shared.common import cactus_override_toil_options
from cactus.shared.common import cactus_call
from cactus.shared.common import getOptionalAttrib, findRequiredNode
from cactus.shared.common import unzip_gz, write_s3
from toil.job import Job
from toil.common import Toil
from toil.lib.bioio import logger
from toil.lib.bioio import setLoggingFromOptions
from toil.realtimeLogger import RealtimeLogger
from toil.lib.threading import cpu_count

from sonLib.nxnewick import NXNewick
from sonLib.bioio import getTempDirectory, getTempFile

def main():
    parser = ArgumentParser()
    Job.Runner.addToilOptions(parser)
    addCactusWorkflowOptions(parser)

    parser.add_argument("seqFile", help = "Seq file (gzipped fastas supported)")
    parser.add_argument("minigraphGFA", help = "Minigraph-compatible reference graph in GFA format (can be gzipped)")
    parser.add_argument("graphmapPAF", type=str, help = "Output pairwise alignment file in PAF format (can be gzipped)")
    parser.add_argument("--outDir", required=True, type=str, help = "Output directory")
    parser.add_argument("--refContigs", nargs="*", help = "Subset to these reference contigs (multiple allowed)", default=[])
    parser.add_argument("--refContigsFile", type=str, help = "Subset to (newline-separated) reference contigs in this file")
    parser.add_argument("--otherContig", type=str, help = "Lump all reference contigs unselected by above options into single one with this name")
    parser.add_argument("--reference", type=str, help = "Name of reference (in seqFile).  Ambiguity filters will not be applied to it")
    parser.add_argument("--maskFilter", type=int, help = "Ignore softmasked sequence intervals > Nbp")
    
    #Progressive Cactus Options
    parser.add_argument("--configFile", dest="configFile",
                        help="Specify cactus configuration file",
                        default=os.path.join(cactusRootPath(), "cactus_progressive_config.xml"))
    parser.add_argument("--latest", dest="latest", action="store_true",
                        help="Use the latest version of the docker container "
                        "rather than pulling one matching this version of cactus")
    parser.add_argument("--containerImage", dest="containerImage", default=None,
                        help="Use the the specified pre-built containter image "
                        "rather than pulling one from quay.io")
    parser.add_argument("--binariesMode", choices=["docker", "local", "singularity"],
                        help="The way to run the Cactus binaries", default=None)

    options = parser.parse_args()

    setupBinaries(options)
    setLoggingFromOptions(options)
    enableDumpStack()

    # todo: would be very nice to support s3 here
    if options.outDir:
        if not os.path.isdir(options.outDir):
            os.makedirs(options.outDir)
        
    # Mess with some toil options to create useful defaults.
    cactus_override_toil_options(options)

    start_time = timeit.default_timer()
    runCactusGraphMapSplit(options)
    end_time = timeit.default_timer()
    run_time = end_time - start_time
    logger.info("cactus-graphmap-split has finished after {} seconds".format(run_time))

def runCactusGraphMapSplit(options):
    with Toil(options) as toil:
        importSingularityImage(options)
        #Run the workflow
        if options.restart:
            split_id_map = toil.restart()
        else:
            options.cactusDir = getTempDirectory()

            #load cactus config
            configNode = ET.parse(options.configFile).getroot()
            config = ConfigWrapper(configNode)
            config.substituteAllPredefinedConstantsWithLiterals()

            # load up the contigs if any
            ref_contigs = set(options.refContigs)
            # todo: use import?
            if options.refContigsFile:
                with open(options.refContigsFile, 'r') as rc_file:
                    for line in rc_file:
                        if len(line.strip()):
                            ref_contigs.add(line.strip().split()[0])

            if options.otherContig:
                assert options.otherContig not in ref_contigs

            # get the minigraph "virutal" assembly name
            graph_event = getOptionalAttrib(findRequiredNode(configNode, "graphmap"), "assemblyName", default="_MINIGRAPH_")

            # load the seqfile
            seqFile = SeqFile(options.seqFile)

            
            #import the graph
            gfa_id = toil.importFile(makeURL(options.minigraphGFA))

            #import the paf
            paf_id = toil.importFile(makeURL(options.graphmapPAF))

            #import the sequences (that we need to align for the given event, ie leaves and outgroups)
            seqIDMap = {}
            leaves = set([seqFile.tree.getName(node) for node in seqFile.tree.getLeaves()])
            
            if graph_event not in leaves:
                raise RuntimeError("Minigraph name {} not found in seqfile".format(graph_event))
            if options.reference and options.reference not in leaves:
                raise RuntimeError("Name given with --reference {} not found in seqfile".format(options.reference))
                
            for genome, seq in seqFile.pathMap.items():
                if genome in leaves:
                    if os.path.isdir(seq):
                        tmpSeq = getTempFile()
                        catFiles([os.path.join(seq, subSeq) for subSeq in os.listdir(seq)], tmpSeq)
                        seq = tmpSeq
                    seq = makeURL(seq)
                    logger.info("Importing {}".format(seq))
                    seqIDMap[genome] = (seq, toil.importFile(seq))

            # run the workflow
            split_id_map = toil.start(Job.wrapJobFn(graphmap_split_workflow, options, config, seqIDMap,
                                                    gfa_id, options.minigraphGFA,
                                                    paf_id, options.graphmapPAF, ref_contigs, options.otherContig))

        #export the split data
        export_split_data(toil, seqIDMap, split_id_map, options.outDir, config)

def graphmap_split_workflow(job, options, config, seqIDMap, gfa_id, gfa_path, paf_id, paf_path, ref_contigs, other_contig):

    root_job = Job()
    job.addChild(root_job)

    # get the sizes before we overwrite below
    gfa_size = gfa_id.size
    paf_size = paf_id.size
    
    # use file extension to sniff out compressed input
    if gfa_path.endswith(".gz"):
        gfa_id = root_job.addChildJobFn(unzip_gz, gfa_path, gfa_id, disk=gfa_id.size * 10).rv()
        gfa_size *= 10
    if paf_path.endswith(".gz"):
        paf_id = root_job.addChildJobFn(unzip_gz, paf_path, paf_id, disk=paf_id.size * 10).rv()
        paf_size *= 10

    mask_bed_id = None
    if options.maskFilter:
        mask_bed_id = root_job.addChildJobFn(get_mask_bed, seqIDMap, options.maskFilter).rv()
        
    # use rgfa-split to split the gfa and paf up by contig
    split_gfa_job = root_job.addFollowOnJobFn(split_gfa, config, gfa_id, paf_id, ref_contigs,
                                              other_contig, options.reference, mask_bed_id,
                                              disk=(gfa_size + paf_size) * 5)

    # use the output of the above splitting to do the fasta splitting
    split_fas_job = split_gfa_job.addFollowOnJobFn(split_fas, seqIDMap, split_gfa_job.rv())

    # gather everythign up into a table
    gather_fas_job = split_fas_job.addFollowOnJobFn(gather_fas, seqIDMap, split_gfa_job.rv(), split_fas_job.rv())

    # return all the files
    return gather_fas_job.rv()

def get_mask_bed(job, seq_id_map, min_length):
    """ make a bed file from the fastas """
    beds = []
    for event in seq_id_map.keys():
        fa_path, fa_id = seq_id_map[event]
        beds.append(job.addChildJobFn(get_mask_bed_from_fasta, event, fa_id, fa_path, min_length, disk=fa_id.size * 5).rv())
    return job.addFollowOnJobFn(cat_beds, beds).rv()

def get_mask_bed_from_fasta(job, event, fa_id, fa_path, min_length):
    """ make a bed file from one fasta"""
    work_dir = job.fileStore.getLocalTempDir()    
    bed_path = os.path.join(work_dir, os.path.basename(fa_path) + '.mask.bed')
    fa_path = os.path.join(work_dir, os.path.basename(fa_path))
    is_gz = fa_path.endswith(".gz")
    job.fileStore.readGlobalFile(fa_id, fa_path, mutable=is_gz)
    if is_gz:
        cactus_call(parameters=['gzip', '-fd', fa_path])
        fa_path = fa_path[:-3]
    with open(bed_path, 'w') as bed_file, open(fa_path, 'r') as fa_file:
        for seq_record in SeqIO.parse(fa_file, 'fasta'):
            first_mask = None
            for i, c in enumerate(seq_record.seq):
                is_mask = c.islower() or c in ['n', 'N']
                if (is_mask is False or i == len(seq_record.seq) - 1) and first_mask is not None and i - first_mask >= min_length:
                    # we're one past an interval: write it
                    bed_file.write('{}\t{}\t{}\n'.format('id={}|{}'.format(event, seq_record.id), first_mask, i))
                    first_mask = None
                elif is_mask is True and first_mask is None:
                    # we're starting a new interval: remember start position
                    first_mask = i
    return job.fileStore.writeGlobalFile(bed_path)

def cat_beds(job, bed_ids):
    in_beds = [job.fileStore.readGlobalFile(bed_id) for bed_id in bed_ids]
    out_bed = job.fileStore.getLocalTempFile()
    catFiles(in_beds, out_bed)
    return job.fileStore.writeGlobalFile(out_bed)
    
def split_gfa(job, config, gfa_id, paf_id, ref_contigs, other_contig, reference_event, mask_bed_id):
    """ Use rgfa-split to divide a GFA and PAF into chromosomes.  The GFA must be in minigraph RGFA output using
    the desired reference. """

    work_dir = job.fileStore.getLocalTempDir()
    gfa_path = os.path.join(work_dir, "mg.gfa")
    paf_path = os.path.join(work_dir, "mg.paf")
    out_prefix = os.path.join(work_dir, "split_")
    bed_path = os.path.join(work_dir, "mask.bed")
    if (mask_bed_id):
        job.fileStore.readGlobalFile(mask_bed_id, bed_path)

    job.fileStore.readGlobalFile(gfa_id, gfa_path)
    job.fileStore.readGlobalFile(paf_id, paf_path)
    
    # get the minigraph "virutal" assembly name
    graph_event = getOptionalAttrib(findRequiredNode(config.xmlRoot, "graphmap"), "assemblyName", default="_MINIGRAPH_")
    # and look up its unique id prefix.  this will be needed to pick its contigs out of the list
    mg_id = graph_event

    # get the specificity filters
    query_coverage = getOptionalAttrib(findRequiredNode(config.xmlRoot, "graphmap_split"), "minQueryCoverage", default="0")
    small_query_coverage = getOptionalAttrib(findRequiredNode(config.xmlRoot, "graphmap_split"), "minQuerySmallCoverage", default="0")
    small_coverage_threshold = getOptionalAttrib(findRequiredNode(config.xmlRoot, "graphmap_split"), "minQuerySmallThreshold", default="0")
    query_uniqueness = getOptionalAttrib(findRequiredNode(config.xmlRoot, "graphmap_split"), "minQueryUniqueness", default="0")
    amb_event = getOptionalAttrib(findRequiredNode(config.xmlRoot, "graphmap_split"), "ambiguousName", default="_AMBIGUOUS_")

    cmd = ['rgfa-split', '-i', 'id={}|'.format(mg_id), '-G',
           '-g', gfa_path,
           '-p', paf_path,
           '-b', out_prefix,
           '-n', query_coverage,
           '-N', small_query_coverage,
           '-T', small_coverage_threshold,
           '-Q', query_uniqueness,
           '-a', amb_event]
    if other_contig:
        cmd += ['-o', other_contig]
    if reference_event:
        cmd += ['-r', 'id={}|'.format(reference_event)]
    if mask_bed_id:
        cmd += ['-B', bed_path]
        
    for contig in ref_contigs:
        cmd += ['-c', contig]

    cactus_call(parameters=cmd, work_dir=work_dir)

    output_id_map = {}
    for out_name in os.listdir(work_dir):
        file_name, ext = os.path.splitext(out_name)
        if file_name.startswith(os.path.basename(out_prefix)) and ext in [".gfa", ".paf", ".fa_contigs"]:
            name = file_name[len(os.path.basename(out_prefix)):]
            if name not in output_id_map:
                output_id_map[name] = {}
            output_id_map[name][ext[1:]] = job.fileStore.writeGlobalFile(os.path.join(work_dir, out_name))
            
    return output_id_map

def split_fas(job, seq_id_map, split_id_map):
    """ Use samtools to split a bunch of fasta files into reference contigs, using the output of rgfa-split as a guide"""

    root_job = Job()
    job.addChild(root_job)
    
    # map event name to dict of contgs.  ex fa_contigs["CHM13"]["chr13"] = file_id
    fa_contigs = {}
    # we do each fasta in parallel
    for event in seq_id_map.keys():
        fa_path, fa_id = seq_id_map[event]
        fa_contigs[event] = root_job.addChildJobFn(split_fa_into_contigs, event, fa_id, fa_path, split_id_map,
                                                   disk=fa_id.size * 3).rv()

    return fa_contigs

def split_fa_into_contigs(job, event, fa_id, fa_path, split_id_map):
    """ Use samtools turn on fasta into one for each contig. this relies on the informatino in .fa_contigs
    files made by rgfa-split """

    # download the fasta
    work_dir = job.fileStore.getLocalTempDir()
    fa_path = os.path.join(work_dir, os.path.basename(fa_path))
    is_gz = fa_path.endswith(".gz")
    job.fileStore.readGlobalFile(fa_id, fa_path, mutable=is_gz)
    if is_gz:
        # samtools can only work on bgzipped files.  so we uncompress here to be able to support gzipped too
        cactus_call(parameters=['gzip', '-fd', fa_path])
        fa_path = fa_path[:-3]

    unique_id = 'id={}|'.format(event)
                        
    contig_fa_dict = {}

    for ref_contig in split_id_map.keys():
        query_contig_list_id = split_id_map[ref_contig]['fa_contigs']
        list_path = os.path.join(work_dir, '{}.fa_contigs'.format(ref_contig))
        job.fileStore.readGlobalFile(query_contig_list_id, list_path)
        faidx_input_path = os.path.join(work_dir, '{}.fa_contigs.clean'.format(ref_contig))
        contig_count = 0
        with open(list_path, 'r') as list_file, open(faidx_input_path, 'w') as clean_file:
            for line in list_file:
                query_contig = line.strip()
                if query_contig.startswith(unique_id):
                    assert query_contig.startswith(unique_id)
                    query_contig = query_contig[len(unique_id):]
                    clean_file.write('{}\n'.format(query_contig))
                    contig_count += 1
        contig_fasta_path = os.path.join(work_dir, '{}_{}.fa'.format(event, ref_contig))
        if contig_count > 0:
            cmd = ['samtools', 'faidx', fa_path, '--region-file', faidx_input_path]
            if is_gz:
                cmd = [cmd, ['gzip']]
            cactus_call(parameters=cmd, outfile=contig_fasta_path)
        else:
            # TODO: review how cases like this are handled
            with open(contig_fasta_path, 'w') as empty_file:
                empty_file.write("")
        contig_fa_dict[ref_contig] = job.fileStore.writeGlobalFile(contig_fasta_path)

    return contig_fa_dict

def gather_fas(job, seq_id_map, output_id_map, contig_fa_map):
    """ take the split_fas output which has everything sorted by event, and move into the ref-contig-based table
    from split_gfa.  return the updated table, which can then be exported into the chromosome projects """

    for ref_contig in output_id_map.keys():
        output_id_map[ref_contig]['fa'] = {}
        for event, fa_id in contig_fa_map.items():
            output_id_map[ref_contig]['fa'][event] = fa_id[ref_contig]

    return output_id_map

def export_split_data(toil, input_seq_id_map, output_id_map, output_dir, config):
    """ download all the split data locally """

    amb_event = getOptionalAttrib(findRequiredNode(config.xmlRoot, "graphmap_split"), "ambiguousName", default="_AMBIGUOUS_")
    
    chrom_file_map = {}
    
    for ref_contig in output_id_map.keys():
        ref_contig_path = os.path.join(output_dir, ref_contig)
        if not os.path.isdir(ref_contig_path):
            os.makedirs(ref_contig_path)

        # GFA: <output_dir>/<contig>/<contig>.gfa
        if 'gfa' in output_id_map[ref_contig]:
            # we do this check because no gfa made for ambiguous sequences "contig"
            toil.exportFile(output_id_map[ref_contig]['gfa'], makeURL(os.path.join(ref_contig_path, '{}.gfa'.format(ref_contig))))

        # PAF: <output_dir>/<contig>/<contig>.paf
        paf_path = os.path.join(ref_contig_path, '{}.paf'.format(ref_contig))
        toil.exportFile(output_id_map[ref_contig]['paf'], makeURL(paf_path))

        # Fasta: <output_dir>/<contig>/fasta/<event>_<contig>.fa ..
        seq_file_map = {}
        for event, ref_contig_fa_id in output_id_map[ref_contig]['fa'].items():
            fa_base = os.path.join(ref_contig_path, 'fasta')
            if not os.path.isdir(fa_base):
                os.makedirs(fa_base)
            fa_path = makeURL(os.path.join(fa_base, '{}_{}.fa'.format(event, ref_contig)))
            if input_seq_id_map[event][0].endswith('.gz'):
                fa_path += '.gz'
            seq_file_map[event] = fa_path
            toil.exportFile(ref_contig_fa_id, fa_path)

        # Seqfile: <output_dir>/seqfiles/<contig>.seqfile
        seq_file_path = os.path.join(output_dir, 'seqfiles', '{}.seqfile'.format(ref_contig))
        if seq_file_path.startswith('s3://'):
            seq_file_temp_path = getTempFile()
        else:
            seq_file_temp_path = seq_file_path
            if not os.path.isdir(os.path.dirname(seq_file_path)):
                os.makedirs(os.path.dirname(seq_file_path))
        with open(seq_file_temp_path, 'w') as seq_file:
            for event, fa_path in seq_file_map.items():
                # cactus can't handle empty fastas.  if there are no sequences for a sample for this
                # contig, just don't add it.
                if output_id_map[ref_contig]['fa'][event].size > 0:
                    seq_file.write('{}\t{}\n'.format(event, fa_path))
        if seq_file_path.startswith('s3://'):
            write_s3(seq_file_temp_path, seq_file_path)

        # Top-level seqfile
        chrom_file_map[ref_contig] = seq_file_path, paf_path

    # Chromfile : <coutput_dir>/chromfile.txt
    chrom_file_path = os.path.join(output_dir, 'chromfile.txt')
    if chrom_file_path.startswith('s3://'):
        chrom_file_temp_path = getTempFile()
    else:
        chrom_file_temp_path = chrom_file_path        
    with open(chrom_file_temp_path, 'w') as chromfile:
        for ref_contig, seqfile_paf in chrom_file_map.items():
            if ref_contig != amb_event:
                seqfile, paf = seqfile_paf[0], seqfile_paf[1]
                if seqfile.startswith('s3://'):
                    # no use to have absolute s3 reference as cactus-align requires seqfiles passed locally
                    seqfile = 'seqfiles/{}'.format(os.path.basename(seqfile))
                chromfile.write('{}\t{}\t{}\n'.format(ref_contig, seqfile, paf))
    if chrom_file_path.startswith('s3://'):
        write_s3(chrom_file_temp_path, chrom_file_path)
    
if __name__ == "__main__":
    main()
