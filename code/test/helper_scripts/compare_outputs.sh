#! /bin/bash

actual=/tigress/tcomi/aclark4_temp/results/analysis_chr1_test/
expected=/tigress/tcomi/aclark4_temp/results/analysis_chr1/
echo starting comarison of $(basename $actual) to $(basename $expected)

module load anaconda3

for file in $(ls $expected); do
    act=$(echo $file | sed 's/__chr1//')
    if [[ $file = hmm* ]]; then
        cmp <(cat $actual$act | python hmm_format.py) \
            <(cat $expected$file | python hmm_format.py) \
            && echo $file passed! || echo $file failed #&& exit

    elif [[ $file = *.txt ]]; then
        cmp $actual$act $expected$file && echo $file passed! #|| exit
    elif [[ $file = probs* ]]; then
        cmp <(zcat $actual$act | python sort_probs.py) \
            <(zcat $expected$file | python sort_probs.py) \
            && echo $file passed! || echo $file failed #&& exit
    else
        cmp <(zcat $actual$act) <(zcat $expected$file) \
            && echo $file passed! || echo $file failed #&& exit
    fi
done
