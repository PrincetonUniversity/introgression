import copy
import gzip
import glob
import re
import itertools
from collections import defaultdict, Counter
from hmm import hmm_bw
from sim import sim_predict
from sim import sim_process
import numpy as np
from typing import List, Dict, Tuple, TextIO
from contextlib import ExitStack
import logging as log
from misc.read_fasta import read_fasta
from misc.config_utils import (check_wildcards, validate,
                               get_states, get_nested)


# TODO remove gp references for symbols. pass args or fold into object?
def process_predict_args(arg_list: List[str]) -> Dict:
    '''
    Parses arguments from argv, producing dictionary of parsed values
    '''

    import global_params as gp
    d = {}
    i = 0

    d['tag'] = arg_list[i]
    i += 1

    d['improvement_frac'] = float(arg_list[i])
    i += 1

    d['threshold'] = 'viterbi'
    try:
        d['threshold'] = float(arg_list[i])
    except ValueError:
        pass
    i += 1

    # expected length of introgressed tracts and fraction of sequence
    # introgressed
    expected_tract_lengths = {}
    expected_frac = {}

    d['known_states'] = gp.alignment_ref_order
    for ref in gp.alignment_ref_order[1:]:
        expected_tract_lengths[ref] = float(arg_list[i])
        i += 1
        expected_frac[ref] = float(arg_list[i])
        i += 1

    d['unknown_states'] = []
    while i < len(arg_list):
        state = arg_list[i]
        d['unknown_states'].append(state)
        i += 1
        expected_tract_lengths[state] = float(arg_list[i])
        i += 1
        expected_frac[state] = float(arg_list[i])
        i += 1

    d['states'] = d['known_states'] + d['unknown_states']

    expected_frac[d['states'][0]] = 0
    expected_frac[d['states'][0]] = 1 - sum(expected_frac.values())
    d['expected_frac'] = expected_frac

    # calculate these based on remaining bases, but after we know
    # which chromosome we're looking at
    expected_tract_lengths[d['states'][0]] = 0
    d['expected_tract_lengths'] = expected_tract_lengths
    d['expected_num_tracts'] = {}
    d['expected_bases'] = {}

    return d


class Predictor():
    '''
    Predictor class
    Stores all variables needed to run an HMM prediction
    '''
    def __init__(self, configuration: Dict):
        self.config = configuration
        self.known_states, self.unknown_states = get_states(self.config)
        self.states = self.known_states + self.unknown_states
        self.chromosomes = None
        self.blocks = None
        self.prefix = None
        self.strains = None
        self.hmm_initial = None
        self.hmm_trained = None
        self.positions = None
        self.probabilities = None
        self.alignment = None
        self.threshold = None

    def set_chromosomes(self):
        '''
        Gets the chromosome list from provided config, raising a ValueError
        if undefined.
        '''
        self.chromosomes = validate(
            self.config,
            'chromosomes',
            'No chromosomes specified in config file!')

    def set_blocks_file(self, blocks: str = None):
        '''
        Set the block wildcard filename.  Checks for appropriate wildcards
        '''
        self.blocks = validate(
            self.config,
            'paths.analysis.block_files',
            'No block file provided',
            blocks)

        check_wildcards(self.blocks, 'state')

    def set_prefix(self, prefix: str = ''):
        '''
        Set prefix string of the predictor to the supplied value or
        build it from the known states
        '''
        if prefix == '':
            if self.known_states == []:
                err = 'Unable to build prefix, no known states provided'
                log.exception(err)
                raise ValueError(err)

            self.prefix = '_'.join(self.known_states)
        else:
            self.prefix = prefix

    def set_threshold(self, threshold: str = None):
        '''
        Set the threshold. Checks if set and converts to float if possible
        '''
        self.threshold = validate(
            self.config,
            'analysis_params.threshold',
            'No threshold provided',
            threshold)
        try:
            self.threshold = float(self.threshold)
        except ValueError:
            if self.threshold != 'viterbi':
                err = f'Unsupported threshold value: {self.threshold}'
                log.exception(err)
                raise ValueError(err)

    def set_strains(self, test_strains: str = ''):
        '''
        build the strains to perform prediction on
        '''
        if test_strains == '':
            test_strains = get_nested(self.config, 'paths.test_strains')
        else:
            # need to support list for test strains
            test_strains = [test_strains]

        if test_strains is not None:
            for test_strain in test_strains:
                check_wildcards(test_strain, 'strain,chrom')

        self.find_strains(test_strains)

    def find_strains(self, test_strains: List[str] = None):
        '''
        Helper method to get strains supplied in config, or from test_strains
        '''
        strains = get_nested(self.config, 'strains')
        self.test_strains = test_strains

        if strains is None:
            if test_strains is None:
                err = ('Unable to find strains in config and '
                       'no test_strains provided')
                log.exception(err)
                raise ValueError(err)

            # try to build strains from wildcards in test_strains
            strains = {}
            for test_strain in test_strains:
                # find matching files
                strain_glob = test_strain.format(
                    strain='*',
                    chrom='*')
                log.info(f'searching for {strain_glob}')
                for fname in glob.iglob(strain_glob):
                    # extract wildcard matches
                    print(fname)
                    match = re.match(
                        test_strain.format(
                            strain='(?P<strain>.*?)',
                            chrom='(?P<chrom>[^_]*?)'
                        ),
                        fname)
                    if match:
                        log.debug(
                            f'matched with {match.group("strain", "chrom")}')
                        strain, chrom = match.group('strain', 'chrom')
                        if strain not in strains:
                            strains[strain] = []
                        strains[strain].append(chrom)

            if len(strains) == 0:
                err = ('Found no chromosome sequence files '
                       f'in {test_strains}')
                log.exception(err)
                raise ValueError(err)

            for strain, chroms in strains.items():
                if len(self.chromosomes) != len(chroms):
                    err = (f'Strain {strain} has incorrect number of '
                           f'chromosomes. Expected {len(self.chromosomes)} '
                           f'found {len(chroms)}')
                    log.exception(err)
                    raise ValueError(err)

            self.strains = list(sorted(strains.keys()))

        else:  # strains set in config
            self.strains = list(sorted(set(strains)))

    def set_output_files(self,
                         hmm_initial: str,
                         hmm_trained: str,
                         positions: str,
                         probabilities: str,
                         alignment: str):
        '''
        Set output files from provided values or config.
        Raises value errors if a file is not provided.
        Checks alignment for all wildcards and replaces prefix.
        '''
        self.hmm_initial = validate(self.config,
                                    'paths.analysis.hmm_initial',
                                    'No initial hmm file provided',
                                    hmm_initial)

        self.hmm_trained = validate(self.config,
                                    'paths.analysis.hmm_trained',
                                    'No trained hmm file provided',
                                    hmm_trained)

        if positions == '':
            self.positions = get_nested(self.config,
                                        'paths.analysis.positions')
        else:
            self.positions = positions

        self.probabilities = validate(self.config,
                                      'paths.analysis.probabilities',
                                      'No probabilities file provided',
                                      probabilities)

        alignment = validate(self.config,
                             'paths.analysis.alignment',
                             'No alignment file provided',
                             alignment)
        check_wildcards(alignment, 'prefix,strain,chrom')
        self.alignment = alignment.replace('{prefix}', self.prefix)

    def validate_arguments(self):
        '''
        Check that all required instance variables are set to perform a
        prediction run. Returns true if valid, raises value error otherwise
        '''
        args = [
            'chromosomes',
            'blocks',
            'prefix',
            'strains',
            'hmm_initial',
            'hmm_trained',
            'probabilities',
            'alignment',
            'known_states',
            'unknown_states',
            'threshold',
        ]
        variables = self.__dict__
        for arg in args:
            if variables[arg] is None:
                err = ('Failed to validate Predictor, required argument '
                       f'{arg} was unset')
                log.exception(err)
                raise ValueError(err)

        # check the parameters for each state are present
        known_states = get_nested(self.config,
                                  'analysis_params.known_states')
        if known_states is None:
            err = 'Configuration did not provide any known_states'
            log.exception(err)
            raise ValueError(err)

        for s in known_states:
            if 'expected_length' not in s:
                err = f'{s["name"]} did not provide an expected_length'
                log.exception(err)
                raise ValueError(err)
            if 'expected_fraction' not in s:
                err = f'{s["name"]} did not provide an expected_fraction'
                log.exception(err)
                raise ValueError(err)

        unknown_states = get_nested(self.config,
                                    'analysis_params.unknown_states')
        if unknown_states is not None:
            for s in unknown_states:
                if 'expected_length' not in s:
                    err = f'{s["name"]} did not provide an expected_length'
                    log.exception(err)
                    raise ValueError(err)
                if 'expected_fraction' not in s:
                    err = f'{s["name"]} did not provide an expected_fraction'
                    log.exception(err)
                    raise ValueError(err)

        reference = get_nested(self.config,
                               'analysis_params.reference')
        if reference is None:
            err = f'Configuration did not specify a reference strain'
            log.exception(err)
            raise ValueError(err)

        return True

    def run_prediction(self, only_poly_sites=True):
        '''
        Run prediction with this predictor object
        '''
        self.validate_arguments()

        hmm_builder = HMM_Builder(self.config)
        hmm_builder.set_expected_values()
        self.emission_symbols = \
            hmm_builder.update_emission_symbols(len(self.known_states))

        with open(self.hmm_initial, 'w') as initial, \
                open(self.hmm_trained, 'w') as trained, \
                gzip.open(self.probabilities, 'wt') as probabilities, \
                ExitStack() as stack:

            self.write_hmm_header(initial)
            self.write_hmm_header(trained)

            if self.positions is not None:
                positions = stack.enter_context(
                    gzip.open(self.positions, 'wt'))
            else:
                positions = None

            block_writers = {state:
                             stack.enter_context(
                                 open(self.blocks.format(state=state), 'w'))
                             for state in
                             self.states}
            for writer in block_writers.values():
                self.write_blocks_header(writer)

            for chrom in self.chromosomes:
                for strain in self.strains:
                    log.info(f'working on: {strain} {chrom}')

                    # get sequences and encode
                    alignment_file = self.alignment.format(
                        strain=strain, chrom=chrom)

                    hmm_initial, hmm_trained, pos = hmm_builder.run_hmm(
                        alignment_file, only_poly_sites)

                    self.write_hmm(hmm_initial, initial, strain, chrom)
                    self.write_hmm(hmm_trained, trained, strain, chrom)

                    # process and threshold hmm result
                    predicted_states, probs = self.process_path(hmm_trained)
                    state_blocks = self.convert_to_blocks(predicted_states)

                    if positions is not None:
                        self.write_positions(pos, positions, strain, chrom)

                    for state, block in state_blocks.items():
                        self.write_blocks(block,
                                          pos,
                                          block_writers[state],
                                          strain,
                                          chrom,
                                          state)

                    self.write_state_probs(probs, probabilities, strain, chrom)

    def write_hmm_header(self, writer: TextIO) -> None:
        '''
        Write the header line for an hmm file to the provided textIO object
        Output is tab delimited with:
        strain chromosome initial_probs emissions transitions
        '''

        writer.write('strain\tchromosome\t')

        states = self.known_states + self.unknown_states

        writer.write('\t'.join(
            [f'init_{s}' for s in states] +  # initial
            [f'emis_{s}_{symbol}'
             for s in states
             for symbol in self.emission_symbols] +  # emissions
            [f'trans_{s1}_{s2}'
             for s1 in states
             for s2 in states]))  # transitions

        writer.write('\n')

    def write_hmm(self,
                  hmm: hmm_bw.HMM,
                  writer: TextIO,
                  strain: str,
                  chrm: str):
        '''
        Write information on the provided hmm as a line to the supplied textIO
        object.
        Output is tab delimited with:
        strain chromosome initial_probs emissions transitions
        '''
        writer.write(f'{strain}\t{chrm}\t')

        states = len(hmm.hidden_states)
        writer.write('\t'.join(
            [f'{p}' for p in hmm.initial_p] +  # initial
            [f'{hmm.emissions[i, hmm.symbol_to_ind[symbol]]}'
             if symbol in hmm.symbol_to_ind else '0.0'
             for i in range(states)
             for symbol in self.emission_symbols] +  # emission
            [f'{hmm.transitions[i, j]}'
             for i in range(states)
             for j in range(states)]  # transition
        ))
        writer.write('\n')

    def write_blocks_header(self, writer: TextIO) -> None:
        '''
        Write header line to tab delimited block file:
        strain chromosome predicted_species start end num_sites_hmm
        '''
        # NOTE: num_sites_hmm represents the sites considered by the HMM,
        # so it might exclude non-polymorphic sites in addition to gaps
        writer.write('\t'.join(['strain',
                                'chromosome',
                                'predicted_species',
                                'start',
                                'end',
                                'num_sites_hmm'])
                     + '\n')

    def write_blocks(self,
                     state_seq_blocks: List[Tuple[int, int]],
                     positions: np.array,
                     writer: TextIO,
                     strain: str,
                     chrm: str,
                     species_pred: str) -> None:
        '''
        Write entry into tab delimited block file, with columns:
        strain chromosome predicted_species start end num_sites_hmm
        '''
        writer.write('\n'.join(
            ['\t'.join([strain,
                        chrm,
                        species_pred,
                        str(positions[start]),
                        str(positions[end]),
                        str(end - start + 1)])
             for start, end in state_seq_blocks]))
        if state_seq_blocks:  # ensure ends with \n
            writer.write('\n')

    def write_positions(self,
                        positions: np.array,
                        writer: TextIO,
                        strain: str,
                        chrm: str) -> None:
        '''
        Write the positions of the specific strain, chromosome as a line to the
        provided textIO object
        '''
        writer.write(f'{strain}\t{chrm}\t' +
                     '\t'.join([str(x) for x in positions]) + '\n')

    def write_state_probs(self,
                          probs: Dict[str, List[float]],
                          writer: TextIO,
                          strain: str,
                          chrm: str) -> None:
        '''
        Write the probability of each state to the supplied textIO object
        Output is tab delimited with:
        strain chrom state1:prob1,prob2,...,probn state2...
        '''
        writer.write(f'{strain}\t{chrm}\t')

        writer.write('\t'.join(
            [f'{state}:' +
             ','.join([f'{site[i]:.5f}' for site in probs])
             for i, state in enumerate(self.states)]))

        writer.write('\n')

    def process_path(self, hmm: hmm_bw.HMM) -> Tuple[List[str], np.array]:
        '''
        Process the hmm path based the the predictor threshold value
        Return the predicted states and the probabilities of the master
        reference sequence
        '''
        probabilities = hmm.posterior_decoding()[0]

        # posterior
        if type(self.threshold) is float:
            path, path_probs = sim_process.get_max_path(probabilities,
                                                        hmm.hidden_states)
            path_t = sim_process.threshold_predicted(path, path_probs,
                                                     self.threshold,
                                                     self.known_states[0])
            return path_t, probabilities

        else:
            predicted = sim_predict.convert_predictions(hmm.viterbi(),
                                                        self.states)
            return predicted, probabilities

    def convert_to_blocks(self,
                          state_seq: List[str]) -> Dict[
                              str, List[Tuple[int, int]]]:
        '''
        Convert a list of sequences into a structure of start and end positions
        Return structure is a dict keyed on species with values of Lists of
        each block, which is a tuple with start and end positions
        '''
        # single individual state sequence
        blocks = {}
        for state in self.states:
            blocks[state] = []
        prev_species = state_seq[0]
        block_start = 0
        block_end = 0
        for i in range(len(state_seq)):
            if state_seq[i] == prev_species:
                block_end = i
            else:
                blocks[prev_species].append((block_start, block_end))
                block_start = i
                block_end = i
                prev_species = state_seq[i]
        # add last block
        if prev_species not in blocks:
            blocks[prev_species] = []
        blocks[prev_species].append((block_start, block_end))

        return blocks


class HMM_Builder():
    def __init__(self, configuration):
        self.config = configuration
        self.symbols = {
            'match': '+',
            'mismatch': '-',
            'unknown': '?',
            'unsequenced': 'n',
            'gap': '-',
            'unaligned': '?',
            'masked': 'x'
        }
        config_symbols = get_nested(self.config, 'HMM_symbols')
        if config_symbols is not None:
            for k, v in config_symbols.items():
                if k not in self.symbols:
                    log.warning("Unused symbol in configuration: "
                                f"{k} -> '{v}'")
                else:
                    self.symbols[k] = v
                    log.debug(f"Overwriting default symbol for {k} with '{v}'")

            for k, v in self.symbols.items():
                if k not in config_symbols:
                    log.warning(f'Symbol for {k} unset in config, '
                                f"using default '{v}'")

        else:
            for k, v in self.symbols.items():
                log.warning(f'Symbol for {k} unset in config, '
                            f"using default '{v}'")

        self.convergence = get_nested(self.config,
                                      'analysis_params.convergence_threshold')
        if self.convergence is None:
            log.warning('No value set for convergence_threshold, using '
                        'default of 0.001')
            self.convergence = 0.001

    def update_emission_symbols(self, repeats: int):
        '''
        Generate all permutations of match and mismatch symbols with
        repeats number of characters, in lexigraphical order.
        Sets internal state and returns the emission symbols
        '''
        syms = [self.symbols['match'], self.symbols['mismatch']]
        emis_symbols = [''.join(x) for x in
                        itertools.product(syms,
                                          repeat=repeats)]
        emis_symbols.sort()
        self.emission_symbols = emis_symbols
        return emis_symbols

    def get_symbol_freqs(self, sequence: np.array) -> Tuple[Dict, List]:
        '''
        Calculate metrics from the provided, coded sequence
        Returns:
        the fraction of each matching pattern (e.g. +--++)
        the weighted fraction of matches for each species
        '''

        weighted = []

        symbols = defaultdict(int, Counter(sequence))
        total = len(sequence)
        for k in symbols:
            symbols[k] /= total

        sequence = np.array([list(s) for s in sequence])

        # look along species
        for s in np.transpose(sequence):
            s = ''.join(s)
            counts = Counter(s)
            weighted.append(counts[self.symbols['match']])

        total = sum(weighted)
        weighted = [w / total for w in weighted]
        return symbols, weighted

    def set_expected_values(self):
        '''
        Get expected lengths and fractions for each state.
        Assumes config has been validated by Predictor prior to running
        '''
        self.expected_lengths = {}
        self.expected_fractions = {}
        known_states = get_nested(self.config,
                                  'analysis_params.known_states')
        for state in known_states:
            self.expected_lengths[state['name']] = state['expected_length']
            self.expected_fractions[state['name']] = state['expected_fraction']

        unknown_states = get_nested(self.config,
                                    'analysis_params.unknown_states')
        for state in unknown_states:
            self.expected_lengths[state['name']] = state['expected_length']
            self.expected_fractions[state['name']] = state['expected_fraction']

        reference = get_nested(self.config,
                               'analysis_params.reference')
        # expected fraction of reference is the remainder after other states
        # are specified
        self.expected_fractions[reference['name']] =\
            1 - sum(self.expected_fractions.values())

        self.known_states, self.unknown_states = get_states(self.config)

        self.ref_state = get_nested(self.config,
                                    'analysis_params.reference.name')

        # have to remove effect of unknown of these values for later
        self.ref_fraction = self.expected_fractions[self.ref_state] + \
            sum([self.expected_fractions[s] for s in self.unknown_states])
        # sum of fraction / length, or 1 / tract length
        self.other_sum = sum([self.expected_fractions[s['name']] /
                              self.expected_lengths[s['name']]
                              for s in known_states])

    def update_expected_length(self, total_length: int):
        '''
        Updates the expected length for the reference state
        based on the provided total_length of the sequence.
        This is the expected length of a single tract, determined as the sum
        of the total length (sequence length * fraction) divided by the number
        of tracts (sequence length * 1 / other's tracts). The + 1 assumes that
        the sequence will start and end with the reference.
        '''
        self.expected_lengths[self.ref_state] = (
            total_length * self.ref_fraction /
            (total_length * self.other_sum + 1))

    def initial_probabilities(self,
                              weighted_match_freqs: List[float]) -> np.array:
        '''
        Estimate the initial probability of being in each state
        based on the number of states and their expected fractions
        Returns the initial probability of each state
        '''

        init = []
        expectation_weight = .9
        for s, state in enumerate(self.known_states):
            expected = self.expected_fractions[state]
            estimated = weighted_match_freqs[s]
            init.append(expected * expectation_weight +
                        estimated * (1 - expectation_weight))

        for state in self.unknown_states:
            expected_frac = self.expected_fractions[state]
            init.append(expected_frac)

        return init / np.sum(init)

    def emission_probabilities(self,
                               symbols: List[str]) -> List[Dict]:
        '''
        Estimate initial emission probabilities
        Return estimates as list of default dict of probabilities
        '''

        match = self.symbols['match']
        mismatch = self.symbols['mismatch']
        probabilities = {
            mismatch + match: 0.9,
            match + match: 0.09,
            mismatch + mismatch: 0.009,
            match + mismatch: 0.001,
        }

        mismatch_bias = .99

        num_per_category = 2 ** (len(self.known_states) - 2)
        for key in probabilities:
            probabilities[key] *= num_per_category

        # for known states
        symbol_array = np.array([list(s) for s in symbols], dtype='<U1')
        # for unknown states
        symbol_length = symbol_array.shape[1]
        number_matches = (symbol_array == match).sum(axis=1)
        # combine first column with rest to generate probabilities
        first_column = np.tile(symbol_array[:, 0:1],
                               (1, len(self.known_states)))
        symbol_array = np.core.defchararray.add(
            first_column, symbol_array[:, 0:len(self.known_states)])
        # index into probabilities and normalize
        emissions = np.vectorize(probabilities.__getitem__)(symbol_array)
        emissions /= sum(emissions)

        # convert to match * (1-bias) + mismatch * bias, simplified
        number_matches = (number_matches + mismatch_bias *
                          (symbol_length - 2 * number_matches))
        number_matches /= sum(number_matches)
        # repeat for each unknown state
        number_matches = np.transpose(
            np.tile(number_matches, (len(self.unknown_states), 1)))

        # convert result into default dict
        result = [defaultdict(float,
                              {k: v for k, v in
                               zip(symbols, emissions[:, i])})
                  for i in range(emissions.shape[1])]
        result.extend([defaultdict(float,
                                   {k: v for k, v in
                                    zip(symbols, number_matches[:, i])})
                       for i in range(number_matches.shape[1])])

        return result

    def transition_probabilities(self) -> np.array:
        '''
        Estimate initial transition probabilities
        '''

        # doesn't depend on sequence observations but maybe it should?

        # also should we care about number of tracts rather than fraction
        # of genome? maybe theoretically, but that number is a lot more
        # suspect

        states = self.known_states + self.unknown_states

        fractions = np.array([self.expected_fractions[s] for s in states])
        lengths = 1/np.array([self.expected_lengths[s] for s in states])

        # general case,
        # trans[i,j] = 1/ length[i] * expected[j] * 1 /(1 - fraction[i])
        transitions = np.outer(
            np.multiply(lengths, 1/(1-fractions)),
            fractions)
        # when i == j, trans[i,j] = 1 - 1/length[i]
        np.fill_diagonal(transitions, 1-lengths)

        # normalize
        return transitions / transitions.sum(axis=1)[:, None]

    def build_initial_hmm(self, seq: np.array) -> hmm_bw.HMM:
        '''
        Build a HMM object initialized based on expected values and sequence
        '''

        # get frequencies of individual symbols (e.g. '+') and all full
        # combinations of symbols (e.g. '+++-')
        (symbol_freqs,
         weighted_match_freqs) = self.get_symbol_freqs(seq)

        # new Hidden Markov Model
        hmm = hmm_bw.HMM()

        hmm.set_initial_p(self.initial_probabilities(weighted_match_freqs))
        hmm.set_emissions(self.emission_probabilities(symbol_freqs.keys()))
        hmm.set_transitions(self.transition_probabilities())
        return hmm

    def run_hmm(self,
                alignment_file: str,
                only_poly_sites: bool = True) -> Tuple[hmm_bw.HMM,
                                                       hmm_bw.HMM,
                                                       np.array]:
        '''
        Runs the hmm training, returning the initial and trained HMM along
        with the positions of hmm importance
        '''
        coded_sequence, positions, len_seq = \
            self.encode_sequence(alignment_file, only_poly_sites)

        self.update_expected_length(len_seq)
        # set initial hmm parameters based on combination of (1) initial
        # expectations (length of introgressed tract and fraction of
        # genome/total number tracts and bases) and (2) number of sites at
        # which predict seq matches each reference
        hmm = self.build_initial_hmm(coded_sequence)

        # set states and initial probabilties
        hmm.set_hidden_states(self.known_states + self.unknown_states)

        # copy before setting observations to save memory
        hmm_init = copy.deepcopy(hmm)

        # set obs
        hmm.set_observations([coded_sequence])

        # Baum-Welch parameter estimation
        hmm.train(self.convergence)

        return hmm_init, hmm, positions

    def encode_sequence(self,
                        alignment_file: str,
                        only_poly_sites: bool = True) -> Tuple[
                            np.array,
                            np.array,
                            int]:
        '''
        open the supplied alignment file, encode, and return the coded
        sequence along with the positions.  If only_poly_sites is True,
        also filter out non-polymorphic sites.
        Returns the encoded sequence, positions, and length of original seq
        '''
        _, sequences = read_fasta(alignment_file)

        references = sequences[:-1]
        predicted = sequences[-1]

        seq_coded, positions = self.ungap_and_code(predicted, references)
        if only_poly_sites:
            seq_coded, positions = self.poly_sites(seq_coded, positions)

        return seq_coded, positions, len(predicted)

    def ungap_and_code(self,
                       predict_seq: str,
                       ref_seqs: List[str],
                       index_ref: int = 0) -> Tuple[np.array, np.array]:
        '''
        Remove any sequence locations where a gap is present and code
        into matching or mismatching sequence
        Returns the coded sequences, by default an array of + where matching, -
        where mismatching.  Also return the positions where the sequences are
        not gapped.
        '''
        # index_ref is index of reference strain to index relative to
        # build character array
        sequences = np.array([list(predict_seq)] +
                             [list(r) for r in ref_seqs])

        isbase = sequences != self.symbols['gap']

        # make boolean for valid characters
        isvalid = np.logical_and(sequences != self.symbols['gap'],
                                 sequences != self.symbols['unsequenced'])

        # positions are where everything is valid, index where the reference is
        # valid.  The +1 removes the predict sequence at index 0
        positions = np.where(
            np.all(isvalid[:, isbase[index_ref+1, :]], axis=0))[0]

        matches = np.where(sequences[0] == sequences[1:],
                           self.symbols['match'],
                           self.symbols['mismatch'])

        matches = np.fromiter((''.join(row)
                               for row in np.transpose(
                                   matches[:, np.all(isvalid, axis=0)])),
                              dtype=f'U{len(sequences) - 1}')

        return matches, positions

    def poly_sites(self,
                   sequences: np.array,
                   positions: np.array) -> Tuple[np.array, np.array]:
        '''
        Remove all sequences where the sequence is all match_symbol
        Returns the filtered sequence and position
        '''
        seq_len = len(sequences[0])
        # check if seq only contains match_symbol
        retain = np.vectorize(
            lambda x: x.count(self.symbols['match']) != seq_len)(sequences)
        indices = np.where(retain)[0]

        ps_poly = positions[indices]
        seq_poly = sequences[indices]

        return seq_poly, ps_poly


def read_positions(filename: str) -> Dict[str, Dict[str, List[int]]]:
    '''
    Read in positions from the provided filename, returning a dictionary
    keyed first by the strain, then chromosome.  Returned positions are
    lists of ints
    '''
    with gzip.open(filename, 'rt') as reader:
        result = defaultdict({})
        for line in reader:
            line = line.split()
            strain, chrm = line[0:2]
            positions = [int(x) for x in line[2:]]
            result[strain][chrm] = positions
    return result


def read_blocks(filename: str,
                labeled: bool = False) -> Dict[
                    str, Dict[str, Tuple[int, int, int, str]]]:
    '''
    Read in the supplied block file, returning a dict keyed on strain,
    then chromosome.  Values are tuples of start, end, and number of postions
    for the block.
    If labeled is true, values contain the region_id as last element
    '''
    with open(filename, 'r') as reader:
        reader.readline()  # header
        result = defaultdict(lambda: defaultdict(list))
        for line in reader:
            tokens = line.split()
            if labeled:
                (region_id, strain, chrm, species,
                 start, end, number_non_gap) = tokens
                item = (region_id, int(start), int(end), int(number_non_gap))
            else:
                (strain, chrm, species,
                 start, end, number_non_gap) = tokens
                item = (int(start), int(end), int(number_non_gap))
            result[strain][chrm].append(item)
    return result
