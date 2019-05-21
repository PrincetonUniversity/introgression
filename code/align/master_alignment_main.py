# combine all chromosomal alignments into one master, indexed relative
# to one reference

import sys
from align.master_alignment import (make_master, write_master)
from align.align_helpers import (get_strains, flatten)
import global_params as gp

strains = get_strains(flatten(gp.non_ref_dirs.values()))

strain_ind = int(sys.argv[1]) - 1

gp_dir = '../'

strain = strains[strain_ind][0]

alignment_prefix = gp_dir + gp.alignments_dir + \
    '_'.join(gp.alignment_ref_order) + \
    '_' + strain
for chrm in gp.chrms:
    print(chrm)
    alignment_fn = alignment_prefix + '_chr' + chrm + gp.alignment_suffix
    master_alignment_fn = (alignment_prefix + '_chr' +
                           chrm + '_master' + gp.fasta_suffix)
    a = make_master(alignment_fn, gp.master_ref)
    write_master(master_alignment_fn, a)
