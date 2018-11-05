# put introgressed genes in a file with relevant info to prioritize
# which ones look interesting

import sys
import os
sys.path.insert(0, '..')
import global_params as gp
sys.path.insert(0, '../misc/')
import read_table

tag = sys.argv[1]
suffix = '_filtered'

# read in filtered regions
fn_regions = gp.analysis_out_dir_absolute + tag + '/' + \
             'introgressed_blocks' + suffix + '_par_' + tag + '_summary_plus.txt'
regions, l = read_table.read_table_rows(fn_regions, '\t')

# read in genes for each region
genes_for_each_region = 

# create dictionary keyed by gene to keep track of strains its
# introgressed in, and corresponding fraction introgressed, and
# regions

# order genes by sum of fractions introgressed

# read in paralogs

# write genes to file

    
    





import sys
from process import *
sys.path.insert(0, '..')
import global_params as gp
sys.path.insert(0, '../sim/')
import sim_analyze_hmm_bw as sim


tag, topology, species_to, species_from1, species_from2, \
    num_samples_species_to, num_samples_species_from1, num_samples_species_from2, \
    N0_species_to, N0_species_from1, N0_species_from2, \
    migration_from1, migration_from2, \
    expected_tract_lengths, \
    expected_num_tracts, \
    has_ref_from1, has_ref_from2, \
    rho, outcross_rate, theta, num_sites, num_reps = \
    sim.process_args(sys.argv)


fn_all = gp.analysis_out_dir_absolute + tag + '/introgressed_hmm_' + tag + \
    '_genes_summary.txt'
f = open(fn_all, 'r')
lines = f.readlines()
header = lines[0]
lines = [x.split('\t') for x in lines][1:]
f.close()

# remove genes in regions where <20 sites uniquely match
# introgressed-from reference
keep_lines = filter(lambda x: float(x[4]) > 20, lines)

# then sort by number of strains introgressed in, decreasing order
keep_lines.sort(key=lambda x: float(x[1]), reverse=True)

# write to file
fn_out = fn_all[:-4] + '_prioritized.txt'
print fn_out
f = open(fn_out, 'w')
f.write(header)
for line in keep_lines:
    f.write('\t'.join(line))
f.close()
