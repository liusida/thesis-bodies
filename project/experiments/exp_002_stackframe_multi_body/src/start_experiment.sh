#!/bin/sh
source activate thesis-bodies

set -x

python 0.init.py
EXP_FOLDER=$(cat .exp_folder)
BODY=300,400,500,600
for seed in 0 1 2 3 4 5 6 7 8 9 10 11 12 13 14 15 16 17 18 19
do
    #Control
    sbatch -J $EXP_FOLDER submit.sh python 1.train.py --train_bodies=$BODY --test_bodies=$BODY --seed=$seed --train_steps=5e6
    #Treatment
    sbatch -J $EXP_FOLDER submit.sh python 1.train.py --train_bodies=$BODY --test_bodies=$BODY --seed=$seed --stack_frames=4 --train_steps=5e6
    sbatch -J $EXP_FOLDER submit.sh python 1.train.py --train_bodies=$BODY --test_bodies=$BODY --seed=$seed --stack_frames=8 --train_steps=5e6
    sbatch -J $EXP_FOLDER submit.sh python 1.train.py --train_bodies=$BODY --test_bodies=$BODY --seed=$seed --stack_frames=16 --train_steps=5e6
done
