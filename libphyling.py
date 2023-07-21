import contextlib
import logging
import os
import re
import subprocess
import sys
import tempfile
import typing
from copy import deepcopy
from functools import partialmethod
from io import BytesIO
from itertools import chain, product
from multiprocessing.dummy import Pool
from pathlib import Path

import pyhmmer
from Bio import AlignIO, SeqIO
from Bio.Align import MultipleSeqAlignment
from Bio.Seq import Seq
from Bio.SeqRecord import SeqRecord
from clipkit import clipkit
from pyhmmer.plan7 import HMM, HMMFile
from tqdm import tqdm

# Disable tqdm progress bar implemented in clipkit
tqdm.__init__ = partialmethod(tqdm.__init__, disable=True)


class HMMFiles(typing.ContextManager[typing.Iterable[HMM]]):
    def __init__(self, *files: Path) -> None:
        self.stack = contextlib.ExitStack()
        self.hmmfiles = [self.stack.enter_context(HMMFile(f)) for f in files]

    def __enter__(self) -> typing.Iterable[HMM]:
        return chain.from_iterable(self.hmmfiles)

    def __exit__(self, exc_value: object, exc_type: object, traceback: object) -> None:
        self.stack.close()


def concat_Bytes_streams(files: list) -> "tuple[BytesIO, list]":
    """
    Create a in-memory BytesIO to hold the concatenated fasta files.

    Attributes
    ----------
    files : list
        A list of fasta files.

    Return
    ------
    BytesIO
        A BytesIO object that can use as a regular bytes stream.
    list
        A list that recorded the sequence length of each fasta file.
    """
    concat_stream = BytesIO()
    seq_count = []
    for file in files:
        count = 0
        with open(file, "rb") as f:
            for line in f.readlines():
                concat_stream.write(line)
                if line.startswith(b">"):
                    count += 1
            concat_stream.write(b"\n")
        seq_count.append(count)
    concat_stream.seek(0)  # Change the stream position to the start of the stream
    return concat_stream, seq_count


class msa_generator:
    def __init__(self, inputs: list):
        self._inputs = inputs
        concat_stream, self._seq_count = concat_Bytes_streams(inputs)
        seq_file = pyhmmer.easel.SequenceFile(concat_stream, digital=True)
        # Use the concatnated fasta in order to retrieve sequences by index later
        self._sequences = seq_file.read_block()

    def search(self, markerset: Path, evalue: float, threads: int) -> None:
        self._markerset = markerset
        all_hmms = list(self._markerset.iterdir())
        logging.info(f"Found {len(all_hmms)} hmm markers")

        self.orthologs = {}
        self._kh = pyhmmer.easel.KeyHash()
        seq_start_idx = 0
        for idx, sample in enumerate(self._inputs):
            # Select the sequences of each sample
            seq_end_idx = seq_start_idx + self._seq_count[idx]
            logging.debug(f"Sequences start idx: {seq_start_idx}; end idx: {seq_end_idx}")
            sub_sequences = self._sequences[seq_start_idx:seq_end_idx]
            for seq in sub_sequences:
                # Replace description to taxon name
                seq.description = sample.name.encode()
                # Use a KeyHash to store seq.name/index pairs which can be used to retreive
                # ortholog sequences by SequenceObject[kh[seq.name]]
                self._kh.add(seq.name)
            with HMMFiles(*all_hmms) as hmm_file:
                for hits in pyhmmer.hmmsearch(hmm_file, sub_sequences, E=evalue, cpus=threads):
                    cog = hits.query_name.decode()
                    for hit in hits:
                        if hit.included:
                            if cog in self.orthologs:
                                self.orthologs[cog].add(hit.name)
                            else:
                                self.orthologs[cog] = set([hit.name])
                            break  # The first hit in hits is the best hit
            seq_start_idx = seq_end_idx
            logging.info(f"Hmmsearch on {sample.name} done")

    @property
    def filter_orthologs(self):
        try:
            self.orthologs = dict(filter(lambda item: len(item[1]) >= 3, self.orthologs.items()))
        except AttributeError:
            logging.error("No orthologs dictionary found. Please make sure the search function was run successfully")
        logging.info(f"Found {len(self.orthologs)} orthologs shared among at least 3 samples")

    def _run_hmmalign(self, hmm: str, hits: set) -> MultipleSeqAlignment:
        # Create an empty SequenceBlock object to store the sequences of the orthologs
        seqs = pyhmmer.easel.DigitalSequenceBlock(pyhmmer.easel.Alphabet.amino())
        for hit in hits:
            seqs.append(self._sequences[self._kh[hit]])

        # HMMalign the ortholog sequences to the corresponding HMM markers
        with HMMFile(self._markerset / f"{hmm}.hmm") as hmm_file:
            hmm_profile = hmm_file.read()
            MSA = pyhmmer.hmmalign(hmm_profile, seqs, trim=True)

        # Create an empty MultipleSeqAlignment object to store the alignment results
        alignment = MultipleSeqAlignment([])
        for name, aligned_seq, seq_info in zip(MSA.names, MSA.alignment, MSA.sequences):
            alignment.append(
                SeqRecord(
                    Seq(re.sub(r"[ZzBbXx\*\.]", "-", aligned_seq)),
                    id=seq_info.description.decode(),
                    name=name.decode(),
                    description=seq_info.description.decode(),
                )
            )
        return alignment

    def _run_muscle(self, hmm: str, hits: set, output: str) -> MultipleSeqAlignment:
        with open(f"{output}/{hmm}.faa", "wb+") as f:
            for hit in hits:
                seq = self._sequences[self._kh[hit]].copy()
                seq.name = deepcopy(seq.description)
                seq.description = "".encode()
                seq.write(f)

        _ = subprocess.check_call(
            ["muscle", "-align", f"{output}/{hmm}.faa", "-output", f"{output}/{hmm}.aln.faa", "-threads", "1"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.STDOUT,
        )
        alignment = AlignIO.read(f"{output}/{hmm}.aln.faa", "fasta")
        return alignment

    # Fill sequence with "-" for missing samples
    def _fill_missing_taxon(self, taxonList: list, alignment: MultipleSeqAlignment) -> MultipleSeqAlignment:
        missing = set(taxonList) - set([seq.id for seq in alignment])
        for sample in missing:
            alignment.append(
                SeqRecord(
                    Seq("-" * alignment.get_alignment_length()),
                    id=sample,
                    description=sample,
                )
            )
        return alignment

    def _run_clipcit(self, hmm: str, alignment: MultipleSeqAlignment) -> MultipleSeqAlignment:
        # Use clipkit to trim MSA alignment
        keepD, _ = clipkit.keep_trim_and_log(
            alignment,
            gaps=0.9,
            mode=clipkit.TrimmingMode("gappy"),
            use_log=False,
            outFile=f"{hmm}.faa",
            complement=False,
            char=clipkit.SeqType("aa"),
        )

        clipkit.check_if_all_sites_were_trimmed(keepD)

        seqList = []
        for seq in keepD.keys():
            seqList.append(SeqRecord(Seq(str(keepD[seq])), id=str(seq), description=""))
        alignment = MultipleSeqAlignment(seqList)
        return alignment

    def align(self, output: Path, method: str, non_trim: bool, concat: bool, threads: int) -> None:
        concat_alignments = {sample.name: "" for sample in self._inputs}

        concat_alignments = MultipleSeqAlignment([])
        for sample in self._inputs:
            concat_alignments.append(SeqRecord(Seq(""), id=sample.name, description=""))
        concat_alignments.sort()

        # Parallelize the MSA step
        logging.info(f"Use {method} for MSA")
        logging.info(f"Use {threads} threads to parallelize MSA")
        with Pool(threads) as pool:
            if method == "muscle":
                with tempfile.TemporaryDirectory() as tempdir:
                    logging.debug(f"Create tempdir at: {tempdir}")
                    alignmentList = pool.starmap(
                        self._run_muscle, [(hmm, hits, tempdir) for hmm, hits in self.orthologs.items()]
                    )
            else:
                alignmentList = pool.starmap(self._run_hmmalign, [(hmm, hits) for hmm, hits in self.orthologs.items()])
            logging.info("MSA done")

            alignmentList = pool.starmap(
                self._fill_missing_taxon, product([[seq.id for seq in concat_alignments]], alignmentList)
            )
            logging.info("Filling missing taxon done")

            # Output the alingment fasta without clipkit trimming
            if not non_trim:
                with open(os.devnull, "w") as temp_out, contextlib.redirect_stdout(temp_out):
                    alignmentList = pool.starmap(
                        self._run_clipcit, zip([hmm for hmm in self.orthologs.keys()], alignmentList)
                    )
                logging.info("Clipkit done")

        for hmm, alignment in zip([hmm for hmm in self.orthologs.keys()], alignmentList):
            output_aa = output / f"{hmm}.faa"
            alignment.sort()
            if concat:
                concat_alignments += alignment
            else:
                with open(output_aa, "w") as f:
                    SeqIO.write(alignment, f, format="fasta")

        if concat:
            output_concat = output / "concat_alignments.faa"
            with open(output_concat, "w") as f:
                SeqIO.write(concat_alignments, f, format="fasta")
            logging.info(f"Output concatenated fasta to {output_concat}")
        else:
            logging.info(f"Output individual fasta to folder {output}")


def main(inputs, input_dir, output, markerset, evalue, method, non_trim, concat, threads, **kwargs):
    """
    The align module generates multiple sequence alignment (MSA) results from the
    orthologous protein sequences that match the hmm markers across samples.

    First, Hmmsearch is used to match the samples against given markerset and report
    the top hit of each sample for each hmm profile to represent "orthologs" among
    all samples. Note that you should have at least 3 samples since the overall
    purpose is to build a tree.

    Next, the sequences are extracted from each input for orthologs found in more
    than 3 inputs. These sequences are then underwent MSA. (Use hmmalign by default)
    The MSA results will further be trimmed by clipkit by default. If you wish not
    to trim it, use -n/--non_trim to disable the trimming step.

    By default, the alignment results will output separately by hmm marker. The
    consensus tree method should be applied to build a phlogenetic tree.

    If you prefer to use the concatenate strategy, you can use -c/--concat to
    concatenate the aligned sequences by sample and build a single tree afterward.
    """
    # If args.input_dir is used to instead of args.inputs
    if input_dir:
        inputs = list(Path(input_dir).iterdir())
    else:
        inputs = [Path(sample) for sample in inputs]
    # Check input files, terminate if less than 3 files
    if len(inputs) < 3:
        logging.error("Should have at least 3 input files")
        sys.exit(1)
    output = Path(output)
    output.mkdir(exist_ok=True)
    # Check if output dir is empty
    if any(output.iterdir()):
        logging.warning(f"Output directory {output} is not empty. Aborted")
        sys.exit(1)
    markerset = Path(markerset)

    msa = msa_generator(inputs)
    msa.search(markerset, evalue=evalue, threads=threads)
    msa.filter_orthologs
    msa.align(output, non_trim=non_trim, method=method, concat=concat, threads=threads)