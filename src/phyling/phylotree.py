"""Phylogenetic tree construction methods."""
from __future__ import annotations

import logging
import subprocess
import tempfile
from io import StringIO, BytesIO
from pathlib import Path

import matplotlib.pyplot as plt
from Bio import AlignIO, Phylo
from Bio.Phylo.TreeConstruction import DistanceCalculator, DistanceTreeConstructor


class tree_generator:
    def __init__(self, method: str, *file: Path):
        self._file = file
        self.method = method

    def _with_VeryFastTree(self, file) -> Phylo.BaseTree.Tree:
        stream = BytesIO()
        with open(file, "r") as f:
            for line in f.read().splitlines():
                if not line.startswith(">"):
                    line = line.upper()
                stream.write(line.encode())
                stream.write(b"\n")
        stream.seek(0)
        p = subprocess.Popen(
            ["VeryFastTree", "-lg", "-gamma"],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        tree, _ = p.communicate(stream.read())
        stream.close()
        return Phylo.read(StringIO(tree.decode().strip("\n")), "newick")

    def _with_phylo_module(self, file) -> Phylo.BaseTree.Tree:
        """Run the tree calculation using a simple distance method."""
        MSA = AlignIO.read(file, format="fasta")
        calculator = DistanceCalculator("identity")
        constructor = DistanceTreeConstructor(calculator, self._method)
        return constructor.build_tree(MSA)

    def _build(self, file) -> Phylo.BaseTree.Tree:
        if self.method in ["upgma", "nj"]:
            tree = self._with_phylo_module(file)
        if self.method == "ft":
            tree = self._with_VeryFastTree(file)
        return tree

    def get(self) -> list[Phylo.BaseTree.Tree]:
        trees = []
        for file in self._file:
            trees.append(self._build(file))
        return trees


def run_astral(file: Path):
    """Run astral to get consensus tree."""
    final_tree = subprocess.check_output(
        ["astral", "-i", file],
        # stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    return Phylo.read(StringIO(final_tree.decode().strip("\n")), "newick")


def phylotree(inputs, input_dir, output, method, figure, **kwargs):
    """
    Construct a phylogenetic tree based on the results of multiple sequence alignment (MSA).

    If multiple MSA results are given, the consensus tree method will be employed, using a 50% cutoff to represent
    the majority of all the trees.

    By default, the UPGMA algorithm is used for tree construction. Users can switch to the Neighbor Joining method by
    specifying the -m/--method nj.

    Once the tree is built, an ASCII figure representing the tree will be displayed, and a treefile in Newick format
    will be generated as output. Additionally, users can choose to obtain a matplotlib-style figure using the
    -f/--figure option.
    """
    # If args.input_dir is used to instead of args.inputs
    logging.info(f"Algorithm choose for tree building: {method}")
    if input_dir:
        inputs = list(Path(input_dir).iterdir())
    else:
        inputs = [Path(sample) for sample in inputs]
    logging.info(f"Found {len(inputs)} MSA fasta")
    if inputs[0].name == "concat_alignments.faa":
        logging.info("Generate phylogenetic tree the on concatenated fasta")
    else:
        logging.info("Generate phylogenetic tree on all MSA fasta and conclude an majority consensus tree")
    output = Path(output)
    output.mkdir(exist_ok=True)

    tree_generator_obj = tree_generator(method, *inputs)
    trees = tree_generator_obj.get()
    if len(trees) == 1:
        final_tree = trees[0]
    else:
        with tempfile.TemporaryDirectory() as tempdir:
            trees_file = f"{tempdir}/trees.nw"
            Phylo.write(trees, trees_file, "newick")
            final_tree = run_astral(trees_file)
    Phylo.draw_ascii(final_tree)

    output_tree = output / f"{method}_tree.nw"
    logging.info(f"Output tree to {output_tree}")
    with open(output_tree, "w") as f:
        Phylo.write(final_tree, f, "newick")

    if figure:
        fig, ax = plt.subplots(figsize=(20, 12))
        output_fig = output / f"{method}_tree.png"
        Phylo.draw(final_tree, axes=ax)
        fig.savefig(output_fig)
