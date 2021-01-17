#!/bin/bash
#SBATCH --partition bluemoon
#SBATCH --mem 2G
#SBATCH -c 1

#not used: SBATCH --exclude=node4[00-17,19-32,34-39,41-46,48-99],node5[00-29]
# same as:
#SBATCH --cores-per-socket 7

#SBATCH --output /users/s/l/sliu1/slurm.out/ga/%j-%x

# cd ${SLURM_SUBMIT_DIR}
# source activate thesis-bodies
echo "Job Start at:"
date
# collect some data to optimize slurm command
echo "Node information:"
lscpu
hostname
echo ""
echo ""
echo "Job Command:"
echo $@
echo "Before training, clean the results"
python 22.2.before_train.py $@
echo ""
$@
echo ""
echo "Job End at:"
date
echo ""
echo "After training, reading results and evoke GA"
python 22.3.after_train.py $@